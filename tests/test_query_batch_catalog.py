import csv
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from batch_catalog import BatchCatalog
import query_batch_catalog as query


class QueryBatchCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.instance = Path(self.temp.name) / "instance"
        catalog = BatchCatalog()
        batch_id = catalog.upsert_batch(self.instance, "SS100/batch-1")
        catalog.import_document(
            self.instance, batch_id, "name_mapping",
            ["AccessionID", "PID", "OriginalPath"],
            [{"AccessionID": "NP25-100", "PID": "AAAAAA",
              "OriginalPath": "one.svs", "NameEdited": "True"}],
            "hash",
        )

    def run_query(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            status = query.main(["--instance-dir", str(self.instance), *args])
        self.assertEqual(0, status)
        return output.getvalue()

    def test_lists_tables_and_document_columns(self):
        self.assertIn("batch_documents", self.run_query("--list-tables"))
        columns = self.run_query("--show-columns").splitlines()
        self.assertIn("PID", columns)
        self.assertIn("NameEdited", columns)
        self.assertIn("BatchPath", columns)
        self.assertEqual(Path("catalog_query.csv"), query.parser().parse_args(["--output"]).output)

    def test_searches_any_column_and_selects_output_columns(self):
        output = self.run_query(
            "--find", "aaaaaa", "--columns", "BatchPath,PID,NameEdited"
        )
        self.assertEqual(
            [{"BatchPath": "SS100/batch-1", "PID": "AAAAAA", "NameEdited": "True"}],
            list(csv.DictReader(io.StringIO(output))),
        )
        self.assertEqual(
            [], list(csv.DictReader(io.StringIO(self.run_query(
                "--find", "AAAAAA", "--search-column", "AccessionID"
            )))),
        )

    def test_queries_physical_table_and_writes_file_without_overwrite(self):
        output_path = Path(self.temp.name) / "results.csv"
        self.run_query(
            "--table", "batches", "--find", "SS100",
            "--columns", "relative_path,public_id", "--output", str(output_path),
        )
        with output_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual("SS100/batch-1", rows[0]["relative_path"])
        self.assertEqual(1, len(rows))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(2, query.main([
                "--instance-dir", str(self.instance), "--output", str(output_path)
            ]))

    def test_missing_catalog_is_not_created(self):
        missing = Path(self.temp.name) / "missing"
        with redirect_stderr(io.StringIO()):
            self.assertEqual(2, query.main(["--instance-dir", str(missing)]))
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
