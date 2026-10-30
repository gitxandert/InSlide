"""
This script is the third and final step in a data processing pipeline for whole-slide images (WSIs).
It takes a CSV file enriched with OCR text (from the second script) and performs the final
data extraction and normalization.

The primary goals of this script are:
1.  **Parse OCR Text:** Use regular expressions (regex) to find and extract critical information,
    specifically the Accession ID and the Stain type, from the combined OCR text of the label
    and macro images.
2.  **Normalize Data:** Preserve accession IDs taken from semicolon-delimited filenames,
    normalize recognized OCR accession variants, and map OCR stain variations such as
    "H and E" and "H+E" to one canonical name ("H&E").
3.  **Enrich the CSV:** Append the parsed and normalized data as new columns to the CSV.
    It also adds flags indicating whether the parsing was successful and a column for
    manual quality control in a subsequent review tool.
4.  **Process Efficiently:** The script uses a thread pool for parallel processing, making it
    fast even with a large number of rows.

The final output is a clean, structured CSV file ready for use in a database or a manual
review application.
"""

# -----------------------------------------------------------------------------
# 1. IMPORTS
# -----------------------------------------------------------------------------
import argparse
import csv
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

from tqdm import tqdm  # For displaying a progress bar
from pipeline_events import EVENT_LOG_NAME, configure_pipeline_event_logging

# -----------------------------------------------------------------------------
# 2. Logging Configuration
# -----------------------------------------------------------------------------
# Set up a logger for informative console output with timestamps.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# 3. Configuration & Constants
# -----------------------------------------------------------------------------
# Define the names for the new columns that will be added to the output CSV.
COL_ACCESSION_ID = "AccessionID"
COL_STAIN = "Stain"
COL_BLOCK_NUMBER = "BlockNumber"
COL_EXTRACTION_SUCCESSFUL = "ExtractionSuccessful"
# This column is added for compatibility with the manual review tool. It starts empty.
COL_QC_PASSED = "ParsingQCPassed"

DEFAULT_ACCESSION_PATTERN = r"\b([A-Za-z]{1,3}\s*\d{2}\s*[ -/]\s*\d+)\b"
LOOSE_ACCESSION_PATTERN = re.compile(
    r"^\s*([A-Za-z]{1,3})\s*(\d{2})\s*[- /]\s*(\d+)\s*$"
)

# A comprehensive dictionary to correct common OCR errors and variations for stain names.
# The key is the "canonical" (standard) name, and the value is a list of all known
# variations that should be mapped to it. This can be easily extended or moved to an
# external config file (like JSON) for easier management.
STAIN_NAME_CORRECTIONS = {
    "HE": ["Hematoxylin", "Hematoxylir", "H and E", "H+E", "H-E", "HBE", "H&B_", "H8E", "H8E_1", "H8E_", "#&E", "HnE", "H8E", "HnBE", "H&E", "HeE", "H&E_1", "H&E_I","H&E_l", "H&E_"],
    "FS": ["FS", "{S"],
    "FS1": ["FS-1", "FS 1", "FS1.", "F-S1", "F-S-1", "{S1", "FS1"],
    "TPREP": ["T-PREP", "TPREP", "T PREP", "TP-REP", "TPREP."],
    "IDH": ["IDH1", "IDH-1", "IDHl", "lDH", "IDH.", "IDH"],
    "INI1": ["INI 1", "1NI 1", "IN1 1", "1N1 1", "1N1 I", "INI I", "INI_1", "1NI_1", "IN1_1", "1N1_1", "1N1_I", "INI_I", "1NI1", "IN11", "1N11", "1N1I", "INII", "INI1"],
    "ACTIN_1A4": ["ACTIN 1A4", "ACTIN_1AA", "ACTIN_1A4"],
    "ATRX": ["ATR-X", "ATRX", "ATR X", "ATRX.", "AT-RX"],
    "1P19Q": ["1P/19Q", "1P-19Q", "1P 19Q", "1P19-Q", "1P19Q.", "1P19Q"],
    "P16": ["P-16", "P 16", "P16.", "P1-6", "P-1-6", "016", "O16", "P16"],
    "P53": ["P-53", "P 53", "P53.", "P5-3", "P-5-3", "053", "O53", "P53"],
    "KI67": ["KI-67", "KI 67", "KI67.", "K167", "KI-6-7", "KI67"],
    "OLIG2": ["OLIG-2", "OLIG 2", "OLIG2.", "OL1G2", "OLIG-2.", "OLIG2"],
    "EGFR": ["E-GFR", "EGFR", "EGFR.", "E GFR", "EG-FR"],
    "MGMT": ["M-GMT", "MGMT", "MGMT.", "MGM-T", "M-G-MT"],
    "H3K27M": ["H3-K27M", "H3 K27M", "H3K27M.", "H3K-27M", "H3-K-27M", "H3K27M"],
    "GFAP-POLY": ["GFAP (polv)", "GFAP (poly)"],
    "GFAP": ["G-FAP", "GFAP", "GFAP.", "GFA-P", "G-F-AP"],
    "CD34": ["CD-34", "CD 34", "CD34.", "CD3-4", "C-D34", "CD34", "CD-3A", "CD 3A", "CD3A.", "CD3-A", "C-D3A", "CD3A"],
    "CD43": ["CD-43", "CD 43", "CD43.", "CD3-4", "C-D43", "CD43", "CD-A3", "CD A3", "CDA3.", "CD3-A", "C-DA3", "CDA3"],
    "CD31": ["CD-31", "CD 31", "CD31.", "CD3-1", "C-D31", "CD31", "CD-3I", "CD 3I", "CD3I.", "CD3-I", "C-D3I", "CD3I"],
    "CD68": ["CD-68", "CD 68", "CD68.", "C-D68", "C-D-68", "CD68"],
    "CD3": ["CD-3", "CD 3", "CD3.", "C-D3", "C-D-3", "CD3"],
    "CD20": ["CD-20", "CD 20", "CD20.", "C-D20", "C-D-20", "CD20", "CD-2O", "CD 2O", "CD2O.", "C-D2O", "C-D-2O", "CD2O"],
    "CD30": ["CD-30", "CD 30", "CD30.", "C-D30", "C-D-30", "CD30", "CD-Z0", "CD Z0", "CDZ0.", "C-DZ0", "C-D-Z0", "CDZ0", "CD-3O", "CD 3O", "CD3O.", "C-D3O", "C-D-3O", "CD3O", "CD-ZO", "CD ZO", "CDZO.", "C-DZO", "C-D-ZO", "CDZO"],
    "BRCA1": ["BRCA-1", "BRCA 1", "BRCA1.", "B-RCA1", "B-RCA-1", "BRCA1"],
    "HER2": ["HER-2", "HER 2", "HER2.", "H-ER2", "H-ER-2", "HER2"],
    "PTEN": ["P-TEN", "PTEN", "PTEN.", "P-T-EN", "P-T-EN."],
    "TP53": ["TP-53", "TP 53", "TP53.", "T-P53", "T-P-53", "TP53"],
    "CD45": ["CD-45", "CD 45", "CD45.", "C-D45", "C-D-45", "CD45"],
    "CD8": ["CD-8", "CD 8", "CD8.", "C-D8", "C-D-8", "CD8"],
    "CD4": ["CD-4", "CD 4", "CD4.", "C-D4", "C-D-4", "CD4"],
    "CD56": ["CD-56", "CD 56", "CD56.", "C-D56", "C-D-56", "CD56"],
    "CD1A": ["CDIA", "CDA", "CD1A"],
    "MUM-1": ["MUM 1", "MUM1", "MUM I", "MUMI", "MUM-I", "MUM-1"],
    "KRAS": ["K-RAS", "KRAS", "KRAS.", "K-RAS.", "K-RAS"],
    "NRAS": ["N-RAS", "NRAS", "NRAS.", "N-RAS.", "N-RAS"],
    "BRAF": ["B-RAF", "BRAF", "BRAF.", "B-RAF.", "B-RAF"],
    "CTNNB1": ["CTNNB-1", "CTNNB 1", "CTNNB1.", "C-TNNB1", "C-TNNB-1", "CTNNB1"],
    "ALK": ["A-LK", "ALK", "ALK.", "A-LK."],
    "LANGERIN": ["LANGERIM", "LARGERIN", "LARGERIM", "LANGERIN"],
    "HBA71-CD99": ["HBA.71 CD99", "HBA 71 CD99", "HBA71 CD99", "HBA.71 CD9g", "HBA 71 CD9g", "HBA71 CD9g", "HBA.71 CDg9", "HBA 71 CDg9", "HBA71 CDg9", "HBA.71 CDgg", "HBA 71 CDgg", "HBA71 CDgg", "HBA71_CD99"],
    "HMBA45": ["HMBA45"],
    "HMB-45-RED": ["HMB-45-RED"],
    "MELAN-A-RED": ["MELAN A-RED", "MELAN_A_RED"],
    "SOX-10": ["S0X-10", "S0X-1O", "SOX-1O", "SOX-10"],
    "SOX-10-RED": ["S0X-10-RED", "S0X-1O-RED", "SOX-1O-RED", "SOX-10-RED"],
    "MSH2-FE11": ["MSH? (FE1 1)", "MSH? (FE11)", "MSHZ (FE1 1)", "MSHZ (FE11)", "MSH2 (FE1 1)", "MSH2 (FE11)"],
    "MSH6-EP49": ["MSH6 (EPA9)", "MSH6 (EPA9}", "MSH6 (EP49)"],
    "WT-1": ["WT-1"],
    "PMS2-EP51": ["PMS2-EP51"],
    "NEUROFILAMENT": ["NEUROFILAMONT", "NEUROFILAMEN", "NEUROFILAMENT"],
    "TOXOPLASMA": ["TOXOPLASMA"],
    "RETIC": ["RETIC"],
    "NEUN": ["NEUN"],
    "PAS": ["PAS"],
    "NUT": ["NUT"],
    "SYNAPTOPHYSIN": ["SVNANTONHVSIN", "SYNAPTEPHYSIN", "SYNAPTEPBYSIS", "SVNAPTOPHVSIZ", "SYNAPTOPHYSIN"],
    "CKAE13": ["CK AE1/3", "CKAE13"],
    "MYOD1": ["MYOD1"],
    "DESMIN": ["DESMIN"],
    "S100": ["$100", "5100", "S100"],
    "CD14-EPR": ["CD1AEPR", "CD14EPR", "CD1A-EPR", "CD14-EPR"],
    "CD-163": ["CD 163", "CD-163"],
    "STAT6": ["STAT6"],
    "RECUT": ["RECUT"],
    "ANDROGEN": ["ANDROGEN"],
    "MLH1-ES05": ["MLH1 (ESO5)", "MLH1 (ES05)"],
    "EMA": ["EMA"],
    "ER": ["ER"],
    "PR-1294": ["PR 1294"],
}


# -----------------------------------------------------------------------------
# 4. Core Functions
# -----------------------------------------------------------------------------
def build_stain_normalizer(
    corrections: Dict[str, List[str]],
) -> Tuple[re.Pattern, Dict[str, str]]:
    """
    Builds a regex pattern and a lookup map for efficient stain name normalization.

    This function preprocesses the `STAIN_NAME_CORRECTIONS` dictionary to create two
    optimized data structures:
    1. A compiled regex pattern that can find any of the known stain variations in text.
    2. A lookup dictionary that maps any lowercase variation directly to its canonical form.

    Args:
        corrections (Dict[str, List[str]]): The dictionary of stain name corrections.

    Returns:
        Tuple[re.Pattern, Dict[str, str]]: A tuple containing the compiled regex
                                           pattern and the variation-to-canonical lookup map.
    """
    variation_lookup = {}
    all_variations = set()

    # Create a reverse mapping from any variation to its canonical name.
    for canonical, variations in corrections.items():
        # Use a set for efficient addition and to handle duplicates.
        current_variations = {v.lower() for v in variations}
        current_variations.add(canonical.lower())  # Add the canonical name itself as a variation.
        all_variations.update(current_variations)
        for var in current_variations:
            variation_lookup[var] = canonical

    # Sort variations by length in descending order. This is a crucial optimization for the
    # regex. It ensures that longer matches (e.g., "H3 K27M") are found before shorter
    # substrings (e.g., "H3"), preventing incorrect partial matches.
    sorted_variations = sorted(list(all_variations), key=len, reverse=True)

    # Join all variations into a single regex "OR" pattern (e.g., 'h-e|h\+e|h&e').
    # re.escape() is used to handle special characters like '+' correctly.
    # (?<!\w) means "not preceded by a word character"
    # (?!\w)  means "not followed by a word character"
    # ^^ This is used instead of \b, which would terminate a string at the first non-word character
    pattern_str = r"(?<!\w)(" + "|".join(re.escape(var) for var in sorted_variations) + r")(?!\w)"
    pattern = re.compile(pattern_str, re.IGNORECASE)

    return pattern, variation_lookup


def normalize_accession_id(value: str) -> str:
    """Normalize a recognized accession ID to the canonical A12-123 form."""
    candidate = (value or "").strip()
    match = LOOSE_ACCESSION_PATTERN.fullmatch(candidate)
    if not match:
        return candidate
    prefix, year, number = match.groups()
    return f"{prefix.upper()}{year}-{number}"


def process_csv_row(
    row: Dict[str, str],
    accession_pattern: re.Pattern,
    stain_pattern: re.Pattern,
    stain_lookup: Dict[str, str],
) -> Dict[str, any]:
    """

    Processes a single CSV row to find, extract, and normalize the accession ID and stain.

    This is the core worker function that is executed in parallel for each row of the CSV.

    Args:
        row (Dict[str, str]): A dictionary representing a single row from the input CSV.
        accession_pattern (re.Pattern): The compiled regex for finding accession IDs.
        stain_pattern (re.Pattern): The compiled regex for finding stain names.
        stain_lookup (Dict[str, str]): The map to convert a found stain variation to its canonical name.

    Returns:
        Dict[str, any]: The original row dictionary, updated with the new parsed columns.
    """
    updated_row = row.copy()
    accession_id = None
    canonical_stain = None
    block_number = None

    # Combine the OCR text from both label and macro images into a single string for searching.
    search_text = f"{row.get('label_text', '')} {row.get('macro_text', '')}"
    search_text = search_text.lower()

    # --- Step 1: Find and Normalize the Accession ID ---
    # first check if the path string already contains the accession ID
    file_path = row.get('original_slide_path', '')
    cleaned_path = re.sub(r'\\+', '/', file_path)
    file_name = cleaned_path.split('/')[-1]
    # accession ID will be the first field in the name;
    # if the QR code is unable to be read, the name is a random hash instead,
    # so only extract if the name is delimited by semicolons
    file_name_split = file_name.split(';')
    if len(file_name_split) > 1:
        accession_id = file_name_split[0].strip()
    else:
        # If no match is found, look in the label text
        accession_match = accession_pattern.search(search_text)
        # If a match is found, normalize it to a standard format (uppercase, hyphens, no spaces).
        if accession_match:
            accession_id = normalize_accession_id(accession_match.group(0))
            # Remove the match from the search text so that the regex doesn't attempt to find block number in the accession ID
            search_text = search_text.replace(accession_match.group(0), "")
        
    # --- Step 2: Find and Normalize the Stain Name ---
    stain_match = stain_pattern.search(search_text)
    if stain_match:
        found_variation = stain_match.group(0).lower()
        # Use the pre-built lookup map to get the canonical name.
        canonical_stain = stain_lookup.get(found_variation, found_variation)

    # --- Step 3: Find and Normalize Block Number
    block_pattern = re.compile(r"\b([A-Za-z]\s*[/]*\s*\d+)\b", re.IGNORECASE)
    
    # block numbers might match stain patterns (e.g. p16, p53),
    # so we need to find everything that looks like a block number and filter out
    all_potential_blocks = re.finditer(block_pattern, search_text)

    for match in all_potential_blocks:
        # 2. Check if this block match overlaps with the stain match
        # We check if the block start is within the stain range, or vice versa
        if stain_match and (match.start() < stain_match.end() and match.end() > stain_match.start()):
            continue  # It's a stain! Skip to the next match
        
        # 3. If we get here, it's a valid block
        block_number = match.group(0).replace("/", "").replace(" ", "").upper()
        break # Grab the first valid one and exit the loop

    # --- Step 4: Add the new data to the row dictionary ---
    updated_row[COL_ACCESSION_ID] = accession_id
    updated_row[COL_STAIN] = canonical_stain
    updated_row[COL_BLOCK_NUMBER] = block_number
    # The extraction is considered successful only if an ID, a stain, and a block number were found.
    updated_row[COL_EXTRACTION_SUCCESSFUL] = bool(
        accession_id
        and canonical_stain
        and block_number
    )
    # Initialize the QC_PASSED column as empty.
    updated_row[COL_QC_PASSED] = ""

    slide = row.get("original_slide_path") or None
    if updated_row[COL_EXTRACTION_SUCCESSFUL]:
        logger.info("Name extraction completed", extra={"slide": slide})
    else:
        missing = [
            name
            for name, value in (
                ("accession ID", accession_id),
                ("stain", canonical_stain),
                ("block number", block_number),
            )
            if not value
        ]
        logger.warning(
            f"Name extraction incomplete; missing {', '.join(missing)}",
            extra={"slide": slide},
        )

    return updated_row


def enrich_csv_with_parsing(
    input_path: Path, output_path: Path, accession_pattern_str: str, num_workers: int
):
    """
    Orchestrates the entire CSV enrichment process: reads the input, processes rows
    in parallel, and saves the final enriched CSV.

    Args:
        input_path (Path): Path to the input CSV file containing OCR text.
        output_path (Path): Path where the final, enriched CSV will be saved.
        accession_pattern_str (str): The regex pattern string for finding accession IDs.
        num_workers (int): The number of concurrent threads to use for processing.
    """
    if not input_path.exists():
        logger.error(f"Input file not found: {input_path}")
        return

    logger.info("Building regex patterns for data extraction...")
    try:
        # Compile the user-provided regex for accession IDs.
        accession_pattern = re.compile(accession_pattern_str, re.IGNORECASE)
        logger.info(f"Using accession pattern: {accession_pattern_str}")
    except re.error as e:
        logger.error(f"Invalid regex pattern for accession ID: '{accession_pattern_str}'. Error: {e}")
        return

    # Build the optimized stain pattern and lookup map.
    stain_pattern, stain_lookup = build_stain_normalizer(STAIN_NAME_CORRECTIONS)

    try:
        # Read the entire input CSV into memory.
        with open(input_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            original_rows = list(reader)
            original_headers = reader.fieldnames
    except Exception as e:
        logger.error(f"Failed to read input CSV '{input_path}': {e}")
        return

    if not original_rows:
        logger.warning("Input CSV is empty. No data to process.")
        return

    logger.info(f"Starting CSV enrichment for {len(original_rows)} rows using {num_workers} workers.")

    # Process all rows in parallel using a thread pool.
    updated_rows = []
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        # Schedule the processing of each row.
        future_to_row = {
            executor.submit(
                process_csv_row, row, accession_pattern, stain_pattern, stain_lookup
            ): row
            for row in original_rows
        }

        # Create a progress bar that updates as each row's processing completes.
        progress = tqdm(as_completed(future_to_row), total=len(original_rows), desc="Parsing rows")
        for future in progress:
            try:
                # Collect the result (the updated row) from the completed future.
                updated_rows.append(future.result())
            except Exception as e:
                row = future_to_row[future]
                logger.error(
                    f"Name extraction failed: {e}",
                    extra={"slide": row.get("original_slide_path") or None},
                )

    # Calculate summary statistics for logging.
    successful_extractions = sum(1 for row in updated_rows if row[COL_EXTRACTION_SUCCESSFUL])

    # Define the headers for the output CSV, ensuring new columns are added.
    # `dict.fromkeys` is a trick to get unique headers while preserving order.
    new_headers = (original_headers or []) + [
        COL_ACCESSION_ID,
        COL_STAIN,
        COL_BLOCK_NUMBER,
        COL_EXTRACTION_SUCCESSFUL,
        COL_QC_PASSED,
    ]
    final_headers = list(dict.fromkeys(new_headers))

    logger.info(f"Writing enriched data to '{output_path}'")
    try:
        # Write the final list of updated rows to the output CSV file.
        with open(output_path, mode="w", newline="", encoding="utf-8") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=final_headers)
            writer.writeheader()
            writer.writerows(updated_rows)

        # Log a final summary of the operation.
        logger.info("CSV enrichment complete.")
        logger.info(f"Successfully processed {len(updated_rows)} data rows.")
        logger.info(f"Successfully extracted ID, Stain, and Block Number in {successful_extractions} rows ({successful_extractions / len(updated_rows):.2%}).")

    except Exception as e:
        logger.exception(f"An unexpected error occurred while writing the output file: {e}")


# -----------------------------------------------------------------------------
# 5. Script Execution
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # Set up the command-line interface for the script.
    parser = argparse.ArgumentParser(
        description="Extracts Accession ID and Stain from OCR text in a CSV, normalizes them, "
        "and appends the results as new columns."
    )
    parser.add_argument(
        "--input_csv",
        type=Path,
        required=True,
        help="Path to the input CSV file enriched with OCR text.",
    )
    parser.add_argument(
        "--output_csv",
        type=Path,
        required=True,
        help="Path for the final, enriched output CSV file.",
    )
    parser.add_argument(
        "--accession_pattern",
        type=str,
        # A robust default regex that matches formats like 'NP 22-950' or 'NP22-123'.
        # \b ensures we match whole words only.
        # \s* allows for zero or more spaces.
        default=DEFAULT_ACCESSION_PATTERN,
        help="Regex pattern to extract the Accession ID.",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of worker threads for parallel processing.",
    )

    args = parser.parse_args()

    # Ensure the output directory exists before writing the file.
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    configure_pipeline_event_logging(
        logger, args.output_csv.parent / EVENT_LOG_NAME, "name"
    )

    # Run the main processing function with the provided arguments.
    try:
        enrich_csv_with_parsing(
            args.input_csv, args.output_csv, args.accession_pattern, args.workers
        )
    except Exception:
        logger.exception("Name stage failed")
        raise
