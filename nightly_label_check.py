import os
import subprocess, sys
from datetime import datetime
from pathlib import Path
import pandas as pd

SRC_DIR = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC_DIR))

from container_paths import runtime_path

label_check_batches = Path(
    os.environ.get("INSLIDE_BATCHES", "D:\\label_check_batches")
)
scanner_inventories = Path(
    os.environ.get("SCANNER_INVENTORIES", "D:\\scanner_inventories")
)
inventory_chunk_size = 10000

for scanner_dir in label_check_batches.iterdir():
    scanner = scanner_dir.name
    scanner_log_path = scanner_dir / f"{scanner}_nightly_label_check_log.csv"
    # skip any scanner we're not currently checking
    if not scanner_log_path.exists():
        continue

    print(f"\x1b[1m{scanner}\x1b[0m")
    # get date of last GT450_images/{scanner} subdirectory checked
    scanner_log = pd.read_csv(scanner_log_path, parse_dates=['datetime_of_run'])

    latest_batches = scanner_log.loc[0, 'batches_created']
    latest_batches_list = latest_batches.split(';')
    latest_batches_sorted = sorted(latest_batches_list, reverse=True)
    abs_latest_batch = latest_batches_sorted[0]
    print(f"Latest batch: {abs_latest_batch}")

    scanner_inventory_path = scanner_inventories / f"{scanner}_inventory.csv"
    if not scanner_inventory_path.exists():
        print(f"\x1b[31mERROR\x1b[0m: no scanner inventory for {scanner} found at {scanner_inventory_path}\n")
        continue

    # search scanner inventory for all new folders that have not yet been checked
    scanner_inventory_reader = pd.read_csv(scanner_inventory_path, chunksize=inventory_chunk_size)

    new_date_folders = []
    break_early = False
    for _, chunk in enumerate(scanner_inventory_reader):
        unique_dirs = chunk['directory'].unique()
        for dir in unique_dirs:
            dir_path = runtime_path(dir)
            dir_date = dir_path.name
            if dir_date > abs_latest_batch:
                new_date_folders.append(dir_path)
            else:
                break_early = True
                break
        if break_early:
            break

    if len(new_date_folders) == 0:
        print(f"No new digitized slides in {scanner} since {abs_latest_batch}\n")
        continue

    # run pipeline on each new scanner subdirectory
    datetime_of_run = datetime.now()
    batches_created = ""
    slides_checked = 0
    errors = "NONE"
    for dir_path in new_date_folders:
        dir_date = dir_path.name
        output_folder = scanner_dir / dir_date
        if output_folder.exists():
            print(f"\x1b[33mWARNING\x1b[0m: {output_folder} already exists and will be overwritten")
        print(f"Running pipeline on {dir_path}...")
        pipeline_cmd = [
            "python",
            "src/pipeline.py",
            "--input-dir",
            dir_path,
            "--output-dir",
            output_folder,
            "--run-type",
            "nightly",
            "--end-at",
            'name'
        ]
        try:
            subprocess.run(
                pipeline_cmd,
                check=True,
                text=True
            )
        except Exception as e:
            print(f"\x1b[31mERROR\x1b[0m: {e}")
            # log error in new log row and continue
            error = f"{output_folder}: {e}"
            if errors == 'NONE':
                errors = error
            else:
                errors += f";{error}"
            continue
        
        # count label images extracted for slide count
        label_dir = output_folder / "label"
        slide_count = sum(1 for item in label_dir.iterdir() if item.is_file())
        print(f"\x1b[32mSUCCESS\x1b[0m: Processed {slide_count} slides in {output_folder}\n")
        slides_checked += slide_count
        if batches_created == "":
            batches_created = dir_date
        else:
            batches_created += f";{dir_date}"
        
    new_log_row = pd.DataFrame({
        'datetime_of_run': [datetime_of_run],
        'batches_created': [batches_created],
        'slides_checked': [slides_checked],
        'errors': [errors]
    })

    updated_log = pd.concat([new_log_row, scanner_log], ignore_index=True)
    try:
        updated_log.to_csv(scanner_log_path, index=False)
        print(f"Updated {scanner_log_path}\n")
    except Exception as e:
        print(f"\x1b[31mERROR\x1b[0m -- unable to update log: {e}\n")
