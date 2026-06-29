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

# Leading bytes of common non-CSV uploads we want to reject outright.
_BINARY_MAGIC = (
    b"PK\x03\x04",        # ZIP container → .xlsx / .ods
    b"%PDF",              # PDF
    b"\xd0\xcf\x11\xe0",  # OLE2 → legacy .xls / .doc
)


def looks_binary(raw: bytes) -> bool:
    """Heuristic: does this byte string look like a binary (non-CSV) file?

    True for known spreadsheet/document magic numbers, or any NUL byte in the
    first 8 KB (text CSVs never contain NUL — this also catches UTF-16, images,
    and other binaries).
    """
    if raw[:4] in _BINARY_MAGIC:
        return True
    return b"\x00" in raw[:8192]


def decode_csv_bytes(raw: bytes) -> str:
    """Decode uploaded CSV bytes to text, tolerating non-UTF-8 exports.

    Rejects binary (non-CSV) uploads with a clear ValueError, then decodes with
    a deterministic ladder: strict UTF-8 (a BOM is stripped), falling back to
    Windows-1252 — the common Excel-on-Windows export — so accented names are
    read correctly instead of producing mojibake. This mirrors the browser's
    client-side `readCsvText` (UTF-8 → windows-1252) so both paths behave the
    same. (Statistical detection, e.g. charset_normalizer, was tried but proved
    unreliable on the short inputs we get and diverged from the frontend.)

    Used by the web multipart path; the browser does the equivalent client-side
    before sending JSON chunks.
    """
    if not raw:
        return ""  # let the caller report the empty-CSV case
    if looks_binary(raw):
        raise ValueError(
            "This doesn't look like a CSV file (it appears to be a spreadsheet "
            "or other binary file). Export it as CSV and try again."
        )
    try:
        return raw.decode("utf-8-sig")  # strict UTF-8; drops a leading BOM
    except UnicodeDecodeError:
        # cp1252 leaves 5 byte values undefined; replace rather than raise.
        return raw.decode("cp1252", errors="replace")


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
    """Return (fieldnames, rows) from the input CSV file.

    Decodes through `decode_csv_bytes` (UTF-8→cp1252, binary-reject) so the CLI
    handles Excel exports and rejects spreadsheet/binary files the same way the
    web upload does.
    """
    with open(input_path, "rb") as fh:
        text = decode_csv_bytes(fh.read())
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
