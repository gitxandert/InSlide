"""Rebuild one accession's catalog names from manually corrected mapping CSVs."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from batch_catalog import catalog
import renaming


def restore_missing_rows(
    instance_dir: Path, batches_root: Path, accession: str
) -> list[tuple[str, int]]:
    key = renaming.accession_key(accession)
    if not key:
        raise ValueError("accession must not be blank")

    restored = []
    for batch in catalog.list_batches(instance_dir):
        if not batch["renamed_complete"] or batch["validity"] != "ready":
            continue
        public_id = str(batch["public_id"])
        try:
            _, stored_rows, _ = catalog.load_document(
                instance_dir, public_id, "name_mapping"
            )
        except KeyError:
            continue
        stored_target = {
            row["OriginalPath"]: row
            for row in stored_rows
            if renaming.row_accession_key(row) == key
        }
        if not stored_target:
            continue

        mapping_path = batches_root / str(batch["relative_path"]) / "name_mapping.csv"
        fields, corrected_rows = renaming.read_csv(mapping_path)
        corrected_paths = {
            row["OriginalPath"]
            for row in corrected_rows
            if renaming.row_accession_key(row) == key
        }
        extra = corrected_paths - stored_target.keys()
        if extra:
            raise ValueError(
                f"{batch['relative_path']}: CSV contains rows absent from catalog: "
                f"{', '.join(sorted(extra))}"
            )
        missing = [
            row for path, row in stored_target.items() if path not in corrected_paths
        ]
        if missing:
            renaming.atomic_write(mapping_path, fields, [*corrected_rows, *missing])
            restored.append((str(mapping_path), len(missing)))
    return restored


def rebuild_pid_names(
    instance_dir: Path,
    batches_root: Path,
    accession: str,
    *,
    apply: bool = False,
) -> list[tuple[str, int]]:
    key = renaming.accession_key(accession)
    if not key:
        raise ValueError("accession must not be blank")

    plans: list[tuple[str, list[str], list[dict[str, str]], int]] = []
    assignments: dict[tuple[str, str], set[str]] = {}
    for batch in catalog.list_batches(instance_dir):
        if not batch["renamed_complete"] or batch["validity"] != "ready":
            continue
        public_id = str(batch["public_id"])
        try:
            fields, stored_rows, _ = catalog.load_document(
                instance_dir, public_id, "name_mapping"
            )
        except KeyError:
            continue
        stored_target = {
            row["OriginalPath"]: index
            for index, row in enumerate(stored_rows)
            if renaming.row_accession_key(row) == key
        }
        if not stored_target:
            continue

        mapping_path = batches_root / str(batch["relative_path"]) / "name_mapping.csv"
        _, corrected_rows = renaming.read_csv(mapping_path)
        corrected_target = {
            row["OriginalPath"]: row
            for row in corrected_rows
            if renaming.row_accession_key(row) == key
        }
        if stored_target.keys() != corrected_target.keys():
            raise ValueError(
                f"{batch['relative_path']}: corrected CSV rows do not match catalog rows"
            )

        updated = [dict(row) for row in stored_rows]
        for path, index in stored_target.items():
            source = corrected_target[path]
            organ = source.get("Organ", "").strip().upper()
            pid = source.get("PID", "").strip().upper()
            assignments.setdefault((organ, pid), set()).add(str(mapping_path))
            updated[index]["Organ"] = organ
            updated[index]["PID"] = pid
            updated[index]["NewName"] = renaming.build_new_name(updated[index])

        validation_rows = [dict(row) for row in updated]
        errors = renaming.validate_mapping_rows(validation_rows)
        if errors:
            raise ValueError(f"{batch['relative_path']}: {'; '.join(errors)}")
        plans.append((public_id, fields, updated, len(stored_target)))

    if not plans:
        raise ValueError(f"accession {accession} was not found in any completed batch")
    if len(assignments) != 1:
        values = "; ".join(
            f"{organ}/{pid} in {', '.join(sorted(paths))}"
            for (organ, pid), paths in sorted(assignments.items())
        )
        raise ValueError(
            f"corrected CSVs still disagree for accession {accession}: {values}"
        )

    if apply:
        for public_id, fields, rows, _ in plans:
            catalog.replace_document(
                instance_dir, public_id, "name_mapping", fields, rows
            )
    return [(public_id, count) for public_id, _, _, count in plans]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("accession", help="accession ID whose Organ/PID was corrected")
    result.add_argument(
        "--instance-dir",
        type=Path,
        default=Path(os.environ.get("INSTANCE_DIR", Path(__file__).parent / "instance")),
    )
    result.add_argument(
        "--batches-root",
        type=Path,
        default=Path(os.environ.get("INSLIDE_BATCHES", r"D:\label_check_batches")),
    )
    action = result.add_mutually_exclusive_group()
    action.add_argument(
        "--apply", action="store_true", help="update catalog and enqueue CSV exports"
    )
    action.add_argument(
        "--restore-missing",
        action="store_true",
        help="restore deleted accession rows from catalog into mapping CSVs",
    )
    return result


def main(argv: Optional[list[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.restore_missing:
            restored = restore_missing_rows(
                args.instance_dir, args.batches_root, args.accession
            )
            count = sum(value for _, value in restored)
            print(f"Restored {count} row(s) in {len(restored)} mapping CSV(s).")
            return 0
        plans = rebuild_pid_names(
            args.instance_dir, args.batches_root, args.accession, apply=args.apply
        )
    except (OSError, sqlite3.Error, renaming.RenamingError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    slide_count = sum(count for _, count in plans)
    print(f"Validated {slide_count} slide(s) in {len(plans)} completed batch(es).")
    if args.apply:
        print("Catalog updated; restart InSlide to export corrected name_mapping.csv files.")
    else:
        print("Dry run complete. Re-run with --apply to update the catalog.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
