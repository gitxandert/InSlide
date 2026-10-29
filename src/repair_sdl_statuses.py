"""One-off SDL status repair using existing batch enriched.csv files."""
from pathlib import Path

from app import Config, _reconcile_sdl


if __name__ == "__main__":
    roots = [Path(Config.INSLIDE_BATCHES) / row["relative_path"]
             for row in __import__("batch_catalog").catalog.list_batches(Config.INSTANCE_DIR)]
    print(f"Updated { _reconcile_sdl(roots) } SDL status cell(s).")
