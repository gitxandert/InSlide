import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

from batch_catalog import BatchCatalog
import rebuild_pid_names
import renaming


class RebuildPidNamesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.instance = self.root / "instance"
        self.batches = self.root / "batches"
        self.catalog = BatchCatalog()
        self.catalog_patch = mock.patch.object(
            rebuild_pid_names, "catalog", self.catalog
        )
        self.catalog_patch.start()

    def tearDown(self):
        self.catalog_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def row(path: str, organ: str, pid: str) -> dict[str, str]:
        row = {
            "AccessionID": "NP25-100",
            "Organ": organ,
            "PID": pid,
            "AccessionDate": "20250101",
            "Timepoint": "XXXX",
            "Stain": "HE",
            "ImageType": "WSI",
            "SampAcqType": "RE",
            "BlockNumber": "A1",
            "SectionCount": "000",
            "OriginalPath": path,
            "Approved": "True",
        }
        row["NewName"] = renaming.build_new_name(row)
        return row

    def add_batch(self, relative_path: str, old_pid: str, corrected_pid: str) -> str:
        batch_id = self.catalog.upsert_batch(
            self.instance, relative_path, slide_count=1
        )
        self.catalog.update_stages(
            self.instance, batch_id, qc_complete=True, renamed_complete=True
        )
        stored = self.row(f"{relative_path}.svs", "BRAIN", old_pid)
        self.catalog.import_document(
            self.instance,
            batch_id,
            "name_mapping",
            renaming.MAPPING_FIELDS,
            [stored],
            "original-hash",
        )
        corrected = self.row(stored["OriginalPath"], "BREAST", corrected_pid)
        renaming.atomic_write(
            self.batches / relative_path / "name_mapping.csv",
            renaming.MAPPING_FIELDS,
            [corrected],
        )
        return batch_id

    def test_rebuilds_names_from_consistent_corrected_csvs(self):
        first = self.add_batch("SS100/batch-1", "AAAAAA", "AAAAAC")
        second = self.add_batch("SS200/batch-2", "AAAAAB", "AAAAAC")

        dry_run = rebuild_pid_names.rebuild_pid_names(
            self.instance, self.batches, "np25-100"
        )
        _, unchanged, _ = self.catalog.load_document(
            self.instance, first, "name_mapping"
        )
        self.assertEqual(2, len(dry_run))
        self.assertEqual("AAAAAA", unchanged[0]["PID"])

        rebuild_pid_names.rebuild_pid_names(
            self.instance, self.batches, "NP25-100", apply=True
        )

        for batch_id in (first, second):
            _, rows, state = self.catalog.load_document(
                self.instance, batch_id, "name_mapping"
            )
            self.assertEqual("BREAST", rows[0]["Organ"])
            self.assertEqual("AAAAAC", rows[0]["PID"])
            self.assertTrue(rows[0]["NewName"].startswith("BREAST_AAAAAC_"))
            self.assertEqual("pending", state["status"])

    def test_rejects_csvs_that_still_disagree(self):
        first = self.add_batch("SS100/batch-1", "AAAAAA", "AAAAAC")
        self.add_batch("SS200/batch-2", "AAAAAB", "AAAAAD")

        with self.assertRaisesRegex(
            ValueError, r"still disagree.*SS100/batch-1.*SS200/batch-2"
        ):
            rebuild_pid_names.rebuild_pid_names(
                self.instance, self.batches, "NP25-100", apply=True
            )

        _, rows, state = self.catalog.load_document(
            self.instance, first, "name_mapping"
        )
        self.assertEqual("AAAAAA", rows[0]["PID"])
        self.assertEqual("current", state["status"])

    def test_restores_deleted_row_from_catalog(self):
        self.add_batch("SS100/batch-1", "AAAAAA", "AAAAAC")
        mapping = self.batches / "SS100/batch-1/name_mapping.csv"
        renaming.atomic_write(mapping, renaming.MAPPING_FIELDS, [])

        restored = rebuild_pid_names.restore_missing_rows(
            self.instance, self.batches, "NP25-100"
        )

        _, rows = renaming.read_csv(mapping)
        self.assertEqual([(str(mapping), 1)], restored)
        self.assertEqual(1, len(rows))
        self.assertEqual("NP25-100", rows[0]["AccessionID"])
        self.assertEqual("AAAAAA", rows[0]["PID"])


if __name__ == "__main__":
    unittest.main()
