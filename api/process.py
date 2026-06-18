"""Vercel serverless function for the Restaurant Status Lookup web app.

Endpoints (Flask WSGI app, served by Vercel as the module-level ``app``):

    GET  /api/fields    catalog of selectable output fields (no auth)
    POST /api/verify    check the shared password BEFORE any CSV is uploaded;
                        returns a short-lived token. Rate-limited per IP.
    POST /api/process   run the lookups (token- or password-gated)
                        • multipart/form-data: legacy single-shot CSV upload
                        • application/json: chunked JSON lookup mode used by
                          the browser's chunk loop (queries=[...], fields=[...])
                          or a one-time key probe (probe_only=true)

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
from places_bot import cache as place_cache, usage_store  # noqa: E402
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

    def check(self, key: str, n: int = 1) -> tuple[bool, int]:
        """Record n hits. Returns (allowed, retry_after_seconds).

        Stores one timestamp entry per unit (row). This allows the process
        limiter to count rows rather than requests.
        """
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            hits.extend([now] * n)
            self._hits[key] = hits
            if len(hits) > self.max_events:
                return False, int(self.window - (now - hits[0])) + 1
            return True, 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


# Wrong-password attempts: 5 per 10 minutes per IP.
_login_limiter = RateLimiter(max_events=5, window_seconds=600)
# Row budget: 5,000 rows per 10 minutes per IP (caps Vercel/API usage).
# Very large batches should use the CLI (uncapped).
_process_limiter = RateLimiter(max_events=5000, window_seconds=600)


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
    given = (
        request.headers.get("X-App-Password", "")
        or request.form.get("password", "")
        or (request.get_json(silent=True) or {}).get("password", "")
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


@app.get("/api/usage")
def usage_counter():
    """Shared monthly API-call count across all users (no auth, like /fields).

    Returns counts only — never the storage credentials. When the Upstash store
    isn't configured, reports ``storage="not_configured"`` so the UI can fall
    back gracefully.
    """
    return jsonify(usage_store.get_api_usage())


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

    if request.is_json:
        return _handle_process_json()
    return _handle_process_multipart()


# --------------------------------------------------------------------------- #
# JSON lookup mode (used by the browser's chunk loop)
# --------------------------------------------------------------------------- #
def _handle_process_json():
    data = request.get_json(silent=True) or {}

    # One-time key pre-check used by the browser before the chunk loop starts.
    # Not rate-limited — it's a single cheap IDs-only call.
    if data.get("probe_only"):
        chosen_key, key_used, key_warning, _, err = _choose_key_json(data)
        if err is not None:
            return err
        # Structured signal for the browser's "forward the user key?" decision —
        # robust vs. parsing the human-readable key_used string. True only when
        # the key that will actually be used IS the user's provided key.
        user_key = (data.get("api_key") or "").strip()
        used_user_key = bool(user_key) and chosen_key == user_key
        return jsonify(
            {
                "key_used": key_used,
                "key_warning": key_warning,
                "used_user_key": used_user_key,
            }
        )

    # Read-only cache pre-check used to refine the cost-confirmation modal: how
    # many of these queries are already cached (and so cost no API call). No
    # Google calls, no counter increment, no rate limit — just a Redis MGET.
    if data.get("cache_check"):
        names = data.get("queries") or []
        if not place_cache.is_enabled() or not names:
            return jsonify({"cache_enabled": place_cache.is_enabled(), "cached_count": 0})
        full = [service.full_query(n, config.DEFAULT_QUERY_SUFFIX) for n in names]
        cached = place_cache.get_many(full)
        return jsonify({"cache_enabled": True, "cached_count": len(cached)})

    queries = data.get("queries")
    if not isinstance(queries, list):
        return _error("'queries' must be a list of names.", 400)
    if not queries:
        return _error("'queries' is empty.", 400)
    if len(queries) > MAX_ROWS:
        return _error(
            f"Too many queries ({len(queries)}). Max per request is {MAX_ROWS}.",
            413,
        )

    ip = _client_ip()
    allowed, retry_after = _process_limiter.check(ip, n=len(queries))
    if not allowed:
        return _error(
            f"Rate limit reached. Try again in {retry_after} seconds.",
            429,
            retry_after=retry_after,
        )

    field_ids = data.get("fields") or None
    fields = fields_mod.resolve_fields(field_ids)

    chosen_key, key_used, key_warning, probe_calls, err = _choose_key_json(data)
    if err is not None:
        return err

    places_client = PlacesClient(api_key=chosen_key)
    # Browser already deduped; pass each name as its own pseudo-row.
    pseudo_rows = [{"q": name} for name in queries]
    summary = service.lookup_statuses(
        pseudo_rows,
        "q",
        suffix=config.DEFAULT_QUERY_SUFFIX,
        client=places_client,
        fields=fields,
        dedupe=False,
        max_workers=MAX_WORKERS,
        cache=place_cache,
    )

    results = []
    for name, row in zip(queries, pseudo_rows):
        result = {"query": name}
        result.update({k: v for k, v in row.items() if k != "q"})
        results.append(result)

    total_calls = summary.api_calls + probe_calls
    usage_store.increment_api_calls(total_calls)

    return jsonify(
        {
            "results": results,
            "api_calls": total_calls,
            "cache_hits": summary.cache_hits,
            "error_count": summary.error_count,
            "error_reasons": summary.error_reasons,
            "key_used": key_used,
            "key_warning": key_warning,
        }
    )


def _choose_key_json(data: dict):
    """Returns (api_key, key_used, key_warning, probe_calls, error_response)."""
    user_key = (data.get("api_key") or "").strip()
    server_key = os.environ.get(config.API_KEY_ENV_VAR, "").strip()

    if not user_key:
        if not server_key:
            return None, None, None, 0, _error(
                "No API key available. Provide your own key, or ask the owner "
                "to configure the server key.",
                500,
            )
        return server_key, "the app's key", None, 0, None

    # skip_probe=True means the browser already validated this key with a
    # probe_only call before the chunk loop; trust it without re-probing.
    if data.get("skip_probe"):
        return user_key, "your key", None, 0, None

    # Probe the key with one cheap IDs-only call.
    probe_error = service.probe_key(
        user_key,
        region_code=config.DEFAULT_REGION_CODE,
        language_code=config.DEFAULT_LANGUAGE_CODE,
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


# --------------------------------------------------------------------------- #
# Multipart (legacy single-shot CSV upload, kept for tests + fallback)
# --------------------------------------------------------------------------- #
def _handle_process_multipart():
    # --- read + validate the CSV first (needed to count rows for rate limit) ---
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

    ip = _client_ip()
    allowed, retry_after = _process_limiter.check(ip, n=len(rows))
    if not allowed:
        return _error(
            f"Rate limit reached. Try again in {retry_after} seconds.",
            429,
            retry_after=retry_after,
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

    places_client = PlacesClient(api_key=chosen_key)
    summary = service.lookup_statuses(
        rows, query_col, suffix=suffix, client=places_client, fields=fields,
        dedupe=True, max_workers=MAX_WORKERS, cache=place_cache,
    )

    total_calls = summary.api_calls + probe_calls
    usage_store.increment_api_calls(total_calls)

    status_counts = Counter(r.get("business_status_label", "") for r in rows)
    return jsonify(
        {
            "filename": "restaurant_status.csv",
            "query_column": query_col,
            "columns": processor.output_fieldnames(fieldnames, fields),
            "rows": rows,
            "csv": processor.rows_to_csv(fieldnames, rows, fields),
            "row_count": len(rows),
            "api_calls": total_calls,
            "cache_hits": summary.cache_hits,
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

    if not user_key:
        if not server_key:
            return None, None, None, 0, _error(
                "No API key available. Provide your own key, or ask the owner "
                "to configure the server key.",
                500,
            )
        return server_key, "the app's key", None, 0, None

    # User supplied a key: validate it with one cheap call before the batch.
    # Region/language match the batch defaults (this app is Singapore-focused).
    probe_error = service.probe_key(
        user_key,
        region_code=config.DEFAULT_REGION_CODE,
        language_code=config.DEFAULT_LANGUAGE_CODE,
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
