"""Find CoPath accession IDs by patient name."""

import argparse
import csv
import os
import sys
import tempfile
from pathlib import Path

import pyodbc

from copath_connection import CoPathConfigurationError, prepared_connection_string


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--last-name", required=True, help="Exact patient last name")
    parser.add_argument("--first-name", help="Exact patient first name")
    parser.add_argument("-o", "--output-file", default="copath_name_results.csv")
    args = parser.parse_args(argv)

    last_name = args.last_name.strip()
    first_name = args.first_name.strip() if args.first_name is not None else None
    if not last_name or first_name == "":
        parser.error("patient names must not be blank")

    query = """SELECT
    pd.patdemog_id,
    pd.firstname AS first_name,
    pd.middlename AS middle_name,
    pd.lastname AS last_name,
    pd.date_of_birth,
    s.specnum_formatted AS accession_id,
    s.accession_date
FROM r_pat_demograph pd
JOIN c_specimen s ON s.patdemog_id = pd.patdemog_id
WHERE pd.lastname = ?"""
    params = [last_name]
    if first_name is not None:
        query += "\n  AND pd.firstname = ?"
        params.append(first_name)
    query += "\nORDER BY pd.lastname, pd.firstname, pd.date_of_birth, s.accession_date DESC;"

    connection = None
    temporary_path = None
    try:
        connection = pyodbc.connect(prepared_connection_string())
        cursor = connection.cursor()
        cursor.execute(query, *params)
        columns = [column[0] for column in cursor.description]
        destination = Path(args.output_file)
        with tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8", dir=destination.parent,
            prefix=f".{destination.name}.", delete=False,
        ) as output:
            temporary_path = output.name
            writer = csv.writer(output)
            writer.writerow(columns)
            writer.writerows(cursor)
        os.replace(temporary_path, destination)
        temporary_path = None
    except (OSError, csv.Error, pyodbc.Error, CoPathConfigurationError) as exc:
        print(f"CoPath name query failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if temporary_path is not None:
            Path(temporary_path).unlink(missing_ok=True)
        if connection is not None:
            connection.close()

    print(f"CoPath name results exported to {args.output_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
