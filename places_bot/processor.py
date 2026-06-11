"""CSV reading, result summarisation, and CSV writing.

Output columns are driven by the selected fields (see `places_bot.fields`), so
`summarize_places`, `error_summary`, `output_fieldnames`, `rows_to_csv`, and
`write_rows` all take a list of `FieldSpec`.
"""

from __future__ import annotations

import csv
import io
from typing import Iterable

from . import fields as fields_mod
from .fields import FieldSpec

# Candidate column names to auto-detect the query column (case-insensitive).
QUERY_COLUMN_CANDIDATES = ("query", "restaurant", "restaurant_name", "name")


def detect_query_column(fieldnames: Iterable[str]) -> str:
    """Pick which input column holds the restaurant query."""
    names = list(fieldnames)
    if not names:
        raise ValueError("Input CSV has no header row / columns.")
    lower = {n.lower(): n for n in names}
    for candidate in QUERY_COLUMN_CANDIDATES:
        if candidate in lower:
            return lower[candidate]
    # Fall back to the first column.
    return names[0]


def _blank_summary(
    fields: list[FieldSpec], status: str, label: str
) -> dict[str, str]:
    """Output row for the no-match / error cases: status set, rest blank."""
    out: dict[str, str] = {}
    for field in fields:
        if field.id == fields_mod.STATUS_FIELD_ID:
            out["business_status"] = status
            out["business_status_label"] = label
        else:
            for col in field.columns:
                out[col] = ""
    return out


def summarize_places(
    places: list[dict], fields: list[FieldSpec]
) -> dict[str, str]:
    """Turn the API's places list into the chosen output columns."""
    if not places:
        return _blank_summary(fields, "NOT_FOUND", "Not found")

    place = places[0]  # Text Search returns best match first.
    out: dict[str, str] = {}
    for field in fields:
        out.update(field.extract(place))
    return out


def error_summary(fields: list[FieldSpec], message: str) -> dict[str, str]:
    """Output row for a lookup that raised (status ERROR + the reason)."""
    return _blank_summary(fields, "ERROR", f"Error: {message}")


def output_fieldnames(
    fieldnames: Iterable[str], fields: list[FieldSpec]
) -> list[str]:
    """Input columns plus the selected fields' columns not already present."""
    out_fields = list(fieldnames)
    for col in fields_mod.field_columns(fields):
        if col not in out_fields:
            out_fields.append(col)
    return out_fields


def read_rows_from_text(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Parse CSV text into (fieldnames, rows). Shared by the CLI and web app."""
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("CSV appears to be empty.")
    fieldnames = list(reader.fieldnames)
    rows = [dict(row) for row in reader]
    return fieldnames, rows


def rows_to_csv(
    fieldnames: Iterable[str], rows: list[dict[str, str]], fields: list[FieldSpec]
) -> str:
    """Serialise rows to CSV text with the selected fields' columns appended."""
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=output_fieldnames(fieldnames, fields),
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def read_rows(input_path: str) -> tuple[list[str], list[dict[str, str]]]:
    """Return (fieldnames, rows) from the input CSV file."""
    with open(input_path, newline="", encoding="utf-8-sig") as fh:
        text = fh.read()
    try:
        return read_rows_from_text(text)
    except ValueError:
        raise ValueError(f"{input_path} appears to be empty.")


def write_rows(
    output_path: str,
    fieldnames: list[str],
    rows: list[dict[str, str]],
    fields: list[FieldSpec],
) -> None:
    """Write rows to a file with the selected fields' columns appended."""
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        fh.write(rows_to_csv(fieldnames, rows, fields))
