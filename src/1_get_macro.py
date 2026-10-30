"""
This script extracts associated images (macro, label, thumbnail) from whole-slide image
(WSI) files or ingests already-extracted label/macro image directories. In both modes it
produces the same CSV schema for the downstream OCR and QC stages.
"""

import argparse
import csv
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pipeline_events import EVENT_LOG_NAME, configure_pipeline_event_logging

try:
    import openslide
except ImportError:
    openslide = None

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **_kwargs):
        return iterable


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

ASSOCIATED_IMAGE_TYPES = ["macro", "label", "thumbnail"]
DEFAULT_THUMBNAIL_SIZE = (300, 300)
DEFAULT_IMAGE_EXTENSIONS = ["png", "jpg", "jpeg", "tif", "tiff", "bmp"]
DEFAULT_SLIDE_EXTENSIONS = [
    "svs",
    "ndpi",
    "mrxs",
    "scn",
    "svslide",
    "tif",
    "tiff",
    "bif",
    "vms",
    "vmu",
]


def write_mapping_csv(results: list[dict], csv_path: Path):
    """
    Writes the stage-1 mapping CSV shared by both input modes.
    """
    logger.info(f"Writing mapping to {csv_path}...")
    with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
        header = [f"{img_type}_path" for img_type in ASSOCIATED_IMAGE_TYPES] + [
            "original_slide_path"
        ]
        writer = csv.DictWriter(csvfile, fieldnames=header)
        writer.writeheader()

        for result in results:
            row = {}
            for img_type in ASSOCIATED_IMAGE_TYPES:
                row[f"{img_type}_path"] = result.get(img_type) or ""
            row["original_slide_path"] = result["original"]
            writer.writerow(row)

    logger.info(f"CSV mapping saved to {csv_path}")


def extract_associated_images(
    svs_path: Path,
    input_dir: Path,
    output_dir: Path,
    maintain_structure: bool = True,
    thumbnail_size: tuple = DEFAULT_THUMBNAIL_SIZE,
):
    """
    Extracts associated images (macro, label, thumbnail) from a single WSI file.
    """
    slide = None
    try:
        if openslide is None:
            raise ImportError(
                "openslide is required for slide mode but is not installed."
            )
        slide = openslide.OpenSlide(str(svs_path))
        base_name = svs_path.stem
        output_paths = {"original": svs_path}

        if maintain_structure:
            relative_sub_dir = svs_path.parent.relative_to(input_dir)
        else:
            relative_sub_dir = Path()

        for image_type in ASSOCIATED_IMAGE_TYPES:
            image_output_dir = output_dir / image_type / relative_sub_dir
            image_output_dir.mkdir(parents=True, exist_ok=True)

            output_filename = f"{base_name}_{image_type}.png"
            image_path = image_output_dir / output_filename

            if image_type in slide.associated_images:
                slide.associated_images[image_type].save(image_path)
                logger.debug(f"Extracted {image_type} from {svs_path}")
            elif image_type == "thumbnail":
                logger.warning(
                    "No embedded thumbnail; generating one",
                    extra={"slide": str(svs_path)},
                )
                slide.get_thumbnail(thumbnail_size).save(image_path)
            else:
                image_path = None

            output_paths[image_type] = image_path

        if output_paths["label"] is None:
            raise RuntimeError("slide has no embedded label image")
        logger.info("Slide extraction completed", extra={"slide": str(svs_path)})
        return output_paths
    except Exception as e:
        logger.error(
            f"Slide extraction failed: {e}", extra={"slide": str(svs_path)}
        )
        return None
    finally:
        if slide is not None:
            slide.close()


def process_slide_files(
    input_dir: Path,
    output_dir: Path,
    csv_path: Path,
    extensions: list,
    num_workers: int,
    thumbnail_size: tuple,
):
    """
    Finds and processes WSI files recursively using a thread pool.
    """
    logger.info(f"Scanning for files with extensions {extensions} in {input_dir}...")

    slide_files = []
    for ext in extensions:
        slide_files.extend(list(input_dir.rglob(f"*.{ext}")))
        slide_files.extend(list(input_dir.rglob(f"*.{ext.upper()}")))

    slide_files = sorted(set(slide_files))
    if not slide_files:
        raise RuntimeError("No slide files found to process.")

    logger.info(f"Found {len(slide_files)} slide files to process.")
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        future_to_svs = {
            executor.submit(
                extract_associated_images,
                svs_path,
                input_dir,
                output_dir,
                thumbnail_size=thumbnail_size,
            ): svs_path
            for svs_path in slide_files
        }

        progress_bar = tqdm(
            as_completed(future_to_svs),
            total=len(slide_files),
            desc="Processing slides",
        )
        for future in progress_bar:
            result = future.result()
            if result:
                results.append(result)

    if results:
        write_mapping_csv(results, csv_path)
    logger.info(
        f"Successfully processed {len(results)} out of {len(slide_files)} files."
    )
    if len(results) != len(slide_files):
        raise RuntimeError(
            f"Failed to process {len(slide_files) - len(results)} of "
            f"{len(slide_files)} slide files."
        )


def normalize_image_stem(path: Path, image_type: str) -> str:
    """
    Normalizes a label or macro filename stem for cross-directory pairing.
    """
    suffix = f"_{image_type}"
    stem = path.stem
    if stem.lower().endswith(suffix):
        stem = stem[: -len(suffix)]
    if path.parent == Path():
        return stem
    return str(path.parent / stem)


def scan_image_dir(
    image_dir: Path, image_type: str, extensions: list[str]
) -> dict[str, Path]:
    """
    Scans one image subtree and returns normalized stem -> source path mappings.
    """
    files = []
    for ext in extensions:
        files.extend(list(image_dir.rglob(f"*.{ext}")))
        files.extend(list(image_dir.rglob(f"*.{ext.upper()}")))

    matches = {}
    for file_path in sorted(set(files)):
        relative_path = file_path.relative_to(image_dir)
        normalized_stem = normalize_image_stem(relative_path, image_type)
        if normalized_stem in matches:
            logger.warning(
                f"Duplicate {image_type} image for '{normalized_stem}'; keeping "
                f"{matches[normalized_stem]} and skipping {file_path}",
                extra={"slide": str(file_path)},
            )
            continue
        matches[normalized_stem] = file_path
    return matches


def build_canonical_output_path(
    output_dir: Path,
    image_type: str,
    normalized_stem: str,
    source_path: Path,
) -> Path:
    """
    Builds the copied image path for image-directory mode.
    """
    stem_path = Path(normalized_stem)
    filename = f"{stem_path.name}_{image_type}{source_path.suffix.lower()}"
    return output_dir / image_type / stem_path.parent / filename


def process_image_files(
    input_dir: Path,
    output_dir: Path,
    csv_path: Path,
    extensions: list[str],
):
    """
    Pairs label/macro images from an image-directory input tree and copies them to
    the standard output layout.
    """
    label_dir = input_dir / "label"
    macro_dir = input_dir / "macro"
    if not label_dir.is_dir() or not macro_dir.is_dir():
        raise RuntimeError(
            "Image-directory mode requires both 'label/' and 'macro/' subdirectories."
        )

    logger.info(f"Scanning image directories in {input_dir}...")
    normalized_extensions = [ext.lower().lstrip(".") for ext in extensions]
    label_images = scan_image_dir(label_dir, "label", normalized_extensions)
    macro_images = scan_image_dir(macro_dir, "macro", normalized_extensions)

    if not label_images:
        raise RuntimeError("No label images found to process.")

    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for normalized_stem in sorted(label_images):
        label_source = label_images[normalized_stem]
        macro_source = macro_images.get(normalized_stem)

        label_output = build_canonical_output_path(
            output_dir, "label", normalized_stem, label_source
        )
        label_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(label_source, label_output)

        macro_output = None
        if macro_source:
            macro_output = build_canonical_output_path(
                output_dir, "macro", normalized_stem, macro_source
            )
            macro_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(macro_source, macro_output)
        else:
            logger.warning(
                "No matching macro image",
                extra={"slide": str(Path(normalized_stem).with_suffix(".svs"))},
            )

        results.append(
            {
                "label": label_output,
                "macro": macro_output,
                "thumbnail": None,
                "original": Path(normalized_stem).with_suffix(".svs"),
            }
        )
        logger.info(
            "Image inputs copied",
            extra={"slide": str(Path(normalized_stem).with_suffix(".svs"))},
        )

    write_mapping_csv(results, csv_path)
    logger.info(f"Successfully processed {len(results)} label-driven image rows.")


def detect_input_mode(input_dir: Path, requested_mode: str) -> str:
    """
    Chooses the effective stage-1 input mode.
    """
    if requested_mode != "auto":
        return requested_mode
    if (input_dir / "label").is_dir() and (input_dir / "macro").is_dir():
        return "images"
    return "slides"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract associated images from whole-slide files or paired image directories."
    )
    parser.add_argument(
        "--input_dir",
        type=Path,
        required=True,
        help="Input directory containing slide files or label/macro image subdirectories.",
    )
    parser.add_argument(
        "--output_dir",
        type=Path,
        required=True,
        help="Output directory for extracted or copied images.",
    )
    parser.add_argument(
        "--csv-path",
        type=Path,
        default=Path("slide_mapping.csv"),
        help="Path to save the CSV mapping file.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of worker threads for parallel processing.",
    )
    parser.add_argument(
        "--extensions",
        nargs="+",
        default=DEFAULT_SLIDE_EXTENSIONS,
        help="List of slide-file extensions to process in slide mode.",
    )
    parser.add_argument(
        "--input-mode",
        choices=["auto", "slides", "images"],
        default="auto",
        help="Interpret the input as whole-slide files or label/macro image directories.",
    )
    parser.add_argument(
        "--image-extensions",
        nargs="+",
        default=DEFAULT_IMAGE_EXTENSIONS,
        help="List of raster image extensions to process in image-directory mode.",
    )
    parser.add_argument(
        "--thumbnail-size",
        type=int,
        nargs=2,
        default=DEFAULT_THUMBNAIL_SIZE,
        metavar=("WIDTH", "HEIGHT"),
        help=(
            "Size of thumbnails to generate if not present. "
            f"Default: {DEFAULT_THUMBNAIL_SIZE[0]} {DEFAULT_THUMBNAIL_SIZE[1]}"
        ),
    )

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    csv_path = output_dir / args.csv_path
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    configure_pipeline_event_logging(logger, output_dir / EVENT_LOG_NAME, "macro")
    input_mode = detect_input_mode(args.input_dir, args.input_mode)

    try:
        if input_mode == "slides":
            process_slide_files(
                args.input_dir,
                output_dir,
                csv_path,
                args.extensions,
                args.workers,
                tuple(args.thumbnail_size),
            )
        else:
            process_image_files(
                args.input_dir,
                output_dir,
                csv_path,
                args.image_extensions,
            )
    except Exception:
        logger.exception("Macro stage failed")
        raise
