import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "1_get_macro.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("get_macro", MODULE_PATH)
get_macro = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(get_macro)


class GetMacroTests(unittest.TestCase):
    def test_extraction_failure_logs_slide_and_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            slide = root / "broken.svs"
            event_log = root / get_macro.EVENT_LOG_NAME
            handler = get_macro.configure_pipeline_event_logging(
                get_macro.logger, event_log, "macro"
            )
            broken_openslide = mock.Mock()
            broken_openslide.OpenSlide.side_effect = RuntimeError("corrupt slide")
            try:
                with mock.patch.object(
                    get_macro, "openslide", broken_openslide
                ):
                    result = get_macro.extract_associated_images(
                        slide, root, root / "output"
                    )
            finally:
                get_macro.logger.removeHandler(handler)
                handler.close()

            event = json.loads(event_log.read_text(encoding="utf-8"))
            self.assertIsNone(result)
            self.assertEqual(str(slide), event["slide"])
            self.assertIn("corrupt slide", event["message"])

    def test_partial_slide_failure_writes_successes_and_completes_stage(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            first = input_dir / "one.svs"
            second = input_dir / "two.svs"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            successful = {
                "original": first,
                "label": output_dir / "label" / "one_label.png",
                "macro": None,
                "thumbnail": None,
            }

            with mock.patch.object(
                get_macro, "extract_associated_images", side_effect=[successful, None]
            ):
                with self.assertLogs(get_macro.logger, level="ERROR") as logs:
                    get_macro.process_slide_files(
                        input_dir,
                        output_dir,
                        output_dir / "slide_mapping.csv",
                        ["svs"],
                        1,
                        (300, 300),
                    )

            self.assertTrue(
                any("Failed to process 1 of 2" in line for line in logs.output)
            )
            with (output_dir / "slide_mapping.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(1, len(rows))
            self.assertEqual(str(first), rows[0]["original_slide_path"])

    def test_all_slide_failures_still_write_mapping_csv(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            (input_dir / "one.svs").write_bytes(b"one")

            with mock.patch.object(
                get_macro, "extract_associated_images", return_value=None
            ):
                get_macro.process_slide_files(
                    input_dir,
                    output_dir,
                    output_dir / "slide_mapping.csv",
                    ["svs"],
                    1,
                    (300, 300),
                )

            with (output_dir / "slide_mapping.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                self.assertEqual([], list(csv.DictReader(handle)))

    def test_common_whole_slide_extensions_are_enabled(self):
        self.assertIn("svs", get_macro.DEFAULT_SLIDE_EXTENSIONS)
        self.assertIn("ndpi", get_macro.DEFAULT_SLIDE_EXTENSIONS)
        self.assertIn("tiff", get_macro.DEFAULT_SLIDE_EXTENSIONS)


if __name__ == "__main__":
    unittest.main()
