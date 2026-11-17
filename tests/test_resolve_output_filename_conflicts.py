import contextlib
import csv
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

from batch_catalog import BatchCatalog
import compare_inventory_paths
import renaming
import resolve_output_filename_conflicts as resolver
import report_output_filename_conflicts as report


class ResolveOutputFilenameConflictsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.instance = Path(self.temporary.name) / "instance"
        self.catalog = BatchCatalog()
        self.catalog_patch = mock.patch.object(resolver, "catalog", self.catalog)
        self.catalog_patch.start()

    def tearDown(self):
        self.catalog_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def row(date: str, section: int) -> dict[str, str]:
        row = {
            "AccessionID": "NP25-100",
            "Organ": "BRAIN",
            "PID": "AAAAAA",
            "AccessionDate": "20250101",
            "Timepoint": "XXXX",
            "Stain": "HE",
            "ImageType": "WSI",
            "SampAcqType": "RE",
            "BlockNumber": "A1",
            "SectionCount": f"{section:03d}",
            "OriginalPath": f"/scanner/{date}/slide-{section}.svs",
            "Approved": "True",
        }
        row["NewName"] = renaming.build_new_name(row)
        return row

    def add_batch(self, relative_path: str, rows: list[dict[str, str]]) -> str:
        public_id = self.catalog.upsert_batch(
            self.instance,
            relative_path,
            qc_complete=True,
            renamed_complete=True,
            slide_count=len(rows),
        )
        self.catalog.import_document(
            self.instance,
            public_id,
            "name_mapping",
            renaming.MAPPING_FIELDS,
            rows,
            "original-hash",
        )
        return public_id

    def test_dry_run_preserves_oldest_and_apply_renames_later_scans(self):
        old_id = self.add_batch(
            "SS100/2026-01-01",
            [self.row("2026-01-01", 0), self.row("2026-01-01", 1)],
        )
        new_id = self.add_batch(
            "SS100/2026-02-01",
            [
                self.row("2026-02-01", 0),
                self.row("2026-02-01", 1),
                self.row("2026-02-01", 2),
            ],
        )

        plan = resolver.plan_repairs(self.instance)

        self.assertEqual(2, plan.conflict_count)
        self.assertEqual(["002", "003", "004"], [
            document.rows[change.row_index]["SectionCount"]
            for change in plan.changes
            for document in plan.documents
            if document.public_id == change.public_id
        ])
        _, stored_new, new_state = self.catalog.load_document(
            self.instance, new_id, "name_mapping"
        )
        self.assertEqual(
            ["000", "001", "002"],
            [row["SectionCount"] for row in stored_new],
        )
        self.assertEqual(1, new_state["desired_version"])

        self.assertEqual(1, resolver.apply_repairs(self.instance, plan))

        _, stored_old, old_state = self.catalog.load_document(
            self.instance, old_id, "name_mapping"
        )
        _, stored_new, new_state = self.catalog.load_document(
            self.instance, new_id, "name_mapping"
        )
        self.assertEqual(["000", "001"], [row["SectionCount"] for row in stored_old])
        self.assertEqual(
            ["002", "003", "004"],
            [row["SectionCount"] for row in stored_new],
        )
        self.assertEqual("current", old_state["status"])
        self.assertEqual("pending", new_state["status"])
        self.assertEqual([], resolver.plan_repairs(self.instance).changes)

    def test_nonfinalized_batches_are_ignored(self):
        self.add_batch("SS100/2026-01-01", [self.row("2026-01-01", 0)])
        public_id = self.catalog.upsert_batch(
            self.instance,
            "SS100/2026-02-01",
            qc_complete=True,
            renamed_complete=False,
            slide_count=1,
        )
        self.catalog.import_document(
            self.instance,
            public_id,
            "name_mapping",
            renaming.MAPPING_FIELDS,
            [self.row("2026-02-01", 0)],
            "original-hash",
        )

        self.assertEqual([], resolver.plan_repairs(self.instance).changes)

    def test_inventory_audit_marks_duplicates_and_missing_sources(self):
        old = self.row("2026-01-01", 0)
        new_duplicate = self.row("2026-02-01", 0)
        missing = self.row("2026-02-01", 1)
        old_id = self.add_batch("SS200/2026-01-01", [old])
        new_id = self.add_batch(
            "SS100/2026-02-01", [new_duplicate, missing]
        )
        inventory_root = Path(self.temporary.name) / "inventories"
        renaming.atomic_write(
            inventory_root / "SS100_inventory.csv",
            ["full_path", "size_in_bytes"],
            [
                {
                    "full_path": new_duplicate["OriginalPath"],
                    "size_in_bytes": "100",
                },
            ],
        )
        renaming.atomic_write(
            inventory_root / "SS200_inventory.csv",
            ["full_path", "size_in_bytes"],
            [{"full_path": old["OriginalPath"], "size_in_bytes": "100"}],
        )

        plan = resolver.plan_inventory_repairs(self.instance, inventory_root)
        output = io.StringIO()
        with mock.patch.object(resolver, "plan_inventory_repairs", return_value=plan):
            with contextlib.redirect_stdout(output):
                self.assertEqual(0, resolver.main([]))
        self.assertIn("2 inventory issue(s):\n  1 duplicates\n  1 missing sources", output.getvalue())
        self.assertIn("3 row(s) need catalog updates:\n  1 duplicates\n  1 missing sources\n  0 renamed slides\n  1 metadata only", output.getvalue())
        self.assertNotIn("SS100/", output.getvalue())

        planned_old = next(
            document for document in plan.documents if document.public_id == old_id
        ).rows[0]
        planned_new = next(
            document for document in plan.documents if document.public_id == new_id
        ).rows
        self.assertEqual("active", planned_old["Disposition"])
        self.assertEqual("duplicate", planned_new[0]["Disposition"])
        self.assertEqual(old_id, planned_new[0]["DuplicateOfBatch"])
        self.assertEqual(old["OriginalPath"], planned_new[0]["DuplicateOfPath"])
        self.assertEqual(old["NewName"], planned_new[0]["ConflictName"])
        self.assertEqual("", planned_new[0]["SectionCount"])
        self.assertEqual("missing_source", planned_new[1]["Disposition"])
        self.assertEqual(missing["NewName"], planned_new[1]["NewName"])
        _, stored_new, _ = self.catalog.load_document(
            self.instance, new_id, "name_mapping"
        )
        self.assertEqual("", stored_new[0]["Disposition"])

        self.assertEqual(2, resolver.apply_repairs(self.instance, plan))

        _, stored_new, _ = self.catalog.load_document(
            self.instance, new_id, "name_mapping"
        )
        self.assertEqual(
            ["duplicate", "missing_source"],
            [row["Disposition"] for row in stored_new],
        )
        audit_dir = self.instance / "catalog_audits"
        self.assertEqual(1, len(list(audit_dir.glob("batch_catalog_*.sqlite3"))))
        self.assertEqual(1, len(list(audit_dir.glob("slide_repairs_*.csv"))))

    def test_compare_inventory_paths_shows_matching_basenames(self):
        self.add_batch("SS100/2026-01-01", [
            self.row("2026-01-01", 0), self.row("2026-01-01", 1)
        ])
        inventory_root = Path(self.temporary.name) / "inventories"
        renaming.atomic_write(
            inventory_root / "SS100_inventory.csv",
            ["full_path", "size_in_bytes"],
            [{"full_path": "/other/slide-0.svs", "size_in_bytes": "100"}],
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            compare_inventory_paths.compare(self.instance, inventory_root)
        self.assertIn("SS100: 2 missing paths", output.getvalue())
        self.assertIn("1 same basename", output.getvalue())
        self.assertIn("1 no basename match", output.getvalue())
        self.assertIn("inventory: /other/slide-0.svs", output.getvalue())

    def test_scanner_directory_case_does_not_mark_source_missing(self):
        row = self.row("2026-01-01", 0)
        row["OriginalPath"] = "/data/gt450-images/SS100/2026-01-01/slide-0.svs"
        self.add_batch("SS100/2026-01-01", [row])
        inventory_root = Path(self.temporary.name) / "inventories"
        renaming.atomic_write(
            inventory_root / "SS100_inventory.csv",
            ["full_path", "size_in_bytes"],
            [{"full_path": "/data/gt450-images/ss100/2026-01-01/slide-0.svs",
              "size_in_bytes": "100"}],
        )
        with mock.patch.dict(os.environ, {"GT450_IMAGES_CONTAINER_ROOT": "/data/gt450-images"}):
            plan = resolver.plan_inventory_repairs(self.instance, inventory_root)
        self.assertEqual(0, plan.conflict_count)
        self.assertEqual("100", plan.documents[0].rows[0]["SourceSize"])
        self.assertEqual("active", plan.documents[0].rows[0]["Disposition"])

    def test_rename_report_identifies_oldest_conflicting_row(self):
        old = self.row("2026-01-01", 0)
        new = self.row("2026-02-01", 0)
        self.add_batch("SS100/2026-01-01", [old])
        self.add_batch("SS100/2026-02-01", [new])
        inventory_root = Path(self.temporary.name) / "inventories"
        renaming.atomic_write(
            inventory_root / "SS100_inventory.csv",
            ["full_path", "size_in_bytes"],
            [
                {"full_path": old["OriginalPath"], "size_in_bytes": "100"},
                {"full_path": new["OriginalPath"], "size_in_bytes": "200"},
            ],
        )
        output = io.StringIO()
        report.write_report(self.instance, inventory_root, output)
        rows = list(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual(1, len(rows))
        self.assertEqual("SS100/2026-02-01", rows[0]["changed_batch"])
        self.assertEqual("1", rows[0]["changed_row"])
        self.assertEqual(old["NewName"], rows[0]["current_name"])
        self.assertEqual("SS100/2026-01-01", rows[0]["conflicting_batch"])
        self.assertEqual("1", rows[0]["conflicting_row"])
        self.assertEqual(old["NewName"], rows[0]["conflicting_name"])
        self.assertEqual(old["NewName"].replace("A1000", "A1001"), rows[0]["proposed_new_name"])

    def test_rename_report_leaves_nonconflict_columns_blank(self):
        row = self.row("2026-01-01", 0)
        row["NewName"] = row["NewName"].replace("A1000", "A1005")
        self.add_batch("SS100/2026-01-01", [row])
        inventory_root = Path(self.temporary.name) / "inventories"
        renaming.atomic_write(
            inventory_root / "SS100_inventory.csv",
            ["full_path", "size_in_bytes"],
            [{"full_path": row["OriginalPath"], "size_in_bytes": "100"}],
        )
        output = io.StringIO()
        report.write_report(self.instance, inventory_root, output)
        rows = list(csv.DictReader(io.StringIO(output.getvalue())))
        self.assertEqual(1, len(rows))
        self.assertEqual("", rows[0]["conflicting_batch"])
        self.assertEqual("", rows[0]["conflicting_row"])
        self.assertEqual("", rows[0]["conflicting_name"])

    def test_exhausted_section_numbers_fail_before_writes(self):
        old_id = self.add_batch(
            "SS100/2026-01-01",
            [self.row("2026-01-01", section) for section in range(1000)],
        )
        self.add_batch("SS100/2026-02-01", [self.row("2026-02-01", 0)])

        with self.assertRaisesRegex(ValueError, "no unused SectionCount"):
            resolver.plan_repairs(self.instance)

        _, _, state = self.catalog.load_document(
            self.instance, old_id, "name_mapping"
        )
        self.assertEqual(1, state["desired_version"])


if __name__ == "__main__":
    unittest.main()
