"""Web-only day-cache for Place Details payloads.

Shares the same Upstash Redis store as the usage counter
(:mod:`places_bot.usage_store`) — no second database. Each entry holds the
**full Pro-tier place payload** as JSON with a TTL (``PLACE_CACHE_TTL`` seconds,
default 24h), so a later request asking for *different* fields still hits the
cache and is filtered down at response time. A cache hit therefore costs **zero**
Google Places API calls.

Best-effort, exactly like the counter: every operation is a no-op when Redis
isn't configured (the CLI / local dev never set the Upstash env vars), so it can
never break or slow down a lookup. Reads are batched with ``MGET`` (one Redis
command per chunk) and writes with a single pipeline call.
"""

from __future__ import annotations

import json
import os

import requests

from . import usage_store

KEY_PREFIX = "place_cache:"
DEFAULT_TTL_SECONDS = 24 * 60 * 60
_TIMEOUT = 3.0


def is_enabled() -> bool:
    """Caching is available only when the shared Redis store is configured."""
    return usage_store.is_configured()


def _ttl_seconds() -> int:
    raw = os.environ.get("PLACE_CACHE_TTL", "").strip()
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError:
        return DEFAULT_TTL_SECONDS
    return ttl if ttl > 0 else DEFAULT_TTL_SECONDS


def _key(query: str) -> str:
    """Cache key for a full query string (name + suffix), normalised so trivial
    case/whitespace differences share an entry."""
    return KEY_PREFIX + " ".join(query.lower().split())


def _request(path: str, body: list):
    """POST a command (``path=""``) or pipeline (``path="/pipeline"``) to the
    Upstash REST API. Returns parsed JSON, or ``None`` if unreachable."""
    cfg = usage_store._config()
    if cfg is None:
        return None
    base, token = cfg
    try:
        resp = requests.post(
            base + path,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def get_many(queries: list[str]) -> dict[str, dict]:
    """Return ``{query: place_payload}`` for the queries currently cached.

    One ``MGET`` for the whole list; missing/garbled entries are simply absent.
    """
    if not queries or not is_enabled():
        return {}
    data = _request("", ["MGET", *[_key(q) for q in queries]])
    if not data:
        return {}
    results = data.get("result") or []
    out: dict[str, dict] = {}
    for query, raw in zip(queries, results):
        if not raw:
            continue
        try:
            out[query] = json.loads(raw)
        except (ValueError, TypeError):
            continue
    return out


def set_many(places: dict[str, dict]) -> None:
    """Cache ``{query: place_payload}`` with the configured TTL (one pipeline)."""
    if not places or not is_enabled():
        return
    ttl = str(_ttl_seconds())
    commands = [
        ["SET", _key(q), json.dumps(payload), "EX", ttl]
        for q, payload in places.items()
    ]
    _request("/pipeline", commands)
