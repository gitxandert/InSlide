import argparse
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
RUN_TYPE_FILE = ".inslide_run_type"


def parse_except(error: subprocess.CalledProcessError) -> None:
    """Print a useful message for a failed child process."""
    if error.returncode < 0:
        print(f"Terminated by signal: {error}")
        return

    descriptions = {
        1: "general error",
        2: "command-line usage error",
        126: "cannot execute command",
        127: "command not found",
    }
    description = descriptions.get(error.returncode, "fatal error")
    detail = f"\n\t{error.stderr}" if error.stderr else ""
    print(f"ERR -- {description} (exit {error.returncode}){detail}")


def open_browser_wsl(url: str) -> None:
    """Open a URL using the host browser, including from WSL."""
    if os.environ.get("INSLIDE_CONTAINER", "false").lower() == "true":
        return
    if "microsoft-standard" in platform.uname().release.lower():
        subprocess.run(["powershell.exe", "-Command", f"Start-Process '{url}'"])
    else:
        import webbrowser

        webbrowser.open(url)


def kill_existing_flask(port: int = 5000) -> None:
    """Find and stop any process currently using the specified port."""
    try:
        pid = subprocess.check_output(["lsof", "-ti", f":{port}"]).decode().strip()
        if pid:
            print(f"Stopping existing process on port {port} (PID: {pid})...")
            subprocess.run(["kill", "-9", pid])
            time.sleep(1)
    except subprocess.CalledProcessError:
        pass


qc_app = None


def signal_handler(_sig, _frame) -> None:
    """Stop the QC subprocess when the pipeline receives Ctrl+C."""
    global qc_app
    print("\n\x1b[1mShutting down QC app...\x1b[0m")
    if qc_app:
        qc_app.terminate()
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)


def copytree_pure_data(src: Path, dst: Path) -> None:
    """Recursively copy file contents without preserving metadata."""
    if src.resolve() == dst.resolve():
        return
    for root, _dirs, files in os.walk(src):
        root_path = Path(root)
        relative_path = root_path.relative_to(src)
        target_dir = dst / relative_path
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename in files:
            shutil.copyfile(root_path / filename, target_dir / filename)


def build_stage_commands(args: argparse.Namespace) -> dict[int, list[object]]:
    """Build child commands, including only explicitly supplied overrides."""
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    mapping_csv = output_dir / "slide_mapping.csv"
    ocr_csv = output_dir / "ocr.csv"
    enriched_csv = output_dir / "enriched.csv"

    macro_command: list[object] = [
        sys.executable,
        "-u",
        SCRIPT_DIR / "1_get_macro.py",
        "--input_dir",
        input_dir,
        "--output_dir",
        output_dir,
    ]
    if args.input_mode:
        macro_command.extend(["--input-mode", args.input_mode])
    if args.macro_workers is not None:
        macro_command.extend(["--workers", str(args.macro_workers)])
    if args.macro_extensions:
        macro_command.extend(["--extensions", *args.macro_extensions])
    if args.macro_image_extensions:
        macro_command.extend(["--image-extensions", *args.macro_image_extensions])
    if args.macro_thumbnail_size:
        macro_command.extend(
            ["--thumbnail-size", *(str(value) for value in args.macro_thumbnail_size)]
        )

    ocr_command: list[object] = [
        sys.executable,
        "-u",
        SCRIPT_DIR / "2_run_dual_ocr.py",
        "--mapping_csv",
        mapping_csv,
        "--output_csv",
        ocr_csv,
    ]
    if args.ocr_workers is not None:
        ocr_command.extend(["--workers", str(args.ocr_workers)])
    if args.ocr_use_cpu:
        ocr_command.append("--use-cpu")

    naming_command: list[object] = [
        sys.executable,
        "-u",
        SCRIPT_DIR / "3_name-files.py",
        "--input_csv",
        ocr_csv,
        "--output_csv",
        enriched_csv,
    ]
    if args.naming_accession_pattern:
        naming_command.extend(
            ["--accession_pattern", args.naming_accession_pattern]
        )
    if args.naming_workers is not None:
        naming_command.extend(["--workers", str(args.naming_workers)])

    return {1: macro_command, 2: ocr_command, 3: naming_command}


def normalized_stage(value: str | None, default: int) -> int:
    aliases = {"1": 1, "macro": 1, "2": 2, "ocr": 2, "3": 3, "name": 3}
    if value is None:
        return default
    return aliases[value]


def run_stage(stage: int, command: list[object]) -> None:
    labels = {1: "1_get_macro.py", 2: "2_run_dual_ocr.py", 3: "3_name-files.py"}
    print(f"\n\x1b[1mExecuting {labels[stage]}...\x1b[0m\n", flush=True)
    subprocess.run(command, check=True, text=True)


def copy_app_bundle(output_dir: Path) -> Path:
    """Copy the QC app and pipeline launcher into a portable output bundle."""
    output_src = output_dir / "src"
    output_src.mkdir(parents=True, exist_ok=True)
    for filename in (
        "app.py",
        "pipeline.py",
        "1_get_macro.py",
        "2_run_dual_ocr.py",
        "3_name-files.py",
        "renaming.py",
        "openapi.json",
    ):
        source = SCRIPT_DIR / filename
        destination = output_src / filename
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
    copytree_pure_data(SCRIPT_DIR / "templates", output_src / "templates")
    copytree_pure_data(
        SCRIPT_DIR / "copath_utilities", output_src / "copath_utilities"
    )
    return output_src / "app.py"


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Label-Check pipeline. Runs preprocessing stages "
            "and optionally initializes the QC application."
        )
    )
    parser.add_argument("--input-dir", required=True, help="Input image directory")
    parser.add_argument("--output-dir", required=True, help="Pipeline output directory")
    parser.add_argument(
        "--run-type",
        choices=["nightly", "on_demand"],
        default="on_demand",
        help="Run category shown in the QC application",
    )
    parser.add_argument(
        "--start-from",
        choices=["1", "macro", "2", "ocr", "3", "name", "app"],
        help="Stage at which to start",
    )
    parser.add_argument(
        "--end-at",
        choices=["1", "macro", "2", "ocr", "3", "name", "app"],
        help="Stage at which to end",
    )

    macro = parser.add_argument_group("1_get_macro.py")
    macro.add_argument("--input-mode", choices=["auto", "slides", "images"])
    macro.add_argument("--macro-workers", type=int)
    macro.add_argument("--macro-extensions", nargs="+")
    macro.add_argument("--macro-image-extensions", nargs="+")
    macro.add_argument("--macro-thumbnail-size", nargs=2, type=int, metavar=("WIDTH", "HEIGHT"))

    ocr = parser.add_argument_group("2_run_dual_ocr.py")
    ocr.add_argument("--ocr-workers", type=int)
    ocr.add_argument("--ocr-use-cpu", action="store_true")

    naming = parser.add_argument_group("3_name-files.py")
    naming.add_argument("--naming-accession-pattern")
    naming.add_argument("--naming-workers", type=int)
    return parser


def main() -> int:
    args = create_parser().parse_args()
    start_from_app = args.start_from == "app"
    start_stage = 4 if start_from_app else normalized_stage(args.start_from, 1)
    end_at_app = args.end_at in (None, "app")
    end_stage = 4 if end_at_app else normalized_stage(args.end_at, 3)

    if start_stage > end_stage:
        print("ERR -- the ending stage cannot be earlier than the starting stage")
        return 2

    commands = build_stage_commands(args)
    try:
        for stage in range(max(1, start_stage), min(3, end_stage) + 1):
            run_stage(stage, commands[stage])
    except subprocess.CalledProcessError as error:
        parse_except(error)
        return error.returncode or 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / RUN_TYPE_FILE).write_text(args.run_type, encoding="utf-8")

    if end_stage <= 3:
        print(f"\n\x1b[1mEnding at stage {end_stage}.\x1b[0m", flush=True)
        return 0

    output_app = copy_app_bundle(output_dir)

    print("\n\x1b[1mInitializing database...\x1b[0m\n", flush=True)
    try:
        subprocess.run(
            [sys.executable, "-m", "flask", "--app", output_app, "init-db"],
            check=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        parse_except(error)
        return error.returncode or 1

    output_src = output_app.parent
    containerized = os.environ.get("INSLIDE_CONTAINER", "false").lower() == "true"
    if not containerized:
        kill_existing_flask()
    print("\n\x1b[1mOpening QC app...\x1b[0m\n", flush=True)
    global qc_app
    server_command = (
        [sys.executable, "-m", "waitress", "--listen=0.0.0.0:5000", "app:app"]
        if containerized
        else [sys.executable, "-m", "flask", "run", "--host", "0.0.0.0"]
    )
    qc_app = subprocess.Popen(
        server_command,
        cwd=output_src,
        env={
            **os.environ,
            "FLASK_APP": "app.py",
            "PYTHONPATH": str(output_src.resolve()),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
    )
    time.sleep(2)
    open_browser_wsl("http://127.0.0.1:5000")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
