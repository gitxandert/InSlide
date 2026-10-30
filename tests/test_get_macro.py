import csv
import importlib.util
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
    def test_partial_slide_failure_writes_successes_then_fails_stage(self):
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
                with self.assertRaisesRegex(RuntimeError, "Failed to process 1 of 2"):
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
                rows = list(csv.DictReader(handle))
            self.assertEqual(1, len(rows))
            self.assertEqual(str(first), rows[0]["original_slide_path"])

    def test_common_whole_slide_extensions_are_enabled(self):
        self.assertIn("svs", get_macro.DEFAULT_SLIDE_EXTENSIONS)
        self.assertIn("ndpi", get_macro.DEFAULT_SLIDE_EXTENSIONS)
        self.assertIn("tiff", get_macro.DEFAULT_SLIDE_EXTENSIONS)


if __name__ == "__main__":
    unittest.main()
