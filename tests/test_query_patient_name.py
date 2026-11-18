import csv
import importlib.util
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src" / "copath_utilities" / "query_patient_name.py"
)
sys.path.insert(0, str(SCRIPT_PATH.parent))
sys.modules.setdefault("pyodbc", mock.Mock(Error=Exception))
SPEC = importlib.util.spec_from_file_location("query_patient_name", SCRIPT_PATH)
query_patient_name = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(query_patient_name)


class QueryPatientNameTests(unittest.TestCase):
    def test_name_is_bound_and_results_are_written(self):
        cursor = mock.Mock()
        cursor.description = [("accession_id",), ("last_name",)]
        cursor.__iter__ = mock.Mock(return_value=iter([("SP25-1", "O'Neil")]))
        connection = mock.Mock()
        connection.cursor.return_value = cursor

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.csv"
            with mock.patch.object(
                query_patient_name, "prepared_connection_string", return_value="test"
            ), mock.patch.object(
                query_patient_name.pyodbc, "connect", return_value=connection
            ):
                result = query_patient_name.main([
                    "--last-name", "O'Neil", "--first-name", "Jane",
                    "--output-file", str(output),
                ])
            with output.open(newline="", encoding="utf-8") as file:
                rows = list(csv.reader(file))
            permissions = stat.S_IMODE(output.stat().st_mode)

        self.assertEqual(0, result)
        self.assertEqual([["accession_id", "last_name"], ["SP25-1", "O'Neil"]], rows)
        self.assertEqual(0o600, permissions)
        sql, last_name, first_name = cursor.execute.call_args.args
        self.assertIn("pd.lastname = ?", sql)
        self.assertIn("pd.firstname = ?", sql)
        self.assertNotIn("O'Neil", sql)
        self.assertEqual(("O'Neil", "Jane"), (last_name, first_name))
        connection.close.assert_called_once_with()

    def test_last_name_only(self):
        cursor = mock.Mock()
        cursor.description = [("accession_id",)]
        cursor.__iter__ = mock.Mock(return_value=iter([]))
        connection = mock.Mock()
        connection.cursor.return_value = cursor

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            query_patient_name, "prepared_connection_string", return_value="test"
        ), mock.patch.object(
            query_patient_name.pyodbc, "connect", return_value=connection
        ):
            self.assertEqual(0, query_patient_name.main([
                "--last-name", "Smith", "-o", str(Path(directory) / "results.csv"),
            ]))

        self.assertNotIn("pd.firstname = ?", cursor.execute.call_args.args[0])
        self.assertEqual(("Smith",), cursor.execute.call_args.args[1:])

    def test_failed_query_preserves_existing_output(self):
        cursor = mock.Mock()
        cursor.execute.side_effect = query_patient_name.pyodbc.Error("query failed")
        connection = mock.Mock()
        connection.cursor.return_value = cursor

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.csv"
            output.write_text("previous results\n", encoding="utf-8")
            with mock.patch.object(
                query_patient_name, "prepared_connection_string", return_value="test"
            ), mock.patch.object(
                query_patient_name.pyodbc, "connect", return_value=connection
            ):
                self.assertEqual(1, query_patient_name.main([
                    "--last-name", "Smith", "-o", str(output),
                ]))
            self.assertEqual("previous results\n", output.read_text(encoding="utf-8"))
            self.assertEqual([output], list(Path(directory).iterdir()))


if __name__ == "__main__":
    unittest.main()
