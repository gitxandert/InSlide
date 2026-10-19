import argparse
import csv
import re
import sys

import pandas as pd
import pyodbc

from copath_connection import prepared_connection_string


DEFAULT_INSERT_BATCH_SIZE = 900
INVALID_ACCESSIONS_FILENAME = "invalid_accessions.csv"
ACCESSION_PATTERN = re.compile(r"^([A-Za-z]+)(\d{2})-(\d+)$")

QUERY = """
  SET NOCOUNT ON;
  DROP TABLE IF EXISTS #input_accessions;
  CREATE TABLE #input_accessions (
      accession_id VARCHAR(60) NOT NULL,
      numwheel_id VARCHAR(15) NOT NULL,
      specnum_year SMALLINT NOT NULL,
      specnum_num INT NOT NULL,
      PRIMARY KEY (numwheel_id, specnum_year, specnum_num)
  );

{insert_statements}

  WITH matched_specimens AS (
      SELECT
          i.accession_id AS requested_accession_id,
          s.specimen_id,
          s.specnum_formatted
      FROM #input_accessions i
      LEFT JOIN c_specimen s
          ON s.numwheel_id = i.numwheel_id
         AND s.specnum_year = i.specnum_year
         AND s.specnum_num = i.specnum_num
  )
  SELECT
      ms.specnum_formatted AS accession_id,
      part.part_designator AS part,
      block.blkdesig_label AS block,
      process.name AS stain,
      stain.slidecount AS slide_count,
      stain.label_text AS slide_label_text,
      status.name AS status
  FROM matched_specimens ms
  LEFT JOIN p_stainprocess stain
      ON stain.specimen_id = ms.specimen_id
  LEFT JOIN p_part part
      ON part.specimen_id = stain.specimen_id
     AND part.part_inst = stain.part_inst
  LEFT JOIN p_block block
      ON block.specimen_id = stain.specimen_id
     AND block.part_inst = stain.part_inst
     AND block.block_inst = stain.block_inst
  LEFT JOIN c_d_stainprocess process
      ON process.id = stain.stainprocess_id
  LEFT JOIN c_d_stainstatus status
      ON status.id = stain.stainstatus_id
  LEFT JOIN c_d_person ordered_by
      ON ordered_by.id = stain.orderedby_id
  ORDER BY
      accession_id,
      part.part_designator,
      block.blkdesig_label,
      stain.sequence,
      stain.stain_inst;
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Query CoPath for block-linked stain/process and slide details "
            "for a list of accession IDs."
        )
    )
    parser.add_argument(
        "input_file",
        help=(
            "Text file containing one accession ID per line, or a CSV file "
            "containing an accession-ID column"
        ),
    )
    parser.add_argument(
        "-c",
        "--column",
        help="Name of the accession-ID column when input_file is a CSV",
    )
    parser.add_argument(
        "-o",
        "--output_file",
        default="copath_slide_results.csv",
        help="Output CSV filename (defaults to copath_slide_results.csv)",
    )
    parser.add_argument(
        "-b",
        "--batch-size",
        type=int,
        default=DEFAULT_INSERT_BATCH_SIZE,
        help="Maximum accession IDs per INSERT ... VALUES block",
    )
    return parser.parse_args()


def process_input_file(file_path, target_column):
    with open(file_path, "r", newline="", encoding="utf-8") as input_file:
        if target_column is None:
            return [line.strip() for line in input_file if line.strip()]

        try:
            reader = csv.DictReader(input_file)
            fieldnames = reader.fieldnames
        except csv.Error as exc:
            raise ValueError(
                f"Error: {file_path} is not a standard CSV file"
            ) from exc

        if not fieldnames:
            raise ValueError(f"Error: {file_path} does not contain a header row")
        if target_column not in fieldnames:
            raise KeyError(
                f"Error: Column '{target_column}' not found. "
                f"Columns detected: {fieldnames}"
            )

        return [row[target_column] for row in reader]


def parse_accession_id(accession_id):
    normalized_accession_id = accession_id.strip()
    match = ACCESSION_PATTERN.fullmatch(normalized_accession_id)
    if match is None:
        raise ValueError("expected format PREFIXYY-NUMBER, e.g. SP25-0001")

    numwheel_id, two_digit_year, specnum_num = match.groups()
    year_value = int(two_digit_year)
    specnum_year = 1900 + year_value if year_value >= 80 else 2000 + year_value

    return {
        "accession_id": normalized_accession_id,
        "numwheel_id": numwheel_id.upper(),
        "specnum_year": specnum_year,
        "specnum_num": int(specnum_num),
    }


def split_valid_invalid_accessions(accession_ids):
    valid_accessions = []
    invalid_accessions = []
    seen_accession_keys = set()

    for accession_id in accession_ids:
        try:
            parsed_accession = parse_accession_id(accession_id)
        except ValueError as exc:
            invalid_accessions.append(
                {"accession_id": accession_id, "reason": str(exc)}
            )
            continue

        accession_key = (
            parsed_accession["numwheel_id"],
            parsed_accession["specnum_year"],
            parsed_accession["specnum_num"],
        )
        if accession_key in seen_accession_keys:
            continue

        seen_accession_keys.add(accession_key)
        valid_accessions.append(parsed_accession)

    return valid_accessions, invalid_accessions


def write_invalid_accessions_csv(
    invalid_accessions, output_path=INVALID_ACCESSIONS_FILENAME
):
    if not invalid_accessions:
        return None

    with open(output_path, "w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(
            output_file, fieldnames=["accession_id", "reason"]
        )
        writer.writeheader()
        writer.writerows(invalid_accessions)

    return output_path


def escape_sql_literal(value):
    return value.replace("'", "''")


def chunk_accessions(accessions, chunk_size):
    return [
        accessions[index:index + chunk_size]
        for index in range(0, len(accessions), chunk_size)
    ]


def format_accession_insert_statement(accessions):
    formatted_rows = [
        (
            f"('{escape_sql_literal(accession['accession_id'])}', "
            f"'{escape_sql_literal(accession['numwheel_id'])}', "
            f"{accession['specnum_year']}, "
            f"{accession['specnum_num']})"
        )
        for accession in accessions
    ]
    values_block = ",\n  ".join(formatted_rows)
    return (
        "  INSERT INTO #input_accessions "
        "(accession_id, numwheel_id, specnum_year, specnum_num)\n"
        "  VALUES\n"
        f"  {values_block};"
    )


def build_query(accessions, batch_size=DEFAULT_INSERT_BATCH_SIZE):
    if batch_size < 1:
        raise ValueError("Error: batch size must be at least 1")
    if not accessions:
        raise ValueError("Error: no valid accession IDs were found in the input")

    insert_statements = "\n\n".join(
        format_accession_insert_statement(chunk)
        for chunk in chunk_accessions(accessions, batch_size)
    )
    return QUERY.format(insert_statements=insert_statements)


def main():
    args = parse_args()
    invalid_accessions_csv = None

    try:
        accession_ids = process_input_file(args.input_file, args.column)
        valid_accessions, invalid_accessions = split_valid_invalid_accessions(
            accession_ids
        )
        invalid_accessions_csv = write_invalid_accessions_csv(invalid_accessions)
        formatted_query = build_query(valid_accessions, args.batch_size)
    except (OSError, KeyError, ValueError) as exc:
        print(exc)
        if invalid_accessions_csv is not None:
            print(f"Wrote invalid accession IDs to {invalid_accessions_csv}")
        return 1

    try:
        connection = pyodbc.connect(prepared_connection_string())
        results = pd.read_sql(formatted_query, connection)
    except Exception as exc:
        print(f"Error connecting to database: {exc}")
        return 1
    finally:
        if "connection" in locals():
            connection.close()

    try:
        results.to_csv(args.output_file, index=False)
    except Exception as exc:
        print(f"Error writing results: {exc}")
        return 1

    print(f"CoPath slide data exported to {args.output_file}")
    if invalid_accessions_csv is not None:
        print(f"Wrote invalid accession IDs to {invalid_accessions_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
