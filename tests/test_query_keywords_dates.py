import importlib.util
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src" / "copath_utilities" / "query_keywords_dates.py"
)
sys.path.insert(0, str(SCRIPT_PATH.parent))
sys.modules.setdefault("pyodbc", mock.Mock())
SPEC = importlib.util.spec_from_file_location("query_keywords_dates", SCRIPT_PATH)
query_keywords_dates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(query_keywords_dates)

build_query = query_keywords_dates.build_query
parse_keyword_expression = query_keywords_dates.parse_keyword_expression


class QueryKeywordsDatesTests(unittest.TestCase):
    def test_main_executes_generated_query_and_writes_csv(self):
        connection = mock.Mock()
        results = mock.Mock()
        output_path = Path(tempfile.gettempdir()) / "keyword-results.csv"
        args = SimpleNamespace(
            keywords="idh",
            after=None,
            before=None,
            search_in=None,
            output_file=str(output_path),
        )

        with mock.patch.object(
            query_keywords_dates, "parse_args", return_value=args
        ), mock.patch.object(
            query_keywords_dates,
            "prepared_connection_string",
            return_value="connection-string",
        ), mock.patch.object(
            query_keywords_dates.pyodbc, "connect", return_value=connection
        ) as connect, mock.patch.object(
            query_keywords_dates.pd, "read_sql", return_value=results
        ) as read_sql:
            self.assertEqual(0, query_keywords_dates.main())

        connect.assert_called_once_with("connection-string")
        query = read_sql.call_args.args[0]
        self.assertIn("w.word IN (N'idh')", query)
        read_sql.assert_called_once_with(query, connection)
        connection.close.assert_called_once_with()
        results.to_csv.assert_called_once_with(str(output_path), index=False)

    def test_keyword_query_uses_only_word_index_and_specimen_table(self):
        query = build_query("idh-wt", None, None, None)

        self.assertEqual(query.count("FROM c_textword w"), 1)
        self.assertIn("JOIN c_specimen s", query)
        self.assertNotIn("c_spec_text", query)
        self.assertNotIn("CREATE TABLE", query)
        self.assertNotIn("STRING_AGG", query)

    def test_words_in_one_term_must_share_paragraph_and_texttype(self):
        query = build_query("anaplastic neoplasm", None, None, None)

        self.assertIn("GROUP BY w.specimen_id, w.texttype_id, w.para", query)
        self.assertIn("w.word = N'anaplastic'", query)
        self.assertIn("w.word = N'neoplasm'", query)
        self.assertIn("MAX(term_1) = 1", query)

    def test_or_and_expression_is_composed_from_indexed_terms(self):
        groups = parse_keyword_expression("alpha&beta|gamma")
        self.assertEqual(groups, [["alpha", "beta"], ["gamma"]])

        query = build_query("alpha&beta|gamma", None, None, None)
        self.assertEqual(query.count("FROM c_textword w"), 1)
        self.assertIn("w.word IN (N'alpha', N'beta', N'gamma')", query)
        self.assertIn("w.word = N'alpha'", query)
        self.assertIn("AND MAX(CASE WHEN w.word = N'beta'", query)
        self.assertIn("OR (", query)

    def test_search_target_limits_texttype(self):
        query = build_query("idh", None, None, ["addendum_diagnosis"])

        self.assertIn("w.texttype_id IN (N'$adddx')", query)
        self.assertNotIn("N'$final'", query)

    def test_dates_are_sargable_and_before_is_inclusive(self):
        query = build_query(
            "idh",
            date(2016, 8, 25),
            date(2026, 8, 25),
            None,
        )

        self.assertIn("s.accession_date >= '20160825'", query)
        self.assertIn("s.accession_date <  '20260826'", query)
        self.assertIn("JOIN eligible_specimens s", query)
        self.assertNotIn("CAST(s.accession_date", query)

    def test_date_only_query_does_not_reference_word_index(self):
        query = build_query(None, date(2020, 1, 1), None, None)

        self.assertIn("FROM c_specimen s", query)
        self.assertNotIn("c_textword", query)
        self.assertNotIn("WITH", query)

    def test_idh_expression_searches_all_words_in_one_pass(self):
        query = build_query(
            "idh-wildtype|idh-wt|anaplastic neoplasm",
            date(2016, 8, 25),
            date(2026, 8, 25),
            None,
        )

        self.assertEqual(query.count("FROM c_textword w"), 1)
        self.assertIn("w.word = N'idh'", query)
        self.assertIn("w.word = N'wildtype'", query)
        self.assertIn("w.word = N'wt'", query)
        self.assertIn("w.word = N'anaplastic'", query)
        self.assertIn("w.word = N'neoplasm'", query)
        self.assertEqual(query.count("MAX(term_"), 3)


if __name__ == "__main__":
    unittest.main()
