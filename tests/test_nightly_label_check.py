import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import nightly_label_check


class NightlyLabelCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.batches = self.root / "batches"
        self.inventories = self.root / "inventories"
        self.scanner = self.batches / "SS100"
        self.scanner.mkdir(parents=True)
        self.inventories.mkdir()
        self.log_path = self.scanner / "SS100_nightly_label_check_log.csv"
        pd.DataFrame(
            {
                "datetime_of_run": ["2026-10-29 01:00:00"],
                "batches_created": ["2026-10-28"],
                "slides_checked": [1],
                "errors": ["NONE"],
            }
        ).to_csv(self.log_path, index=False)
        self.environment = {
            "INSLIDE_BATCHES": str(self.batches),
            "SCANNER_INVENTORIES": str(self.inventories),
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_inventory(self):
        old = self.root / "images" / "SS100" / "2026-10-27"
        new = self.root / "images" / "SS100" / "2026-10-29"
        pd.DataFrame({"directory": [old, new, new]}).to_csv(
            self.inventories / "SS100_inventory.csv", index=False
        )
        return new

    def test_unsorted_inventory_and_nested_labels_are_counted(self):
        new = self.write_inventory()

        def run_pipeline(command, **_kwargs):
            output = Path(command[command.index("--output-dir") + 1])
            (output / "label" / "rack").mkdir(parents=True)
            (output / "label" / "rack" / "one.png").write_bytes(b"one")
            (output / "label" / "two.png").write_bytes(b"two")
            pd.DataFrame(
                {
                    "label_path": ["label/rack/one.png", "label/two.png"],
                    "original_slide_path": ["rack/one.svs", "two.svs"],
                }
            ).to_csv(output / "slide_mapping.csv", index=False)

        with mock.patch.dict(os.environ, self.environment), mock.patch.object(
            nightly_label_check.subprocess, "run", side_effect=run_pipeline
        ) as run:
            result = nightly_label_check.main()

        command = run.call_args.args[0]
        updated_log = pd.read_csv(self.log_path)
        self.assertEqual(0, result)
        self.assertEqual(sys.executable, command[0])
        self.assertEqual(str(ROOT_DIR / "src" / "pipeline.py"), command[1])
        self.assertEqual(str(new), command[command.index("--input-dir") + 1])
        self.assertEqual(2, updated_log.loc[0, "slides_checked"])

    def test_pipeline_failure_returns_nonzero_scheduler_status(self):
        self.write_inventory()
        error = subprocess.CalledProcessError(1, ["pipeline"])

        with mock.patch.dict(os.environ, self.environment), mock.patch.object(
            nightly_label_check.subprocess, "run", side_effect=error
        ):
            result = nightly_label_check.main()

        updated_log = pd.read_csv(self.log_path)
        self.assertEqual(1, result)
        self.assertIn("returned non-zero exit status 1", updated_log.loc[0, "errors"])


if __name__ == "__main__":
    unittest.main()
