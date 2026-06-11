"""CSV reading, result summarisation, and CSV writing."""

from __future__ import annotations

import csv
import io
from typing import Any, Iterable

# Google Maps' raw businessStatus values -> friendly labels.
BUSINESS_STATUS_LABELS = {
    "OPERATIONAL": "Open",
    "CLOSED_TEMPORARILY": "Temporarily closed",
    "CLOSED_PERMANENTLY": "Permanently closed",
}

# Columns this tool appends to each input row.
OUTPUT_COLUMNS = [
    "business_status",  # raw Google value, or NOT_FOUND / ERROR
    "business_status_label",  # human-friendly version
    "matched_name",  # so you can confirm the right place was matched
    "matched_address",
    "google_maps_uri",
]

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


def summarize_places(places: list[dict[str, Any]]) -> dict[str, str]:
    """Turn the API's places list into the flat columns we output."""
    if not places:
        return {
            "business_status": "NOT_FOUND",
            "business_status_label": "Not found",
            "matched_name": "",
            "matched_address": "",
            "google_maps_uri": "",
        }

    place = places[0]  # Text Search returns best match first.
    raw_status = place.get("businessStatus", "")
    if raw_status:
        label = BUSINESS_STATUS_LABELS.get(raw_status, raw_status)
    else:
        # Place matched but Google has no business status for it.
        raw_status = "UNKNOWN"
        label = "Unknown"

    return {
        "business_status": raw_status,
        "business_status_label": label,
        "matched_name": (place.get("displayName") or {}).get("text", ""),
        "matched_address": place.get("formattedAddress", ""),
        "google_maps_uri": place.get("googleMapsUri", ""),
    }


def error_summary(message: str) -> dict[str, str]:
    return {
        "business_status": "ERROR",
        "business_status_label": f"Error: {message}",
        "matched_name": "",
        "matched_address": "",
        "google_maps_uri": "",
    }


def output_fieldnames(fieldnames: Iterable[str]) -> list[str]:
    """Input columns plus any OUTPUT_COLUMNS not already present."""
    out_fields = list(fieldnames)
    for col in OUTPUT_COLUMNS:
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


def rows_to_csv(fieldnames: Iterable[str], rows: list[dict[str, str]]) -> str:
    """Serialise rows to CSV text, appending OUTPUT_COLUMNS that aren't present."""
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=output_fieldnames(fieldnames), extrasaction="ignore"
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
    output_path: str, fieldnames: list[str], rows: list[dict[str, str]]
) -> None:
    """Write rows to a file, appending OUTPUT_COLUMNS that aren't present."""
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        fh.write(rows_to_csv(fieldnames, rows))
