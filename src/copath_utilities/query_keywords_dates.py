import argparse
from datetime import date, timedelta
import re
import sys

import pandas as pd
import pyodbc

from copath_connection import prepared_connection_string


DIAGNOSIS_TEXT_TYPES = [
    ("$final", "final_diagnosis"),
    ("$adddx", "addendum_diagnosis"),
]

TEXT_FIELD_TO_TEXTTYPE = {
    column_name: texttype_id for texttype_id, column_name in DIAGNOSIS_TEXT_TYPES
}
TEXT_FIELDS = list(TEXT_FIELD_TO_TEXTTYPE)

SEARCH_TARGET_ALIASES = {
    **{column_name: column_name for _, column_name in DIAGNOSIS_TEXT_TYPES},
    **{f"ta.{column_name}": column_name for _, column_name in DIAGNOSIS_TEXT_TYPES},
}


DATE_ONLY_QUERY_TEMPLATE = """SELECT
    s.specnum_formatted AS accession_id,
    s.accession_date
FROM c_specimen s{where_clause}
ORDER BY s.accession_date DESC;
"""


SINGLE_WORD_QUERY_TEMPLATE = """WITH
{specimen_cte}hits AS (
    SELECT w.specimen_id
    FROM c_textword w{specimen_join}
    WHERE w.word IN ({word_literals})
      AND w.texttype_id IN ({texttype_literals})
    GROUP BY w.specimen_id
    HAVING {having}
)
SELECT
    s.specnum_formatted AS accession_id,
    s.accession_date
FROM hits h
JOIN {result_source} s
  ON s.specimen_id = CONVERT(VARCHAR(15), h.specimen_id)
ORDER BY s.accession_date DESC;
"""


MULTIWORD_QUERY_TEMPLATE = """WITH
{specimen_cte}paragraph_hits AS (
    SELECT
        w.specimen_id,
        w.texttype_id,
        w.para,
{term_column_sql}
    FROM c_textword w{specimen_join}
    WHERE w.word IN ({word_literals})
      AND w.texttype_id IN ({texttype_literals})
    GROUP BY w.specimen_id, w.texttype_id, w.para
),
hits AS (
    SELECT specimen_id
    FROM paragraph_hits
    GROUP BY specimen_id
    HAVING {having}
)
SELECT
    s.specnum_formatted AS accession_id,
    s.accession_date
FROM hits h
JOIN {result_source} s
  ON s.specimen_id = CONVERT(VARCHAR(15), h.specimen_id)
ORDER BY s.accession_date DESC;
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Query CoPath for indexed diagnosis words and return accession "
            "IDs and dates."
        )
    )
    parser.add_argument(
        "-k",
        "--keywords",
        help=(
            "Keyword expression using '&' for AND and '|' for OR; words within "
            "each term must occur in the same diagnosis paragraph"
        ),
    )
    parser.add_argument(
        "-a",
        "--after",
        help="Inclusive lower accession-date bound in YYYY-MM-DD format",
    )
    parser.add_argument(
        "-b",
        "--before",
        help="Inclusive upper accession-date bound in YYYY-MM-DD format",
    )
    parser.add_argument(
        "-i",
        "--in",
        dest="search_in",
        help=(
            "Optional comma-delimited diagnosis fields to search: "
            "final_diagnosis,addendum_diagnosis"
        ),
    )
    parser.add_argument(
        "-o",
        "--output_file",
        help="Output CSV filename (defaults to a name based on the filters)",
    )
    return parser.parse_args()


def parse_iso_date(raw_value: str | None, flag_name: str) -> date | None:
    if raw_value is None:
        return None
    try:
        return date.fromisoformat(raw_value)
    except ValueError as exc:
        raise ValueError(f"{flag_name} must be a valid date in YYYY-MM-DD format") from exc


def validate_filters(keywords: str | None, after: date | None, before: date | None) -> None:
    if not any([keywords, after, before]):
        raise ValueError("At least one filter is required: --keywords, --after, or --before")
    if after is not None and before is not None and after > before:
        raise ValueError("--after cannot be later than --before")


def parse_keyword_expression(expression: str) -> list[list[str]]:
    groups: list[list[str]] = []
    for raw_group in expression.split("|"):
        group = raw_group.strip()
        if group == "":
            raise ValueError("Keyword expression contains an empty OR group")
        terms = []
        for raw_term in group.split("&"):
            term = raw_term.strip()
            if term == "":
                raise ValueError("Keyword expression contains an empty AND term")
            terms.append(term)
        groups.append(terms)
    return groups


def parse_search_targets(raw_value: str | None) -> list[str] | None:
    if raw_value is None:
        return None

    targets = []
    seen = set()
    for raw_target in raw_value.split(","):
        target = raw_target.strip()
        if target == "":
            continue
        normalized = SEARCH_TARGET_ALIASES.get(target.lower())
        if normalized is None:
            valid_targets = ", ".join(sorted(SEARCH_TARGET_ALIASES))
            raise ValueError(
                f"Unsupported --in target '{target}'. "
                f"Valid targets include: {valid_targets}"
            )
        if normalized in seen:
            continue
        seen.add(normalized)
        targets.append(normalized)

    if not targets:
        raise ValueError("--in was provided, but no valid search targets were found")

    return targets


def escape_sql_literal(value: str) -> str:
    return value.replace("'", "''")


def sql_nvarchar_literal(value: str) -> str:
    return "N'" + escape_sql_literal(value) + "'"


def index_words(term: str) -> list[str]:
    words = []
    seen = set()
    for word in re.findall(r"[^\W_]+", term.lower(), flags=re.UNICODE):
        if word in seen:
            continue
        seen.add(word)
        words.append(word)
    if not words:
        raise ValueError(f"Keyword term {term!r} contains no searchable words")
    return words


def selected_search_fields(search_targets: list[str] | None) -> list[str]:
    return TEXT_FIELDS if search_targets is None else search_targets


def build_date_predicates(after: date | None, before: date | None) -> list[str]:
    predicates = []
    if after is not None:
        predicates.append(f"s.accession_date >= '{after.strftime('%Y%m%d')}'")
    if before is not None:
        exclusive_before = before + timedelta(days=1)
        predicates.append(
            f"s.accession_date <  '{exclusive_before.strftime('%Y%m%d')}'"
        )
    return predicates


def prepare_terms(
    keyword_groups: list[list[str]],
) -> tuple[list[tuple[int, list[str]]], list[list[int]]]:
    terms = []
    group_term_ids = []
    term_id = 0
    for group in keyword_groups:
        ids = []
        for term in group:
            term_id += 1
            ids.append(term_id)
            terms.append((term_id, index_words(term)))
        group_term_ids.append(ids)
    return terms, group_term_ids


def combine_group_conditions(
    group_term_ids: list[list[int]],
    term_conditions: dict[int, str],
) -> str:
    groups = []
    for term_ids in group_term_ids:
        conditions = [term_conditions[term_id] for term_id in term_ids]
        groups.append("(\n        " + "\n        AND ".join(conditions) + "\n    )")
    return "\n    OR ".join(groups)


def build_keyword_query(
    keyword_groups: list[list[str]],
    search_targets: list[str] | None,
    specimen_cte: str,
    specimen_source: str | None = None,
    result_source: str = "c_specimen",
) -> str:
    text_fields = selected_search_fields(search_targets)
    if not text_fields:
        raise ValueError("Keyword search has no searchable target fields")
    texttype_literals = ", ".join(
        sql_nvarchar_literal(TEXT_FIELD_TO_TEXTTYPE[field]) for field in text_fields
    )

    terms, group_term_ids = prepare_terms(keyword_groups)
    unique_words = list(dict.fromkeys(word for _, words in terms for word in words))
    word_literals = ", ".join(sql_nvarchar_literal(word) for word in unique_words)
    specimen_join = ""
    if specimen_source is not None:
        specimen_join = (
            f"\n    JOIN {specimen_source} s"
            "\n      ON s.specimen_id = CONVERT(VARCHAR(15), w.specimen_id)"
        )

    if all(len(words) == 1 for _, words in terms):
        term_conditions = {
            term_id: (
                "MAX(CASE WHEN w.word = "
                f"{sql_nvarchar_literal(words[0])} THEN 1 ELSE 0 END) = 1"
            )
            for term_id, words in terms
        }
        having = combine_group_conditions(group_term_ids, term_conditions)
        return SINGLE_WORD_QUERY_TEMPLATE.format(
            specimen_cte=specimen_cte,
            specimen_join=specimen_join,
            word_literals=word_literals,
            texttype_literals=texttype_literals,
            having=having,
            result_source=result_source,
        )

    term_columns = []
    for term_id, words in terms:
        word_checks = "\n            AND ".join(
            "MAX(CASE WHEN w.word = "
            f"{sql_nvarchar_literal(word)} THEN 1 ELSE 0 END) = 1"
            for word in words
        )
        term_columns.append(
            "        CASE WHEN "
            + word_checks
            + f"\n             THEN 1 ELSE 0 END AS term_{term_id}"
        )

    term_conditions = {
        term_id: f"MAX(term_{term_id}) = 1" for term_id, _ in terms
    }
    having = combine_group_conditions(group_term_ids, term_conditions)
    term_column_sql = ",\n".join(term_columns)
    return MULTIWORD_QUERY_TEMPLATE.format(
        specimen_cte=specimen_cte,
        term_column_sql=term_column_sql,
        specimen_join=specimen_join,
        word_literals=word_literals,
        texttype_literals=texttype_literals,
        having=having,
        result_source=result_source,
    )


def build_query(
    keywords: str | None,
    after: date | None,
    before: date | None,
    search_targets: list[str] | None,
) -> str:
    date_predicates = build_date_predicates(after, before)
    where_clause = ""
    if date_predicates:
        where_clause = "\nWHERE " + "\n  AND ".join(date_predicates)

    if keywords is None:
        return DATE_ONLY_QUERY_TEMPLATE.format(where_clause=where_clause)

    keyword_groups = parse_keyword_expression(keywords)
    specimen_cte = ""
    specimen_source = None
    result_source = "c_specimen"
    if date_predicates:
        specimen_source = "eligible_specimens"
        result_source = specimen_source
        date_predicate_sql = "\n      AND ".join(date_predicates)
        specimen_cte = f"""eligible_specimens AS (
    SELECT s.specimen_id, s.specnum_formatted, s.accession_date
    FROM c_specimen s
    WHERE {date_predicate_sql}
),
"""
    return build_keyword_query(
        keyword_groups,
        search_targets,
        specimen_cte,
        specimen_source,
        result_source,
    )


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "query"


def default_output_filename(
    keywords: str | None,
    after: date | None,
    before: date | None,
) -> str:
    components = ["copath"]
    if keywords:
        components.append(f"keywords_{slugify(keywords)[:40]}")
    if after:
        components.append(f"after_{after.isoformat()}")
    if before:
        components.append(f"before_{before.isoformat()}")
    return "_".join(components) + "_results.csv"


def main():
    args = parse_args()

    try:
        after = parse_iso_date(args.after, "--after")
        before = parse_iso_date(args.before, "--before")
        search_targets = parse_search_targets(args.search_in)
        validate_filters(args.keywords, after, before)
        query = build_query(args.keywords, after, before, search_targets)
    except Exception as exc:
        print(exc)
        return 1

    output_file = args.output_file or default_output_filename(
        args.keywords,
        after,
        before,
    )
    try:
        connection = pyodbc.connect(prepared_connection_string())
        results = pd.read_sql(query, connection)
    except Exception as exc:
        print(f"Error connecting to database: {exc}")
        return 1
    finally:
        if "connection" in locals():
            connection.close()

    try:
        results.to_csv(output_file, index=False)
    except Exception as exc:
        print(f"Error writing results: {exc}")
        return 1

    print(f"CoPath keyword/date results exported to {output_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
