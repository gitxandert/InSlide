import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from batch_catalog import BatchCatalog
import remove_name_edited_column as cleanup
import renaming


class RemoveNameEditedColumnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.batches = self.root / "batches"
        self.mapping = self.batches / "SS100" / "batch-1" / "name_mapping.csv"
        self.instance = self.root / "instance"
        self.catalog = BatchCatalog()
        self.mapping.parent.mkdir(parents=True)
        fields = ["OriginalPath", "PID", "NameEdited"]
        rows = [{"OriginalPath": "one.svs", "PID": "AAAAAA", "NameEdited": "True"}]
        renaming.atomic_write(self.mapping, fields, rows)
        self.batch_id = self.catalog.upsert_batch(self.instance, "SS100/batch-1")
        self.catalog.import_document(
            self.instance, self.batch_id, "name_mapping", fields, rows,
            hashlib.sha256(self.mapping.read_bytes()).hexdigest(),
        )

    def test_catalog_pid_wins_and_export_is_queued(self):
        fields, rows = renaming.read_csv(self.mapping)
        renaming.atomic_write(self.mapping, fields, [dict(rows[0], PID="AAAAAB")])
        changes = cleanup.plan(self.batches, self.instance)
        self.assertEqual(1, len(changes))
        backup = cleanup.apply(changes, self.instance)
        csv_fields, csv_rows = renaming.read_csv(self.mapping)
        db_fields, db_rows, state = self.catalog.load_document(
            self.instance, self.batch_id, "name_mapping"
        )
        self.assertIn("NameEdited", csv_fields)
        self.assertEqual("AAAAAB", csv_rows[0]["PID"])
        self.assertEqual("AAAAAB", renaming.read_csv(
            backup / "SS100" / "batch-1" / "name_mapping.csv"
        )[1][0]["PID"])
        self.assertNotIn("NameEdited", db_fields)
        self.assertEqual("AAAAAA", db_rows[0]["PID"])
        self.assertEqual("True", db_rows[0]["NameEdited"])
        self.assertEqual("pending", state["status"])
        snapshot = self.catalog.export_snapshot(
            self.instance, self.batch_id, "name_mapping", state["desired_version"]
        )
        self.assertEqual("AAAAAA", snapshot[1][0]["PID"])
        self.assertNotIn("NameEdited", snapshot[0])

    def test_queues_missing_export_from_catalog(self):
        self.mapping.unlink()
        changes = cleanup.plan(self.batches, self.instance)
        self.assertEqual(1, len(changes))
        cleanup.apply(changes, self.instance)
        self.assertFalse(self.mapping.exists())
        state = self.catalog.document_state(self.instance, self.batch_id, "name_mapping")
        self.assertEqual("pending", state["status"])

    def test_refuses_catalog_version_change_after_plan(self):
        changes = cleanup.plan(self.batches, self.instance)
        fields, rows, _ = self.catalog.load_document(
            self.instance, self.batch_id, "name_mapping"
        )
        self.catalog.replace_document(
            self.instance, self.batch_id, "name_mapping", fields, rows
        )
        with self.assertRaisesRegex(RuntimeError, "catalog changed"):
            cleanup.apply(changes, self.instance)

    def test_removes_column_even_when_stored_hash_matches(self):
        fields, rows, _ = self.catalog.load_document(
            self.instance, self.batch_id, "name_mapping"
        )
        self.catalog.adopt_document(
            self.instance, self.batch_id, "name_mapping",
            [field for field in fields if field != "NameEdited"], rows,
            hashlib.sha256(self.mapping.read_bytes()).hexdigest(),
        )
        changes = cleanup.plan(self.batches, self.instance)
        self.assertEqual(1, len(changes))
        cleanup.apply(changes, self.instance)
        self.assertNotIn(
            "NameEdited",
            self.catalog.load_document(self.instance, self.batch_id, "name_mapping")[0],
        )

    def test_reconciles_conflicted_export_from_catalog(self):
        self.catalog.mark_document_conflict(
            self.instance, self.batch_id, "name_mapping", "changed outside InSlide"
        )
        changes = cleanup.plan(self.batches, self.instance)
        cleanup.apply(changes, self.instance)
        state = self.catalog.document_state(self.instance, self.batch_id, "name_mapping")
        self.assertEqual("pending", state["status"])

    def test_recovers_pending_export_after_failed_direct_write(self):
        fields, rows, _ = self.catalog.load_document(
            self.instance, self.batch_id, "name_mapping"
        )
        self.catalog.replace_document(
            self.instance, self.batch_id, "name_mapping",
            [field for field in fields if field != "NameEdited"], rows,
        )
        changes = cleanup.plan(self.batches, self.instance)
        self.assertEqual(1, len(changes))
        cleanup.apply(changes, self.instance)
        state = self.catalog.document_state(self.instance, self.batch_id, "name_mapping")
        self.assertEqual("pending", state["status"])

    def test_refuses_orphan_csv(self):
        orphan = self.batches / "SS200" / "batch-2" / "name_mapping.csv"
        renaming.atomic_write(orphan, ["PID"], [{"PID": "AAAAAB"}])
        with self.assertRaisesRegex(ValueError, "no matching catalog document"):
            cleanup.plan(self.batches, self.instance)

    def test_requires_catalog(self):
        self.catalog.database_path(self.instance).unlink()
        with self.assertRaisesRegex(ValueError, "catalog not found"):
            cleanup.plan(self.batches, self.instance)


if __name__ == "__main__":
    unittest.main()
