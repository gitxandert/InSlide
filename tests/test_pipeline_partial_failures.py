import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))


def load_stage(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SRC_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ocr = load_stage("pipeline_ocr", "2_run_dual_ocr.py")
name_files = load_stage("pipeline_name_files", "3_name-files.py")


class PipelinePartialFailureTests(unittest.TestCase):
    def test_empty_mapping_still_produces_ocr_and_enriched_csvs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mapping = root / "slide_mapping.csv"
            ocr_csv = root / "ocr.csv"
            enriched_csv = root / "enriched.csv"
            with mapping.open("w", newline="", encoding="utf-8") as handle:
                csv.DictWriter(
                    handle,
                    fieldnames=[
                        "macro_path",
                        "label_path",
                        "thumbnail_path",
                        "original_slide_path",
                    ],
                ).writeheader()

            with mock.patch.object(ocr.easyocr, "Reader") as reader:
                ocr.add_ocr_to_mapping(mapping, ocr_csv, False, 1)
            name_files.enrich_csv_with_parsing(
                ocr_csv,
                enriched_csv,
                name_files.DEFAULT_ACCESSION_PATTERN,
                1,
            )

            reader.assert_not_called()
            with enriched_csv.open(newline="", encoding="utf-8") as handle:
                enriched = csv.DictReader(handle)
                self.assertEqual([], list(enriched))
                self.assertIn(name_files.COL_ACCESSION_ID, enriched.fieldnames)

    def test_unexpected_ocr_row_failure_is_logged_and_preserved(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mapping = root / "slide_mapping.csv"
            output = root / "ocr.csv"
            with mapping.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["original_slide_path", "thumbnail_path"],
                )
                writer.writeheader()
                writer.writerow(
                    {"original_slide_path": "broken.svs", "thumbnail_path": ""}
                )

            with mock.patch.object(ocr.easyocr, "Reader"), mock.patch.object(
                ocr, "perform_ocr_on_row", side_effect=RuntimeError("decoder failed")
            ), self.assertLogs(ocr.logger, level="ERROR") as logs:
                ocr.add_ocr_to_mapping(mapping, output, False, 1)

            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual("broken.svs", rows[0]["original_slide_path"])
            self.assertEqual("False", rows[0]["ocr_qc_needed"])
            self.assertTrue(any("decoder failed" in line for line in logs.output))

    def test_unexpected_name_row_failure_is_logged_and_preserved(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_csv = root / "ocr.csv"
            output = root / "enriched.csv"
            with input_csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["original_slide_path", "label_text", "macro_text"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "original_slide_path": "broken.svs",
                        "label_text": "",
                        "macro_text": "",
                    }
                )

            with mock.patch.object(
                name_files,
                "process_csv_row",
                side_effect=RuntimeError("parser failed"),
            ), self.assertLogs(name_files.logger, level="ERROR") as logs:
                name_files.enrich_csv_with_parsing(
                    input_csv,
                    output,
                    name_files.DEFAULT_ACCESSION_PATTERN,
                    1,
                )

            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual("broken.svs", rows[0]["original_slide_path"])
            self.assertEqual("False", rows[0][name_files.COL_EXTRACTION_SUCCESSFUL])
            self.assertTrue(any("parser failed" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
