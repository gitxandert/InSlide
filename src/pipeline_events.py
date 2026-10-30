"""Structured event logging shared by Label-Check pipeline stages."""

import datetime
import json
import logging
from pathlib import Path


EVENT_LOG_NAME = "pipeline_events.jsonl"


def _event(
    stage: str, level: str, message: str, slide: str | None = None
) -> dict[str, str | None]:
    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "stage": stage,
        "level": level,
        "slide": slide,
        "message": message,
    }


def append_pipeline_event(
    path: Path,
    stage: str,
    level: str,
    message: str,
    slide: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_event(stage, level, message, slide)) + "\n")


class PipelineEventFormatter(logging.Formatter):
    def __init__(self, stage: str):
        super().__init__()
        self.stage = stage

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        event = _event(
            self.stage,
            record.levelname,
            message,
            getattr(record, "slide", None),
        )
        event["timestamp"] = (
            datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        return json.dumps(event)


def configure_pipeline_event_logging(
    logger: logging.Logger, path: Path, stage: str
) -> logging.FileHandler:
    resolved_path = path.resolve()
    for handler in logger.handlers:
        if getattr(handler, "pipeline_event_path", None) == resolved_path:
            return handler

    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.pipeline_event_path = resolved_path
    handler.setLevel(logging.INFO)
    handler.setFormatter(PipelineEventFormatter(stage))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler
