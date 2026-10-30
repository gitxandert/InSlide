import json
import logging
import tempfile
import unittest
from pathlib import Path

from src.pipeline_events import configure_pipeline_event_logging


class PipelineEventTests(unittest.TestCase):
    def test_structured_warning_includes_stage_slide_and_utc_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "pipeline_events.jsonl"
            logger = logging.getLogger(f"pipeline-event-test-{id(self)}")
            logger.propagate = False
            handler = configure_pipeline_event_logging(logger, path, "ocr")
            try:
                logger.warning("Label image missing", extra={"slide": "one.svs"})
                logger.error("OCR failed", extra={"slide": "two.svs"})
            finally:
                logger.removeHandler(handler)
                handler.close()

            events = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(["WARNING", "ERROR"], [event["level"] for event in events])
        self.assertEqual(["one.svs", "two.svs"], [event["slide"] for event in events])
        self.assertEqual("Label image missing", events[0]["message"])
        self.assertTrue(all(event["stage"] == "ocr" for event in events))
        self.assertTrue(all(event["timestamp"].endswith("Z") for event in events))


if __name__ == "__main__":
    unittest.main()
