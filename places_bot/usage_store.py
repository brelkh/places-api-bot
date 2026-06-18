"""Shared, durable monthly API-call counter backed by Upstash Redis.

Unlike :mod:`places_bot.usage` (a per-machine local estimate for the CLI), this
module persists *one aggregate count across all users of the web app*. It lives
in Upstash Redis (a Redis-compatible HTTP store with a free tier that fits
Vercel hobby deployments) and is reached over its REST API with the ``requests``
dependency we already ship — no extra package, no SDK.

Two counters are kept:

    places_api_calls:<YYYY-MM>   calls made this calendar month (auto-rolls over:
                                 a new month is simply a new key, no reset job)
    places_api_calls:total       all-time calls

Everything here is **best-effort**. If the Upstash env vars are absent (e.g.
local dev) or the store is unreachable, reads report ``storage="not_configured"``
with null counts and writes are silently skipped — the app keeps working.

Env vars come from Vercel's KV/Upstash integration, whose names vary (and can
carry a user-chosen prefix, e.g. ``UPSTASH_REDIS_REST_KV_REST_API_URL``). We
accept the canonical names first, then fall back to a suffix match so any prefix
works:

    REST URL   : UPSTASH_REDIS_REST_URL, KV_REST_API_URL, or *…KV_REST_API_URL
    write token: UPSTASH_REDIS_REST_TOKEN, KV_REST_API_TOKEN, or *…KV_REST_API_TOKEN

The read-only token (``*…READ_ONLY_TOKEN``) is deliberately never used — it
can't run ``INCRBY``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import requests

MONTHLY_KEY_PREFIX = "places_api_calls:"
TOTAL_KEY = "places_api_calls:total"

# Keep requests snappy: a slow store must never stall a lookup response.
_TIMEOUT = 3.0


def _first_present(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _by_suffix(suffix: str, *, exclude: str = "") -> str:
    """First env var whose name ends with ``suffix`` (skipping any containing
    ``exclude``). Lets a prefixed integration (e.g. ``FOO_KV_REST_API_URL``)
    resolve without hardcoding the prefix."""
    for name, value in os.environ.items():
        if name.endswith(suffix) and (not exclude or exclude not in name):
            value = (value or "").strip()
            if value:
                return value
    return ""


def _config() -> tuple[str, str] | None:
    """Return ``(base_url, token)`` if Upstash is configured, else ``None``."""
    url = _first_present("UPSTASH_REDIS_REST_URL", "KV_REST_API_URL") or _by_suffix(
        "KV_REST_API_URL"
    )
    token = _first_present(
        "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_TOKEN"
    ) or _by_suffix("KV_REST_API_TOKEN", exclude="READ_ONLY")
    url = url.rstrip("/")
    if not url or not token:
        return None
    return url, token


def is_configured() -> bool:
    return _config() is not None


def current_month_key() -> tuple[str, str]:
    """Return ``(month, redis_key)`` for the current UTC month."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return month, f"{MONTHLY_KEY_PREFIX}{month}"


def _command(path: str) -> int | None:
    """Run one Upstash REST command (e.g. ``incrby/key/5``). Returns the integer
    result, or ``None`` if unconfigured / unreachable / non-numeric."""
    cfg = _config()
    if cfg is None:
        return None
    base, token = cfg
    try:
        resp = requests.post(
            f"{base}/{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json().get("result")
    except (requests.RequestException, ValueError):
        return None
    if result is None:
        return 0
    try:
        return int(result)
    except (TypeError, ValueError):
        return None


def increment_api_calls(count: int) -> None:
    """Add ``count`` Places API calls to the monthly and all-time counters.

    No-ops on non-positive counts or when the store is unconfigured/unreachable.
    """
    if count <= 0 or not is_configured():
        return
    _, month_key = current_month_key()
    _command(f"incrby/{month_key}/{count}")
    _command(f"incrby/{TOTAL_KEY}/{count}")


def get_api_usage() -> dict:
    """Return shared usage counts for the current month.

    Shape (safe to expose publicly — counts only, never credentials)::

        {"month": "2026-06", "monthly_api_calls": 123,
         "total_api_calls": 456, "storage": "upstash_redis"}

    When unconfigured, the counts are ``None`` and ``storage`` is
    ``"not_configured"``.
    """
    month, month_key = current_month_key()
    if not is_configured():
        return {
            "month": month,
            "monthly_api_calls": None,
            "total_api_calls": None,
            "storage": "not_configured",
        }
    monthly = _command(f"get/{month_key}")
    total = _command(f"get/{TOTAL_KEY}")
    return {
        "month": month,
        "monthly_api_calls": monthly or 0,
        "total_api_calls": total or 0,
        "storage": "upstash_redis",
    }
