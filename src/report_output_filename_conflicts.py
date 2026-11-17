"""Write proposed active filename changes and their existing name conflicts as CSV."""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

from resolve_output_filename_conflicts import (
    _load_documents,
    _scan_sort_key,
    plan_inventory_repairs,
)


def write_report(instance_dir: Path, inventory_root: Path, output) -> None:
    originals = _load_documents(instance_dir)
    by_name = defaultdict(list)
    for document in originals:
        for index, row in enumerate(document.rows):
            name = row.get("NewName", "").strip()
            if name:
                by_name[name.casefold()].append((document, index, row))

    writer = csv.writer(output)
    writer.writerow((
        "changed_batch", "changed_row", "current_name",
        "conflicting_batch", "conflicting_row", "conflicting_name",
        "proposed_new_name",
    ))
    for change in plan_inventory_repairs(instance_dir, inventory_root).changes:
        if change.disposition != "active" or change.old_name == change.new_name:
            continue
        others = [
            (document, index, row)
            for document, index, row in by_name[change.old_name.strip().casefold()]
            if (document.public_id, index) != (change.public_id, change.row_index)
        ]
        oldest = min(
            others,
            key=lambda item: _scan_sort_key(item[0].relative_path, item[2], item[1]),
            default=None,
        )
        writer.writerow((
            change.relative_path, change.row_index + 1, change.old_name,
            oldest[0].relative_path if oldest else "",
            oldest[1] + 1 if oldest else "",
            oldest[2]["NewName"] if oldest else "",
            change.new_name,
        ))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-dir", type=Path, default=Path(
        os.environ.get("INSTANCE_DIR", Path(__file__).parent / "instance")
    ))
    parser.add_argument("--inventory-root", type=Path, default=Path(
        os.environ.get("SCANNER_INVENTORIES", r"D:\scanner_inventories")
    ))
    args = parser.parse_args()
    write_report(args.instance_dir, args.inventory_root, sys.stdout)


if __name__ == "__main__":
    main()
