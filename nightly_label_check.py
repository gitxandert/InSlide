import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

SRC_DIR = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC_DIR))

from container_paths import runtime_path

inventory_chunk_size = 10000


def find_new_date_folders(inventory_path: Path, latest_batch: str) -> list[Path]:
    folders = {
        runtime_path(directory)
        for chunk in pd.read_csv(inventory_path, chunksize=inventory_chunk_size)
        for directory in chunk["directory"].dropna().unique()
        if runtime_path(directory).name > latest_batch
    }
    return sorted(folders, key=lambda path: path.name)


def main() -> int:
    label_check_batches = Path(
        os.environ.get("INSLIDE_BATCHES", "D:\\label_check_batches")
    )
    scanner_inventories = Path(
        os.environ.get("SCANNER_INVENTORIES", "D:\\scanner_inventories")
    )
    failed = False

    for scanner_dir in label_check_batches.iterdir():
        scanner = scanner_dir.name
        scanner_log_path = scanner_dir / f"{scanner}_nightly_label_check_log.csv"
        if not scanner_log_path.exists():
            continue

        print(f"\x1b[1m{scanner}\x1b[0m")
        scanner_log = pd.read_csv(scanner_log_path, parse_dates=["datetime_of_run"])
        completed_batches = [
            batch
            for value in scanner_log["batches_created"].dropna().astype(str)
            for batch in value.split(";")
            if batch
        ]
        if not completed_batches:
            print(f"\x1b[31mERROR\x1b[0m: {scanner_log_path} has no completed batches\n")
            failed = True
            continue
        abs_latest_batch = max(completed_batches)
        print(f"Latest batch: {abs_latest_batch}")

        scanner_inventory_path = scanner_inventories / f"{scanner}_inventory.csv"
        if not scanner_inventory_path.exists():
            print(
                f"\x1b[31mERROR\x1b[0m: no scanner inventory for {scanner} "
                f"found at {scanner_inventory_path}\n"
            )
            failed = True
            continue

        new_date_folders = find_new_date_folders(
            scanner_inventory_path, abs_latest_batch
        )
        if not new_date_folders:
            print(f"No new digitized slides in {scanner} since {abs_latest_batch}\n")
            continue

        datetime_of_run = datetime.now()
        batches_created = []
        slides_checked = 0
        errors = []
        for dir_path in new_date_folders:
            dir_date = dir_path.name
            output_folder = scanner_dir / dir_date
            if output_folder.exists():
                print(f"\x1b[33mWARNING\x1b[0m: reusing existing {output_folder}")
            print(f"Running pipeline on {dir_path}...")
            pipeline_cmd = [
                sys.executable,
                str(SRC_DIR / "pipeline.py"),
                "--input-dir",
                str(dir_path),
                "--output-dir",
                str(output_folder),
                "--run-type",
                "nightly",
                "--end-at",
                "name",
            ]
            try:
                subprocess.run(pipeline_cmd, check=True, text=True)
                mapping_path = output_folder / "slide_mapping.csv"
                slide_count = len(pd.read_csv(mapping_path))
            except Exception as error:
                print(f"\x1b[31mERROR\x1b[0m: {error}")
                errors.append(f"{output_folder}: {error}")
                failed = True
                break

            print(
                f"\x1b[32mSUCCESS\x1b[0m: Processed {slide_count} slides "
                f"in {output_folder}\n"
            )
            slides_checked += slide_count
            batches_created.append(dir_date)

        new_log_row = pd.DataFrame(
            {
                "datetime_of_run": [datetime_of_run],
                "batches_created": [";".join(batches_created)],
                "slides_checked": [slides_checked],
                "errors": [";".join(errors) or "NONE"],
            }
        )
        updated_log = pd.concat([new_log_row, scanner_log], ignore_index=True)
        try:
            updated_log.to_csv(scanner_log_path, index=False)
            print(f"Updated {scanner_log_path}\n")
        except Exception as error:
            print(f"\x1b[31mERROR\x1b[0m -- unable to update log: {error}\n")
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
