"""Shared rules for assigning Slide Digitization Log Types to scan batches."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional


SDLTypeKey = tuple[str, str, str]


def normalized_type(value: object) -> str:
    slide_type = str(value or "").strip()
    return "NONE" if slide_type.casefold() == "none" else slide_type


def normalized_date(value: object) -> str:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.strftime("%Y-%m-%d")
    return str(value or "").strip()


def type_key(accession: object, scanner: object, loaded_date: object) -> SDLTypeKey:
    return (
        str(scanner or "").strip().casefold(),
        normalized_date(loaded_date),
        str(accession or "").strip().casefold(),
    )


@dataclass(frozen=True)
class SDLTypeRecord:
    row_number: int
    accession: str
    scanner: str
    loaded_date: str
    slide_type: str

    @property
    def key(self) -> SDLTypeKey:
        return type_key(self.accession, self.scanner, self.loaded_date)


@dataclass(frozen=True)
class SDLTypeConflict:
    key: SDLTypeKey
    records: tuple[SDLTypeRecord, ...]


def type_record(
    values: Mapping[str, object], row_number: int
) -> Optional[SDLTypeRecord]:
    accession = str(values.get("Accession ID") or "").strip()
    slide_type = normalized_type(values.get("Type"))
    if not accession or not slide_type:
        return None
    return SDLTypeRecord(
        row_number=row_number,
        accession=accession,
        scanner=str(values.get("Scanner") or "").strip(),
        loaded_date=normalized_date(values.get("Date Loaded")),
        slide_type=slide_type,
    )


def resolve_types(
    records: Iterable[SDLTypeRecord],
) -> tuple[dict[SDLTypeKey, str], list[SDLTypeConflict]]:
    """Use first Type per key and report keys containing contrasting Types."""
    grouped: dict[SDLTypeKey, list[SDLTypeRecord]] = defaultdict(list)
    resolved: dict[SDLTypeKey, str] = {}
    for record in records:
        grouped[record.key].append(record)
        resolved.setdefault(record.key, record.slide_type)

    conflicts = []
    for key, values in grouped.items():
        if len({record.slide_type.casefold() for record in values}) > 1:
            conflicts.append(SDLTypeConflict(key, tuple(values)))
    return resolved, conflicts


def format_conflict(conflict: SDLTypeConflict) -> str:
    first = conflict.records[0]
    rows = ", ".join(
        f"row {record.row_number} ({record.slide_type})"
        for record in conflict.records
    )
    return (
        f"scanner={first.scanner or '<blank>'}, "
        f"date={first.loaded_date or '<blank>'}, accession={first.accession}: {rows}"
    )
