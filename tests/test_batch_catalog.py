import csv
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from batch_catalog import BatchCatalog, normalize_relative_path
import migrate_batch_catalog


def write_csv(path: Path, fields, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class BatchCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.instance = self.root / "instance"
        self.catalog = BatchCatalog()
        self.batch_id = self.catalog.upsert_batch(
            self.instance, "SS100/batch-1", slide_count=1
        )
        self.catalog.replace_queue(
            self.instance,
            self.batch_id,
            [{"original_index": 0, "status": "pending"}],
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_relative_paths_are_portable_and_case_insensitively_unique(self):
        self.assertEqual("SS100/batch-1", normalize_relative_path(r"SS100\batch-1"))
        same_id = self.catalog.upsert_batch(self.instance, "SS100/BATCH-1")

        rows = self.catalog.list_batches(self.instance)

        self.assertEqual(self.batch_id, same_id)
        self.assertEqual(1, len(rows))

    def test_concurrent_claims_lease_item_to_only_one_user(self):
        barrier = threading.Barrier(2)
        results = []

        def claim(user_id):
            barrier.wait()
            results.append(
                self.catalog.claim_item(
                    self.instance, self.batch_id, user_id, "2026-08-28T12:00:00"
                )
            )

        threads = [threading.Thread(target=claim, args=(user,)) for user in ("one", "two")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        claimed = [row for row in results if row is not None]
        self.assertEqual(1, len(claimed))
        self.assertEqual("leased", claimed[0]["status"])
        stored = self.catalog.load_queue(self.instance, self.batch_id)
        self.assertEqual(claimed[0]["leased_by_id"], stored[0]["leased_by_id"])

    def test_stage_update_preserves_other_batch_and_queue(self):
        other_id = self.catalog.upsert_batch(self.instance, "SS200/batch-2")

        self.catalog.update_stages(self.instance, self.batch_id, qc_complete=True)

        first = self.catalog.get_batch(self.instance, self.batch_id)
        other = self.catalog.get_batch(self.instance, other_id)
        self.assertTrue(first["qc_complete"])
        self.assertFalse(first["renamed_complete"])
        self.assertFalse(other["qc_complete"])
        self.assertEqual(1, first["queue_total"])

    def test_run_type_is_stored_and_validated(self):
        batch_id = self.catalog.upsert_batch(
            self.instance, "SS300/2026-10-02", run_type="on_demand"
        )

        self.assertEqual("on_demand", self.catalog.get_batch(self.instance, batch_id)["run_type"])
        with self.assertRaisesRegex(ValueError, "invalid run type"):
            self.catalog.upsert_batch(
                self.instance, "SS300/2026-10-03", run_type="unknown"
            )

    def test_schema_version_one_is_upgraded_with_transfer_tables(self):
        legacy_instance = self.root / "legacy-instance"
        legacy_instance.mkdir()
        database = legacy_instance / "batch_catalog.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE catalog_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO catalog_metadata(key,value) VALUES('schema_version','1')"
        )
        connection.commit()
        connection.close()

        legacy_catalog = BatchCatalog()
        self.assertEqual([], legacy_catalog.list_batches(legacy_instance))

        connection = sqlite3.connect(database)
        try:
            version = connection.execute(
                "SELECT value FROM catalog_metadata WHERE key='schema_version'"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertEqual("4", version)
        self.assertIn("transfer_slides", tables)
        self.assertIn("transfer_sources", tables)
        self.assertIn("batch_documents", tables)
        self.assertTrue(
            (legacy_instance / "batch_catalog.sqlite3.pre-v4.backup").is_file()
        )

    def test_schema_version_two_adds_nightly_run_type(self):
        legacy_instance = self.root / "version-two-instance"
        legacy_catalog = BatchCatalog()
        batch_id = legacy_catalog.upsert_batch(legacy_instance, "SS100/2026-10-01")
        database = legacy_catalog.database_path(legacy_instance)
        connection = sqlite3.connect(database)
        connection.execute("ALTER TABLE batches DROP COLUMN run_type")
        connection.execute(
            "UPDATE catalog_metadata SET value='2' WHERE key='schema_version'"
        )
        connection.commit()
        connection.close()

        upgraded = BatchCatalog().get_batch(legacy_instance, batch_id)

        self.assertEqual("nightly", upgraded["run_type"])

    def test_queue_counts_follow_status_changes_without_list_aggregation(self):
        self.catalog.apply_queue_changes(
            self.instance,
            self.batch_id,
            [{"original_index": 0, "status": "completed"}],
        )

        batch = self.catalog.list_batches(self.instance)[0]

        self.assertEqual(0, batch["pending_count"])
        self.assertEqual(1, batch["completed_count"])
        self.assertEqual(1, batch["queue_total"])

    def test_document_edits_coalesce_into_one_durable_export(self):
        imported = self.catalog.import_document(
            self.instance,
            self.batch_id,
            "enriched",
            ["AccessionID", "ParsingQCPassed"],
            [{"AccessionID": "NP1", "ParsingQCPassed": ""}],
            "original-hash",
        )
        self.assertTrue(imported)
        first_version = self.catalog.replace_document(
            self.instance,
            self.batch_id,
            "enriched",
            ["AccessionID", "ParsingQCPassed"],
            [{"AccessionID": "NP2", "ParsingQCPassed": ""}],
        )
        latest_version = self.catalog.replace_document(
            self.instance,
            self.batch_id,
            "enriched",
            ["AccessionID", "ParsingQCPassed"],
            [{"AccessionID": "NP3", "ParsingQCPassed": "TRUE"}],
        )

        job = self.catalog.claim_export(self.instance, "2000-01-01T00:00:00Z")
        snapshot = self.catalog.export_snapshot(
            self.instance, self.batch_id, "enriched", latest_version
        )

        self.assertEqual(first_version + 1, latest_version)
        self.assertEqual(latest_version, job["desired_version"])
        self.assertEqual("NP3", snapshot[1][0]["AccessionID"])
        self.assertTrue(
            self.catalog.finish_export(
                self.instance, self.batch_id, "enriched", latest_version, "new-hash"
            )
        )
        state = self.catalog.document_state(self.instance, self.batch_id, "enriched")
        self.assertEqual("current", state["status"])
        self.assertEqual("new-hash", state["source_hash"])

    def test_partial_document_update_preserves_other_rows(self):
        self.catalog.import_document(
            self.instance,
            self.batch_id,
            "enriched",
            ["AccessionID", "ParsingQCPassed"],
            [
                {"AccessionID": "NP1", "ParsingQCPassed": ""},
                {"AccessionID": "NP2", "ParsingQCPassed": ""},
            ],
            "original-hash",
        )

        version = self.catalog.update_document_rows(
            self.instance,
            self.batch_id,
            "enriched",
            {1: {"ParsingQCPassed": "TRUE"}},
        )
        _, rows, state = self.catalog.load_document(
            self.instance, self.batch_id, "enriched"
        )

        self.assertEqual("", rows[0]["ParsingQCPassed"])
        self.assertEqual("TRUE", rows[1]["ParsingQCPassed"])
        self.assertEqual(version, state["desired_version"])

    def test_copath_reports_are_selected_by_accession(self):
        self.catalog.replace_copath_source(
            self.instance,
            "organ:Breast",
            "/clone/Breast/copath_data.csv",
            10,
            20,
            [
                {"accession_id": "NP1", "report": "one"},
                {"accession_id": "NP2", "report": "two"},
            ],
        )

        reports = self.catalog.copath_reports(self.instance, ["np2"])

        self.assertEqual({"np2": {"accession_id": "NP2", "report": "two"}}, reports)


class BatchCatalogMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.batches = self.root / "batches"
        self.state = self.root / "state"
        self.batch = self.batches / "SS100" / "batch-1"
        (self.batch / "label").mkdir(parents=True)
        (self.batch / "macro").mkdir()
        write_csv(
            self.batch / "enriched.csv",
            ["AccessionID", "ParsingQCPassed", "original_slide_path"],
            [{"AccessionID": "A12-123", "ParsingQCPassed": "TRUE", "original_slide_path": "one.svs"}],
        )
        write_csv(
            self.batch / "completed_stages.csv",
            ["QC", "Renamed"],
            [{"QC": "True", "Renamed": "False"}],
        )
        queue_name = migrate_batch_catalog.legacy_queue_name(
            "/data/inslide-batches", "SS100/batch-1"
        )
        self.queue = self.state / "instance" / "batch_queues" / queue_name
        write_csv(
            self.queue,
            migrate_batch_catalog.QUEUE_FIELDS,
            [{
                "original_index": "0", "status": "completed",
                "leased_by_id": "", "leased_at": "",
                "completed_by_id": "reviewer", "completed_at": "2026-08-28T12:00:00",
            }],
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_apply_imports_and_archives_verified_legacy_state(self):
        arguments = [
            "--batches-root", str(self.batches),
            "--state-root", str(self.state),
            "--apply",
        ]

        self.assertEqual(0, migrate_batch_catalog.main(arguments))

        database = self.state / "instance" / "batch_catalog.sqlite3"
        connection = sqlite3.connect(database)
        try:
            batch = connection.execute(
                "SELECT qc_complete,renamed_complete FROM batches"
            ).fetchone()
            queue = connection.execute(
                "SELECT status,completed_by_id FROM queue_items"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual((1, 0), batch)
        self.assertEqual(("completed", "reviewer"), queue)
        self.assertFalse((self.batch / "completed_stages.csv").exists())
        self.assertFalse(self.queue.exists())
        manifests = list(
            (self.state / "instance" / "legacy_batch_state_archive").glob("*/manifest.json")
        )
        self.assertEqual(1, len(manifests))

    def test_dry_run_does_not_write_or_archive(self):
        result = migrate_batch_catalog.main(
            ["--batches-root", str(self.batches), "--state-root", str(self.state)]
        )

        self.assertEqual(0, result)
        self.assertTrue((self.batch / "completed_stages.csv").exists())
        self.assertTrue(self.queue.exists())
        self.assertFalse((self.state / "instance" / "batch_catalog.sqlite3").exists())

    def test_malformed_stage_aborts_before_database_or_archive(self):
        write_csv(
            self.batch / "completed_stages.csv",
            ["QC", "Renamed"],
            [{"QC": "maybe", "Renamed": "False"}],
        )

        result = migrate_batch_catalog.main(
            [
                "--batches-root", str(self.batches),
                "--state-root", str(self.state),
                "--apply",
            ]
        )

        self.assertEqual(2, result)
        self.assertTrue((self.batch / "completed_stages.csv").exists())
        self.assertTrue(self.queue.exists())
        self.assertFalse((self.state / "instance" / "batch_catalog.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
