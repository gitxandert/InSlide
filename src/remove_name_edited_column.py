"""Queue catalog-backed name_mapping.csv exports without NameEdited.

Stop InSlide before --apply. Restart it afterward to write queued exports.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from batch_catalog import catalog, normalize_relative_path


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def has_name_edited_column(path: Path) -> bool:
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            return "NameEdited" in next(csv.reader(handle), [])
    except (UnicodeError, csv.Error):
        return True


def plan(batches_root: Path, instance_dir: Path) -> list[tuple[Path, str, list[str], list[dict], int]]:
    if not batches_root.is_dir():
        raise ValueError(f"batch directory not found: {batches_root}")
    if not catalog.database_path(instance_dir).is_file():
        raise ValueError(f"catalog not found: {catalog.database_path(instance_dir)}")
    changes = []
    known_paths = set()
    for batch in catalog.list_batches(instance_dir):
        batch_id = str(batch["public_id"])
        try:
            fields, rows, state = catalog.load_document(
                instance_dir, batch_id, "name_mapping"
            )
        except KeyError:
            continue
        path = batches_root / normalize_relative_path(str(batch["relative_path"])) / "name_mapping.csv"
        known_paths.add(path)
        if ("NameEdited" in fields or state["status"] != "current"
                or not path.is_file() or file_hash(path) != state["source_hash"]
                or has_name_edited_column(path)):
            changes.append((path, batch_id, fields, rows, int(state["desired_version"])))
    for path in batches_root.glob("SS*/*/name_mapping.csv"):
        if path not in known_paths:
            raise ValueError(f"{path}: no matching catalog document")
    return changes


def apply(changes: list[tuple[Path, str, list[str], list[dict], int]],
          instance_dir: Path) -> Path:
    for path, batch_id, _, _, expected_version in changes:
        state = catalog.document_state(instance_dir, batch_id, "name_mapping")
        if state is None or int(state["desired_version"]) != expected_version:
            raise RuntimeError(f"{path}: catalog changed during cleanup; rerun")
    backup_dir = instance_dir / "catalog_audits" / (
        "remove_name_edited_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    )
    backup_dir.mkdir(parents=True)
    database = catalog.database_path(instance_dir)
    if database.is_file():
        with closing(sqlite3.connect(database)) as source, closing(
            sqlite3.connect(backup_dir / database.name)
        ) as target:
            source.backup(target)
    for path, _, _, _, _ in changes:
        if not path.is_file():
            continue
        relative = Path(path.parent.parent.name) / path.parent.name / path.name
        backup_path = backup_dir / relative
        backup_path.parent.mkdir(parents=True)
        shutil.copy2(path, backup_path)
    for path, batch_id, fields, rows, _ in changes:
        updated_fields = [field for field in fields if field != "NameEdited"]
        catalog.replace_document(
            instance_dir, batch_id, "name_mapping", updated_fields, rows
        )
    return backup_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batches-root", type=Path, required=True)
    parser.add_argument("--instance-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        changes = plan(args.batches_root, args.instance_dir)
        print(f"{len(changes)} mapping export(s) need reconciliation")
        for path, _, _, _, _ in changes:
            print(path)
        if args.apply and changes:
            print(f"Queued {len(changes)} catalog export(s); backup: {apply(changes, args.instance_dir)}")
            print("Restart InSlide to write the CSV files.")
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
