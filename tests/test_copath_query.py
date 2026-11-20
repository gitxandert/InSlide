import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src" / "copath_utilities" / "query_copath_db.py"
)
sys.path.insert(0, str(SCRIPT_PATH.parent))
sys.modules.setdefault("pyodbc", mock.Mock(Error=Exception))
sys.modules.setdefault("striprtf", mock.Mock())
sys.modules.setdefault("striprtf.striprtf", mock.Mock(rtf_to_text=lambda value: value))
SPEC = importlib.util.spec_from_file_location("query_copath_db", SCRIPT_PATH)
query_copath_db = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(query_copath_db)


class CoPathArbitraryAccessionTests(unittest.TestCase):
    def test_arbitrary_accession_is_preserved_and_deduplicated_case_insensitively(self):
        valid, invalid = query_copath_db.split_valid_invalid_accessions(
            [" Custom / Id_7 ", "custom / id_7", "Other accession"]
        )

        self.assertEqual(
            [{"accession_id": "Custom / Id_7"}, {"accession_id": "Other accession"}],
            valid,
        )
        self.assertEqual([], invalid)

    def test_accession_sql_matches_formatted_value_and_escapes_quotes(self):
        statement = query_copath_db.format_accession_insert_statement(
            [{"accession_id": "Patient's custom ID"}]
        )
        query = query_copath_db.format_query(statement, "accession")

        self.assertIn("N'Patient''s custom ID'", query)
        self.assertIn("i.accession_id = s.specnum_formatted", query)
        self.assertNotIn("i.numwheel_id", query)


if __name__ == "__main__":
    unittest.main()
