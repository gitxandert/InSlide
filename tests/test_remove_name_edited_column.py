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
        renaming.atomic_write(self.mapping, ["OriginalPath", "NameEdited"], [
            {"OriginalPath": "one.svs", "NameEdited": "True"}
        ])

    def test_removes_csv_column_and_preserves_database_flag(self):
        batch_id = self.catalog.upsert_batch(self.instance, "SS100/batch-1")
        fields, rows = renaming.read_csv(self.mapping)
        self.catalog.import_document(
            self.instance, batch_id, "name_mapping", fields, rows,
            hashlib.sha256(self.mapping.read_bytes()).hexdigest(),
        )
        changes = cleanup.plan(self.batches, self.instance)
        self.assertEqual(1, len(changes))
        backup = cleanup.apply(changes, self.instance)
        csv_fields, _ = renaming.read_csv(self.mapping)
        db_fields, db_rows, state = self.catalog.load_document(
            self.instance, batch_id, "name_mapping"
        )
        self.assertEqual(["OriginalPath"], csv_fields)
        self.assertEqual(csv_fields, db_fields)
        self.assertEqual("True", db_rows[0]["NameEdited"])
        self.assertEqual("current", state["status"])
        self.assertEqual([], cleanup.plan(self.batches, self.instance))
        self.assertEqual(fields, renaming.read_csv(
            backup / "SS100" / "batch-1" / "name_mapping.csv"
        )[0])

    def test_refuses_to_drop_true_flag_without_catalog(self):
        with self.assertRaisesRegex(ValueError, "need a catalog"):
            cleanup.plan(self.batches, self.instance)

    def test_cleans_legacy_csv_without_catalog_when_flags_are_false(self):
        renaming.atomic_write(self.mapping, ["OriginalPath", "NameEdited"], [
            {"OriginalPath": "one.svs", "NameEdited": "False"}
        ])
        changes = cleanup.plan(self.batches, self.instance)
        cleanup.apply(changes, self.instance)
        fields, _ = renaming.read_csv(self.mapping)
        self.assertEqual(["OriginalPath"], fields)

    def test_refuses_catalog_csv_mismatch(self):
        batch_id = self.catalog.upsert_batch(self.instance, "SS100/batch-1")
        fields, rows = renaming.read_csv(self.mapping)
        self.catalog.import_document(
            self.instance, batch_id, "name_mapping", fields, rows, "wrong-hash"
        )
        with self.assertRaisesRegex(ValueError, "catalog and CSV differ"):
            cleanup.plan(self.batches, self.instance)


if __name__ == "__main__":
    unittest.main()
