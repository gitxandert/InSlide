"""One-off SDL status repair using existing batch enriched.csv files."""
from pathlib import Path

from app import Config, _reconcile_sdl


if __name__ == "__main__":
    roots = [path.parent for path in Path(Config.INSLIDE_BATCHES).glob("SS*/*/enriched.csv")]
    print(f"Found {len(roots)} batch enriched.csv file(s) under {Config.INSLIDE_BATCHES}.")
    print(f"Updated {_reconcile_sdl(roots)} SDL status cell(s).")
