"""Vercel serverless function for the Restaurant Status Lookup web app.

Endpoints (Flask WSGI app, served by Vercel as the module-level ``app``):

    GET  /api/fields    catalog of selectable output fields (no auth)
    POST /api/verify    check the shared password BEFORE any CSV is uploaded;
                        returns a short-lived token. Rate-limited per IP.
    POST /api/process   run the lookups (token- or password-gated)

Environment variables (set in the Vercel dashboard):

    GOOGLE_MAPS_API_KEY   server-side Places API key (fallback key)
    APP_PASSWORD          shared password gating the app (also signs tokens)
    MAX_ROWS              optional, default 750
    PLACES_MAX_WORKERS    optional, default 8

Note on abuse protection: the rate limiter below is *in-memory and per
instance*. Vercel runs functions across many ephemeral instances, so this
slows a casual attacker but is not a hard guarantee. For real protection enable
Vercel's Firewall / Attack Challenge Mode (see README).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
import threading
import time
from collections import Counter

# Make the places_bot package importable when bundled by Vercel.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, request  # noqa: E402

from places_bot import config, fields as fields_mod, processor, service  # noqa: E402
from places_bot.client import PlacesClient  # noqa: E402

app = Flask(__name__)

MAX_ROWS = int(os.environ.get("MAX_ROWS", "750"))
MAX_WORKERS = int(os.environ.get("PLACES_MAX_WORKERS", "8"))
# Reject oversized uploads early (750 rows of names is well under this).
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # 4 MB

TOKEN_TTL_SECONDS = 30 * 60

REASON_TEXT = {
    "quota": "API quota / rate limit exceeded",
    "auth": "API key was rejected",
    "invalid_request": "the request was rejected as invalid",
    "network": "could not reach the Places API",
    "empty": "the row had no restaurant name",
    "unknown": "an unknown error occurred",
}


# --------------------------------------------------------------------------- #
# In-memory, per-instance rate limiter
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Best-effort sliding-window limiter. Not durable across instances."""

    def __init__(self, max_events: int, window_seconds: int) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Record a hit. Returns (allowed, retry_after_seconds)."""
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            hits.append(now)
            self._hits[key] = hits
            if len(hits) > self.max_events:
                return False, int(self.window - (now - hits[0])) + 1
            return True, 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


# Wrong-password attempts: 5 per 10 minutes per IP.
_login_limiter = RateLimiter(max_events=5, window_seconds=600)
# Total processing calls: 40 per 10 minutes per IP (caps Vercel/API usage).
_process_limiter = RateLimiter(max_events=40, window_seconds=600)


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.headers.get("X-Real-IP", "") or request.remote_addr or "unknown"


# --------------------------------------------------------------------------- #
# Auth (password + short-lived signed token)
# --------------------------------------------------------------------------- #
def _expected_password() -> str:
    return os.environ.get("APP_PASSWORD", "")


def _password_matches(given: str) -> bool:
    expected = _expected_password()
    return bool(expected) and hmac.compare_digest(given, expected)


def _sign(timestamp: int) -> str:
    secret = _expected_password().encode()
    return hmac.new(secret, str(timestamp).encode(), hashlib.sha256).hexdigest()


def _make_token() -> str:
    ts = int(time.time())
    return f"{ts}.{_sign(ts)}"


def _token_valid(token: str) -> bool:
    try:
        ts_str, sig = token.split(".", 1)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return False
    if time.time() - ts > TOKEN_TTL_SECONDS:
        return False
    return hmac.compare_digest(sig, _sign(ts))


def _is_authorized() -> bool:
    """Process endpoint accepts a valid token, or the password as a fallback."""
    token = request.headers.get("X-App-Token", "")
    if token and _token_valid(token):
        return True
    given = request.headers.get("X-App-Password", "") or request.form.get(
        "password", ""
    )
    return _password_matches(given)


def _error(message: str, status: int, **extra):
    return jsonify({"error": message, **extra}), status


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/api/fields")
def fields_catalog():
    return jsonify(
        {
            "fields": fields_mod.catalog_for_ui(),
            "note": "All fields are in the Places API Pro pricing tier.",
        }
    )


@app.post("/api/verify")
def verify():
    if not _expected_password():
        return _error("Server is not configured: APP_PASSWORD is not set.", 500)

    ip = _client_ip()
    given = (request.get_json(silent=True) or {}).get("password", "")
    if request.form.get("password"):
        given = request.form["password"]

    if _password_matches(given):
        _login_limiter.reset(ip)
        return jsonify({"ok": True, "token": _make_token()})

    allowed, retry_after = _login_limiter.check(ip)
    if not allowed:
        return _error(
            f"Too many incorrect attempts. Try again in {retry_after} seconds.",
            429,
            retry_after=retry_after,
        )
    return _error("Incorrect password.", 401)


@app.post("/api/process")
def process_api():
    return _handle_process()


# Vercel maps this file to /api/process; also accept root for local/direct use.
@app.post("/")
def process_root():
    return _handle_process()


def _handle_process():
    if not _is_authorized():
        return _error("Unauthorized. Enter the access password first.", 401)

    ip = _client_ip()
    allowed, retry_after = _process_limiter.check(ip)
    if not allowed:
        return _error(
            f"Rate limit reached. Try again in {retry_after} seconds.",
            429,
            retry_after=retry_after,
        )

    # --- read + validate the CSV ---
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return _error("No CSV uploaded (the form field must be named 'file').", 400)
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

    query_column = request.form.get("query_column") or None
    query_col = query_column or processor.detect_query_column(fieldnames)
    if query_col not in fieldnames:
        return _error(
            f"Column '{query_col}' not found. Your columns are: "
            f"{', '.join(fieldnames)}.",
            400,
        )

    suffix = request.form.get("suffix", config.DEFAULT_QUERY_SUFFIX)
    fields = fields_mod.resolve_fields(request.form.getlist("fields") or None)

    # --- pick which API key to use (optional user key, fallback to server) ---
    chosen_key, key_used, key_warning, probe_calls, err = _choose_key()
    if err is not None:
        return err

    client = PlacesClient(
        api_key=chosen_key, field_mask=fields_mod.build_field_mask(fields)
    )
    summary = service.lookup_statuses(
        rows, query_col, suffix=suffix, client=client, fields=fields,
        dedupe=True, max_workers=MAX_WORKERS,
    )

    status_counts = Counter(r.get("business_status_label", "") for r in rows)
    return jsonify(
        {
            "filename": "restaurant_status.csv",
            "query_column": query_col,
            "columns": processor.output_fieldnames(fieldnames, fields),
            "rows": rows,
            "csv": processor.rows_to_csv(fieldnames, rows, fields),
            "row_count": len(rows),
            "api_calls": summary.api_calls + probe_calls,
            "summary": dict(status_counts),
            "key_used": key_used,
            "key_warning": key_warning,
            "error_count": summary.error_count,
            "error_banner": _error_banner(summary, len(rows)),
        }
    )


def _choose_key():
    """Returns (api_key, key_used, key_warning, probe_calls, error_response)."""
    user_key = (request.form.get("api_key") or "").strip()
    server_key = os.environ.get(config.API_KEY_ENV_VAR, "").strip()
    region = request.form.get("region_code", config.DEFAULT_REGION_CODE)
    language = request.form.get("language_code", config.DEFAULT_LANGUAGE_CODE)

    if not user_key:
        if not server_key:
            return None, None, None, 0, _error(
                "No API key available. Provide your own key, or ask the owner "
                "to configure the server key.",
                500,
            )
        return server_key, "the app's key", None, 0, None

    # User supplied a key: validate it with one cheap call before the batch.
    probe_error = service.probe_key(
        user_key, region_code=region, language_code=language
    )
    if probe_error is None:
        return user_key, "your key", None, 1, None

    reason = REASON_TEXT.get(probe_error.reason, probe_error.reason)
    if server_key:
        warning = (
            f"Your API key wasn't used — {reason}. "
            f"Fell back to the app's key for this run."
        )
        return server_key, "the app's key (your key failed)", warning, 1, None
    return None, None, None, 1, _error(
        f"Your API key failed ({reason}) and the server has no fallback key.", 502
    )


def _error_banner(summary: service.LookupSummary, total: int) -> str | None:
    if not summary.error_count:
        return None
    top = max(summary.error_reasons, key=summary.error_reasons.get)
    msg = (
        f"{summary.error_count} of {total} lookups had problems "
        f"(most common: {REASON_TEXT.get(top, top)})."
    )
    if "quota" in summary.error_reasons:
        msg += " You may have hit your Google API quota or rate limit."
    if "auth" in summary.error_reasons:
        msg += " The API key was rejected."
    return msg


@app.errorhandler(413)
def _too_large(_e):
    return _error("Upload too large. Please send a smaller CSV.", 413)


if __name__ == "__main__":  # local dev: `python api/process.py`
    app.run(port=5000, debug=True)
