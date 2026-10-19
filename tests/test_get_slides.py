import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src" / "copath_utilities" / "get_slides.py"
)
sys.path.insert(0, str(SCRIPT_PATH.parent))
sys.modules.setdefault("pyodbc", mock.Mock())
SPEC = importlib.util.spec_from_file_location("get_slides", SCRIPT_PATH)
get_slides = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(get_slides)


class GetSlidesTests(unittest.TestCase):
    def test_main_executes_generated_query_and_writes_csv(self):
        connection = mock.Mock()
        results = mock.Mock()

        with tempfile.TemporaryDirectory() as temporary:
            input_path = Path(temporary) / "accessions.txt"
            output_path = Path(temporary) / "slides.csv"
            input_path.write_text("SP25-0001\n", encoding="utf-8")
            args = SimpleNamespace(
                input_file=str(input_path),
                column=None,
                output_file=str(output_path),
                batch_size=900,
            )

            with mock.patch.object(get_slides, "parse_args", return_value=args), mock.patch.object(
                get_slides, "prepared_connection_string", return_value="connection-string"
            ), mock.patch.object(
                get_slides.pyodbc, "connect", return_value=connection
            ) as connect, mock.patch.object(
                get_slides.pd, "read_sql", return_value=results
            ) as read_sql:
                self.assertEqual(0, get_slides.main())

        connect.assert_called_once_with("connection-string")
        query = read_sql.call_args.args[0]
        self.assertIn("SET NOCOUNT ON", query)
        self.assertIn("('SP25-0001', 'SP', 2025, 1)", query)
        read_sql.assert_called_once_with(query, connection)
        connection.close.assert_called_once_with()
        results.to_csv.assert_called_once_with(str(output_path), index=False)


if __name__ == "__main__":
    unittest.main()
