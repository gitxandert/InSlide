"""Resolve duplicate output filenames across finalized slide mappings."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional

from batch_catalog import catalog
import renaming


@dataclass
class MappingDocument:
    public_id: str
    relative_path: str
    fields: list[str]
    rows: list[dict[str, str]]
    version: int


@dataclass(frozen=True)
class FilenameChange:
    public_id: str
    relative_path: str
    row_index: int
    old_name: str
    new_name: str


@dataclass
class RepairPlan:
    documents: list[MappingDocument]
    changes: list[FilenameChange]
    conflict_count: int


def _date(value: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _scan_sort_key(
    relative_path: str, row: dict[str, str], row_index: int
) -> tuple[object, ...]:
    scan_date = _scan_date(relative_path, row)
    return (
        scan_date is None,
        scan_date or dt.date.max,
        relative_path.casefold(),
        row_index,
    )


def _scan_date(relative_path: str, row: dict[str, str]) -> Optional[dt.date]:
    original_path = row.get("OriginalPath", "")
    parent = (
        PureWindowsPath(original_path).parent.name
        if "\\" in original_path
        else Path(original_path).parent.name
    )
    return _date(parent) or _date(PurePosixPath(relative_path).name)


def _scan_group(relative_path: str, row: dict[str, str]) -> tuple[object, ...]:
    scan_date = _scan_date(relative_path, row)
    return (
        ("date", scan_date, relative_path.casefold())
        if scan_date is not None
        else ("batch", relative_path.casefold())
    )


def _filename_family(row: dict[str, str]) -> tuple[str, ...]:
    fields = (
        "Organ",
        "PID",
        "AccessionDate",
        "Timepoint",
        "Stain",
        "ImageType",
        "SampAcqType",
        "BlockNumber",
    )
    suffix = Path(row.get("OriginalPath", "")).suffix
    return (*(row.get(field, "").casefold() for field in fields), suffix.casefold())


def _load_documents(instance_dir: Path) -> list[MappingDocument]:
    documents = []
    for batch in catalog.list_batches(instance_dir):
        if batch["validity"] != "ready" or not batch["renamed_complete"]:
            continue
        public_id = str(batch["public_id"])
        try:
            fields, rows, state = catalog.load_document(
                instance_dir, public_id, "name_mapping"
            )
        except KeyError:
            continue
        documents.append(
            MappingDocument(
                public_id,
                str(batch["relative_path"]),
                fields,
                [dict(row) for row in rows],
                int(state["desired_version"]),
            )
        )
    return documents


def plan_repairs(instance_dir: Path) -> RepairPlan:
    documents = _load_documents(instance_dir)
    occurrences: dict[str, list[tuple[MappingDocument, int]]] = defaultdict(list)
    families: dict[tuple[str, ...], list[tuple[MappingDocument, int]]] = defaultdict(list)
    occupied_names: Counter[str] = Counter()

    for document in documents:
        for row_index, row in enumerate(document.rows):
            name = row.get("NewName", "").strip()
            if name:
                key = name.casefold()
                occurrences[key].append((document, row_index))
                occupied_names[key] += 1
            families[_filename_family(row)].append((document, row_index))

    conflicts = [
        group for _, group in sorted(occurrences.items()) if len(group) > 1
    ]
    changes = []
    conflicting_families = {
        _filename_family(document.rows[row_index])
        for group in conflicts
        for document, row_index in group
    }
    for family in sorted(conflicting_families):
        ordered = sorted(
            families[family],
            key=lambda item: _scan_sort_key(
                item[0].relative_path, item[0].rows[item[1]], item[1]
            ),
        )
        first_document, first_row_index = ordered[0]
        first_scan = _scan_group(
            first_document.relative_path, first_document.rows[first_row_index]
        )
        preserved = [
            item for item in ordered
            if _scan_group(item[0].relative_path, item[0].rows[item[1]]) == first_scan
        ]
        later = [item for item in ordered if item not in preserved]
        sections = [
            int(document.rows[row_index]["SectionCount"])
            for document, row_index in preserved
            if renaming.SECTION_RE.fullmatch(
                document.rows[row_index].get("SectionCount", "")
            )
        ]
        next_section = max(sections, default=-1) + 1
        for document, row_index in later:
            old_key = document.rows[row_index].get("NewName", "").strip().casefold()
            if old_key:
                occupied_names[old_key] -= 1

        for document, row_index in later:
            row = document.rows[row_index]
            old_name = row["NewName"]
            for section in range(next_section, 1000):
                candidate_row = dict(row)
                candidate_row["SectionCount"] = f"{section:03d}"
                candidate_name = renaming.build_new_name(candidate_row)
                if occupied_names[candidate_name.casefold()]:
                    continue
                row["SectionCount"] = candidate_row["SectionCount"]
                row["NewName"] = candidate_name
                occupied_names[candidate_name.casefold()] += 1
                next_section = section + 1
                if candidate_name != old_name:
                    changes.append(
                        FilenameChange(
                            document.public_id,
                            document.relative_path,
                            row_index,
                            old_name,
                            candidate_name,
                        )
                    )
                break
            else:
                raise ValueError(
                    f"{document.relative_path}: no unused SectionCount remains for "
                    f"{old_name}"
                )

    changed_ids = {change.public_id for change in changes}
    for document in documents:
        if document.public_id not in changed_ids:
            continue
        validation_rows = [dict(row) for row in document.rows]
        errors = renaming.validate_mapping_rows(validation_rows)
        if errors:
            raise ValueError(f"{document.relative_path}: {'; '.join(errors)}")

    final_names = [
        row.get("NewName", "").strip().casefold()
        for document in documents
        for row in document.rows
        if row.get("NewName", "").strip()
    ]
    if len(final_names) != len(set(final_names)):
        raise ValueError("repair plan still contains duplicate output filenames")
    return RepairPlan(documents, changes, len(conflicts))


def apply_repairs(instance_dir: Path, plan: RepairPlan) -> int:
    changed_ids = {change.public_id for change in plan.changes}
    changed_documents = [
        document
        for document in plan.documents
        if document.public_id in changed_ids
    ]
    for document in changed_documents:
        state = catalog.document_state(
            instance_dir, document.public_id, "name_mapping"
        )
        if state is None or int(state["desired_version"]) != document.version:
            raise RuntimeError(
                f"{document.relative_path}: mapping changed during repair; rerun"
            )
    for document in changed_documents:
        catalog.replace_document(
            instance_dir,
            document.public_id,
            "name_mapping",
            document.fields,
            document.rows,
        )
    return len(changed_documents)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--instance-dir",
        type=Path,
        default=Path(
            os.environ.get("INSTANCE_DIR", Path(__file__).parent / "instance")
        ),
        help="catalog state directory (defaults to INSTANCE_DIR)",
    )
    result.add_argument(
        "--apply",
        action="store_true",
        help="update mappings and enqueue their CSV exports",
    )
    return result


def main(argv: Optional[list[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        plan = plan_repairs(args.instance_dir)
        for change in plan.changes:
            print(
                f"{change.relative_path} row {change.row_index + 1}: "
                f"{change.old_name} -> {change.new_name}"
            )
        if not plan.changes:
            print("No duplicate output filenames found.")
            return 0
        print(
            f"Found {plan.conflict_count} conflicting filename(s); "
            f"{len(plan.changes)} row(s) need new names."
        )
        if not args.apply:
            print("Dry run complete. Re-run with --apply to update the catalog.")
            return 0
        document_count = apply_repairs(args.instance_dir, plan)
    except (OSError, sqlite3.Error, renaming.RenamingError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(
        f"Updated {len(plan.changes)} row(s) in {document_count} batch(es); "
        "CSV exports are queued."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
