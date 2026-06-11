"""Vercel serverless function: upload a restaurants CSV, get statuses back.

Exposed as a Flask WSGI app (Vercel serves the module-level ``app``). The
Google API key and the shared access password come from environment variables
configured in the Vercel dashboard:

    GOOGLE_MAPS_API_KEY   your server-side Places API key
    APP_PASSWORD          shared password that gates every request
    MAX_ROWS              optional, default 750 (protects the request timeout)
    PLACES_MAX_WORKERS    optional, default 8 (concurrent API calls)
"""

from __future__ import annotations

import hmac
import os
import sys
from collections import Counter

# Make the places_bot package importable when bundled by Vercel.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, request  # noqa: E402

from places_bot import config, processor, service  # noqa: E402
from places_bot.client import PlacesClient  # noqa: E402

app = Flask(__name__)

MAX_ROWS = int(os.environ.get("MAX_ROWS", "750"))
MAX_WORKERS = int(os.environ.get("PLACES_MAX_WORKERS", "8"))


def _password_ok() -> tuple[bool, str]:
    """Constant-time check of the shared password against APP_PASSWORD."""
    expected = os.environ.get("APP_PASSWORD", "")
    if not expected:
        return False, "Server is not configured: APP_PASSWORD is not set."
    given = request.headers.get("X-App-Password", "") or request.form.get(
        "password", ""
    )
    if not hmac.compare_digest(given, expected):
        return False, "Incorrect password."
    return True, ""


def _error(message: str, status: int):
    return jsonify({"error": message}), status


def _handle():
    ok, msg = _password_ok()
    if not ok:
        return _error(msg, 401)

    try:
        api_key = config.get_api_key()
    except RuntimeError as exc:
        return _error(str(exc), 500)

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return _error("No CSV uploaded (the form field must be named 'file').", 400)

    suffix = request.form.get("suffix", config.DEFAULT_QUERY_SUFFIX)
    query_column = request.form.get("query_column") or None

    try:
        text = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return _error("Could not read the file as a UTF-8 CSV.", 400)

    try:
        fieldnames, rows = processor.read_rows_from_text(text)
    except ValueError as exc:
        return _error(str(exc), 400)

    if not rows:
        return _error("The CSV has a header but no data rows.", 400)
    if len(rows) > MAX_ROWS:
        return _error(
            f"Too many rows ({len(rows)}). The hosted limit is {MAX_ROWS}. "
            f"Split the file or run the CLI locally for larger batches.",
            413,
        )

    query_col = query_column or processor.detect_query_column(fieldnames)
    if query_col not in fieldnames:
        return _error(
            f"Query column '{query_col}' not found. "
            f"Columns are: {', '.join(fieldnames)}",
            400,
        )

    client = PlacesClient(api_key=api_key)
    api_calls = service.lookup_statuses(
        rows,
        query_col,
        suffix=suffix,
        client=client,
        dedupe=True,
        max_workers=MAX_WORKERS,
    )

    summary = Counter(r.get("business_status_label", "") for r in rows)
    return jsonify(
        {
            "filename": "restaurant_status.csv",
            "query_column": query_col,
            "columns": processor.output_fieldnames(fieldnames),
            "rows": rows,
            "csv": processor.rows_to_csv(fieldnames, rows),
            "api_calls": api_calls,
            "row_count": len(rows),
            "summary": dict(summary),
        }
    )


# Vercel maps this file to the /api/process path; accept the root too so the
# function also works when invoked directly or run locally.
@app.post("/api/process")
def process_api():
    return _handle()


@app.post("/")
def process_root():
    return _handle()


if __name__ == "__main__":  # local dev: `python api/process.py`
    app.run(port=5000, debug=True)
