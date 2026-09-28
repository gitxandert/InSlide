import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

import audit_sdl_type_conflicts


class SDLTypeAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "sdl.xlsx"

    def tearDown(self):
        self.temporary.cleanup()

    def write_workbook(self, types):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "general"
        worksheet.append(["Accession ID", "Type", "Scanner", "Date Loaded"])
        for slide_type in types:
            worksheet.append(
                ["NP25-100", slide_type, "RSCH1 (SS12797)", "2026-07-20"]
            )
        workbook.save(self.path)
        workbook.close()

    def run_audit(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = audit_sdl_type_conflicts.main(["--workbook", str(self.path)])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_clean_workbook_returns_zero(self):
        self.write_workbook(["PROSP", "prosp"])
        result, stdout, stderr = self.run_audit()
        self.assertEqual(0, result)
        self.assertIn("No contrasting", stdout)
        self.assertEqual("", stderr)

    def test_conflict_reports_rows_and_returns_one(self):
        self.write_workbook(["PROSP", "SEMINOMA"])
        result, stdout, stderr = self.run_audit()
        self.assertEqual(1, result)
        self.assertIn("row 2 (PROSP)", stdout)
        self.assertIn("row 3 (SEMINOMA)", stdout)
        self.assertEqual("", stderr)

    def test_schema_error_returns_two(self):
        workbook = Workbook()
        workbook.active.title = "general"
        workbook.active.append(["Accession ID"])
        workbook.save(self.path)
        workbook.close()
        result, stdout, stderr = self.run_audit()
        self.assertEqual(2, result)
        self.assertEqual("", stdout)
        self.assertIn("Could not audit", stderr)


if __name__ == "__main__":
    unittest.main()
