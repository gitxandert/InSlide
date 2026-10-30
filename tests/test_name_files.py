import importlib.util
import re
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "src" / "3_name-files.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("name_files", SCRIPT_PATH)
name_files = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(name_files)


class NameFilesAccessionTests(unittest.TestCase):
    def test_normalizes_supported_accession_variants(self):
        self.assertEqual(name_files.normalize_accession_id("np 22-950"), "NP22-950")
        self.assertEqual(name_files.normalize_accession_id("SP25/0001"), "SP25-0001")
        self.assertEqual(name_files.normalize_accession_id("A12 123"), "A12-123")

    def test_semicolon_filename_uses_first_field_without_format_validation(self):
        stain_pattern, stain_lookup = name_files.build_stain_normalizer(
            name_files.STAIN_NAME_CORRECTIONS
        )
        accession_pattern = re.compile(r"\b[A-Za-z]{1,3}\d{2}[ /-]\d+\b", re.IGNORECASE)
        base_row = {
            "label_text": "H&E A1",
            "macro_text": "",
        }

        valid = name_files.process_csv_row(
            {**base_row, "original_slide_path": "np 22-950;slide.svs"},
            accession_pattern,
            stain_pattern,
            stain_lookup,
        )
        arbitrary = name_files.process_csv_row(
            {**base_row, "original_slide_path": "BAD;slide.svs"},
            accession_pattern,
            stain_pattern,
            stain_lookup,
        )

        self.assertEqual(valid[name_files.COL_ACCESSION_ID], "np 22-950")
        self.assertTrue(valid[name_files.COL_EXTRACTION_SUCCESSFUL])
        self.assertEqual(arbitrary[name_files.COL_ACCESSION_ID], "BAD")
        self.assertTrue(arbitrary[name_files.COL_EXTRACTION_SUCCESSFUL])

    def test_default_pattern_accepts_documented_spaced_accession(self):
        match = re.compile(
            name_files.DEFAULT_ACCESSION_PATTERN, re.IGNORECASE
        ).search("case NP 22-950")

        self.assertIsNotNone(match)
        self.assertEqual(name_files.normalize_accession_id(match.group(0)), "NP22-950")


if __name__ == "__main__":
    unittest.main()
