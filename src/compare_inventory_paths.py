"""Diagnose catalog source paths absent from scanner inventories (read only)."""

from __future__ import annotations

import argparse
import os
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

import renaming
from resolve_output_filename_conflicts import _inventory_rows, _load_documents


def basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def compare(instance_dir: Path, inventory_root: Path) -> None:
    inventories = {
        path.name.removesuffix("_inventory.csv"): _inventory_rows(path)
        for path in inventory_root.glob("SS*_inventory.csv")
    }
    matches = defaultdict(list)
    for scanner, inventory in inventories.items():
        for path in inventory:
            matches[scanner, basename(path)].append(path)

    counts = Counter()
    examples = defaultdict(list)
    for document in _load_documents(instance_dir):
        scanner = PurePosixPath(document.relative_path).parts[0]
        if scanner not in inventories:
            raise ValueError(f"scanner inventory is missing for {scanner}")
        for row in document.rows:
            original = row.get("OriginalPath", "")
            normalized = renaming.normalized_slide_path(original)
            if normalized in inventories[scanner]:
                continue
            candidates = matches[scanner, basename(normalized)]
            kind = "same basename" if candidates else "no basename match"
            counts[scanner, kind] += 1
            if len(examples[scanner, kind]) < 3:
                examples[scanner, kind].append((original, normalized, candidates[:2]))

    if counts:
        print(f"{sum(counts.values())} missing paths total")
    for scanner in sorted({scanner for scanner, _ in counts}):
        print(f"{scanner}: {sum(counts[scanner, kind] for kind in ('same basename', 'no basename match'))} missing paths")
        for kind in ("same basename", "no basename match"):
            print(f"  {counts[scanner, kind]} {kind}")
            for original, normalized, candidates in examples[scanner, kind]:
                print(f"    catalog: {original}")
                if normalized != original:
                    print(f"    normalized: {normalized}")
                for candidate in candidates:
                    print(f"    inventory: {candidate}")
    if not counts:
        print("All catalog source paths appear in scanner inventories.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-dir", type=Path, default=Path(
        os.environ.get("INSTANCE_DIR", Path(__file__).parent / "instance")
    ))
    parser.add_argument("--inventory-root", type=Path, default=Path(
        os.environ.get("SCANNER_INVENTORIES", r"D:\scanner_inventories")
    ))
    args = parser.parse_args()
    compare(args.instance_dir, args.inventory_root)


if __name__ == "__main__":
    main()
