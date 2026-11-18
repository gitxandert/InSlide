"""Search the batch catalog without changing it."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


DOCUMENT_VIEWS = {"name_mapping", "enriched"}
DOCUMENT_COLUMNS = ("BatchID", "BatchPath", "RowIndex")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--instance-dir", type=Path,
        default=Path(os.environ.get("INSTANCE_DIR", Path(__file__).parent / "instance")),
        help="directory containing batch_catalog.sqlite3",
    )
    result.add_argument("--table", default="name_mapping", help="catalog table or document view")
    result.add_argument("--list-tables", action="store_true", help="show available tables and views")
    result.add_argument("--show-columns", action="store_true", help="show columns for --table")
    result.add_argument("--find", help="case-insensitive substring to search")
    result.add_argument("--search-column", help="search only this column")
    result.add_argument("--columns", help="comma-separated columns to display")
    result.add_argument("--limit", type=int, default=100, help="maximum rows; 0 means all")
    result.add_argument(
        "--output", nargs="?", const="catalog_query.csv", type=Path,
        help="write CSV; default filename: catalog_query.csv",
    )
    return result


def available_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    if table in DOCUMENT_VIEWS:
        columns = list(DOCUMENT_COLUMNS)
        for (fields_json,) in connection.execute(
            "SELECT fields_json FROM batch_documents WHERE kind=?", (table,)
        ):
            columns.extend(json.loads(fields_json))
        if table == "name_mapping":
            columns.append("NameEdited")
        return list(dict.fromkeys(columns))
    quoted = '"' + table.replace('"', '""') + '"'
    return [row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")]


def records(connection: sqlite3.Connection, table: str):
    if table in DOCUMENT_VIEWS:
        for row in connection.execute(
            "SELECT b.public_id,b.relative_path,r.row_index,r.data_json "
            "FROM batch_document_rows r "
            "JOIN batches b ON b.id=r.batch_id "
            "WHERE r.kind=? ORDER BY b.relative_path,r.row_index",
            (table,),
        ):
            yield {
                "BatchID": row[0], "BatchPath": row[1], "RowIndex": row[2],
                **json.loads(row[3]),
            }
    else:
        quoted = '"' + table.replace('"', '""') + '"'
        for row in connection.execute(f"SELECT * FROM {quoted}"):
            yield dict(row)


def display(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    database = args.instance_dir / "batch_catalog.sqlite3"
    if not database.is_file():
        print(f"ERROR: catalog not found: {database}", file=sys.stderr)
        return 2
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            tables = sorted(DOCUMENT_VIEWS | {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            })
            if args.list_tables:
                print("\n".join(tables))
                return 0
            if args.table not in tables:
                raise ValueError(f"unknown table {args.table}; use --list-tables")
            fields = available_columns(connection, args.table)
            if args.show_columns:
                print("\n".join(fields))
                return 0
            selected = [field.strip() for field in args.columns.split(",")] if args.columns else fields
            unknown = [field for field in selected if field not in fields]
            if unknown:
                raise ValueError(f"unknown column(s): {', '.join(unknown)}; use --show-columns")
            if args.search_column and args.search_column not in fields:
                raise ValueError(f"unknown search column: {args.search_column}; use --show-columns")
            if args.limit < 0:
                raise ValueError("--limit must be 0 or greater")
            if args.output is None:
                handle = sys.stdout
            else:
                handle = os.fdopen(
                    os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                    "w", newline="", encoding="utf-8",
                )
            try:
                writer = csv.DictWriter(handle, fieldnames=selected, extrasaction="ignore")
                writer.writeheader()
                count = 0
                for row in records(connection, args.table):
                    if args.find is not None:
                        search_fields = [args.search_column] if args.search_column else fields
                        if not any(
                            args.find.casefold() in display(row.get(field)).casefold()
                            for field in search_fields
                        ):
                            continue
                    writer.writerow({field: display(row.get(field)) for field in selected})
                    count += 1
                    if args.limit and count >= args.limit:
                        break
            finally:
                if args.output is not None:
                    handle.close()
            if args.output is not None:
                print(f"Wrote {count} row(s) to {args.output}")
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
