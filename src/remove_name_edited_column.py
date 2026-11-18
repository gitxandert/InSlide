"""Remove NameEdited from existing name_mapping.csv files.

Stop InSlide before --apply. Without --apply, list files that need cleanup.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from batch_catalog import catalog, public_batch_id
import renaming


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan(batches_root: Path, instance_dir: Path) -> list[tuple[Path, str | None, list[str], list[dict]]]:
    if not batches_root.is_dir():
        raise ValueError(f"batch directory not found: {batches_root}")
    paths = sorted(batches_root.glob("SS*/*/name_mapping.csv"))
    if not paths:
        raise ValueError(f"no name_mapping.csv files found under {batches_root}")
    has_catalog = catalog.database_path(instance_dir).is_file()
    changes = []
    for path in paths:
        fields, rows = renaming.read_csv(path)
        if len(fields) != len(set(fields)) or any(None in row for row in rows):
            raise ValueError(f"{path}: malformed CSV header or row")
        batch_id = None
        if has_catalog:
            batch_id = public_batch_id(path.parent.relative_to(batches_root).as_posix())
            try:
                stored_fields, stored_rows, state = catalog.load_document(
                    instance_dir, batch_id, "name_mapping"
                )
            except KeyError as exc:
                raise ValueError(f"{path}: no matching catalog document") from exc
            if state["status"] not in {"current", "conflict"}:
                raise ValueError(f"{path}: catalog export is {state['status']}; finish it first")
            public_fields = [field for field in fields if field != "NameEdited"]
            stored_public_fields = [field for field in stored_fields if field != "NameEdited"]
            if public_fields != stored_public_fields:
                raise ValueError(f"{path}: CSV and catalog columns differ beyond NameEdited")
            if len(rows) != len(stored_rows):
                raise ValueError(
                    f"{path}: CSV has {len(rows)} rows; catalog has {len(stored_rows)}"
                )
            for index, (row, stored) in enumerate(zip(rows, stored_rows), 1):
                for field in public_fields:
                    if row.get(field, "") != stored.get(field, ""):
                        raise ValueError(f"{path}: row {index} differs in {field}")
            rows = [
                dict(row, NameEdited=(
                    "True" if renaming.parse_bool(row.get("NameEdited", ""))
                    or renaming.parse_bool(stored.get("NameEdited", "")) else "False"
                ))
                for row, stored in zip(rows, stored_rows)
            ]
        elif any(renaming.parse_bool(row.get("NameEdited", "")) for row in rows):
            raise ValueError(f"{path}: edited flags need a catalog before removal")
        if ("NameEdited" in fields or has_catalog and (
                "NameEdited" in stored_fields or state["status"] == "conflict"
                or file_hash(path) != state["source_hash"])):
            changes.append((path, batch_id, fields, rows))
    return changes


def apply(changes: list[tuple[Path, str | None, list[str], list[dict]]],
          instance_dir: Path) -> Path:
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
    for path, batch_id, fields, rows in changes:
        relative = Path(path.parent.parent.name) / path.parent.name / path.name
        backup_path = backup_dir / relative
        backup_path.parent.mkdir(parents=True)
        shutil.copy2(path, backup_path)
        updated_fields = [field for field in fields if field != "NameEdited"]
        if batch_id is not None:
            version = catalog.replace_document(
                instance_dir, batch_id, "name_mapping", updated_fields, rows
            )
        renaming.atomic_write(path, updated_fields, rows)
        if os.name != "nt" and batch_id is not None:
            path.chmod(0o444)
        if batch_id is not None:
            catalog.finish_export(instance_dir, batch_id, "name_mapping", version, file_hash(path))
    return backup_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batches-root", type=Path, required=True)
    parser.add_argument("--instance-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        changes = plan(args.batches_root, args.instance_dir)
        print(f"{len(changes)} mapping(s) contain NameEdited")
        for path, _, _, _ in changes:
            print(path)
        if args.apply and changes:
            print(f"Cleaned {len(changes)} mapping(s); backup: {apply(changes, args.instance_dir)}")
    except (OSError, sqlite3.Error, renaming.RenamingError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
