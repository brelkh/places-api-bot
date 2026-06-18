"""Shared restaurant-status lookup, used by both the CLI and the web app.

This is the single source of truth for turning a list of rows + a query column
into rows enriched with the selected output columns. It handles de-duplication
of identical queries and can run lookups concurrently (the web app uses this to
finish a CSV inside the request timeout; the CLI runs it sequentially).
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from . import processor
from .client import PlacesAPIError, PlacesClient
from .fields import FIELD_CATALOG, FieldSpec, build_details_field_mask


@dataclass
class LookupSummary:
    api_calls: int = 0
    error_count: int = 0
    error_reasons: dict[str, int] = field(default_factory=dict)
    # Queries served from the shared cache (0 Google calls). Web path only.
    cache_hits: int = 0


def full_query(raw_query: str, suffix: str) -> str:
    """The exact text sent to the API (raw query + disambiguating suffix)."""
    return f"{raw_query.strip()}{suffix}"


def _execute(
    fn: Callable[[str], tuple[dict[str, str], str | None]],
    queries: list[str],
    max_workers: int,
) -> list[tuple[dict[str, str], str | None]]:
    """Run `fn` over `queries`, preserving order. Concurrent if max_workers > 1."""
    if not queries:
        return []
    if max_workers <= 1:
        return [fn(q) for q in queries]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(queries))) as pool:
        return list(pool.map(fn, queries))


def lookup_statuses(
    rows: list[dict[str, str]],
    query_col: str,
    *,
    suffix: str,
    client: PlacesClient,
    fields: list[FieldSpec],
    dedupe: bool = True,
    max_workers: int = 1,
    on_result: Callable[[dict[str, str]], None] | None = None,
    cache=None,
) -> LookupSummary:
    """Enrich `rows` in place with the selected columns. Returns a LookupSummary.

    Rows with an empty query get an error summary and cost no API call. When
    `dedupe` is True, identical queries are looked up once and shared.
    `on_result`, if given, is called with each row after it is filled (in row
    order), which the CLI uses for progress output.

    `cache`, if given and enabled, is a day-cache (``places_bot.cache``) used by
    the web path: queries already cached cost no Google call, and freshly looked
    up places are stored for next time. Because a cached entry must satisfy any
    later field selection, the Place Details call fetches the **full Pro field
    set** when caching is on (same Pro-tier price as one field). The CLI passes
    no cache, so its behaviour is unchanged.
    """
    use_cache = cache is not None and cache.is_enabled()

    # With caching on, fetch the full Pro payload so a later request for other
    # fields still hits the cache; otherwise fetch only what was requested.
    call_fields = FIELD_CATALOG if use_cache else fields
    detail_mask = build_details_field_mask(call_fields)

    def raw_lookup(query: str):
        """Returns the place dict (found), None (no match), or a PlacesAPIError."""
        try:
            # Step 1: Text Search IDs-only (free tier) — get the place ID.
            id_results = client.search_text(query)
            if not id_results:
                return None
            place_id = id_results[0].get("id", "")
            if not place_id:
                return None
            # Step 2: Place Details (Pro tier) — fetch the fields.
            return client.get_place_details(place_id, detail_mask)
        except PlacesAPIError as exc:
            return exc

    def summarize(place) -> tuple[dict[str, str], str | None]:
        if isinstance(place, PlacesAPIError):
            return processor.error_summary(fields, str(place)), place.reason
        if place is None:
            return processor.summarize_places([], fields), None
        return processor.summarize_places([place], fields), None

    if use_cache:
        return _lookup_cached(
            rows, query_col, suffix, raw_lookup, summarize, cache,
            max_workers, on_result, fields,
        )

    # --- non-cached path (CLI + any caller without Redis); behaviour unchanged ---
    work: list[str] = []
    seen: set[str] = set()
    for row in rows:
        raw = (row.get(query_col) or "").strip()
        if not raw:
            continue
        query = full_query(raw, suffix)
        if dedupe and query in seen:
            continue
        seen.add(query)
        work.append(query)

    results = [summarize(p) for p in _execute(raw_lookup, work, max_workers)]
    result_map = dict(zip(work, results)) if dedupe else None
    result_iter = iter(results)

    reasons: Counter[str] = Counter()
    error_count = 0
    for row in rows:
        raw = (row.get(query_col) or "").strip()
        if not raw:
            summary, reason = processor.error_summary(fields, "empty query"), "empty"
        elif dedupe:
            summary, reason = result_map[full_query(raw, suffix)]  # type: ignore[index]
        else:
            summary, reason = next(result_iter)
        if reason:
            reasons[reason] += 1
            error_count += 1
        row.update(summary)
        if on_result is not None:
            on_result(row)

    return LookupSummary(
        api_calls=len(work), error_count=error_count, error_reasons=dict(reasons)
    )


def _lookup_cached(
    rows, query_col, suffix, raw_lookup, summarize, cache, max_workers,
    on_result, fields,
) -> LookupSummary:
    """Cache-backed variant: unique queries, MGET hits, look up only the misses,
    then store the freshly found places. `api_calls` counts misses only."""
    work: list[str] = []
    seen: set[str] = set()
    for row in rows:
        raw = (row.get(query_col) or "").strip()
        if not raw:
            continue
        query = full_query(raw, suffix)
        if query not in seen:
            seen.add(query)
            work.append(query)

    cached = cache.get_many(work)  # {query: place dict}
    misses = [q for q in work if q not in cached]
    fetched = dict(zip(misses, _execute(raw_lookup, misses, max_workers)))

    # Persist only successfully found places (skip not-found / errors).
    to_store = {q: p for q, p in fetched.items() if isinstance(p, dict)}
    cache.set_many(to_store)

    place_by_query = {**cached, **fetched}
    summary_by_query = {q: summarize(place_by_query.get(q)) for q in work}

    reasons: Counter[str] = Counter()
    error_count = 0
    for row in rows:
        raw = (row.get(query_col) or "").strip()
        if not raw:
            summary, reason = processor.error_summary(fields, "empty query"), "empty"
        else:
            summary, reason = summary_by_query[full_query(raw, suffix)]
        if reason:
            reasons[reason] += 1
            error_count += 1
        row.update(summary)
        if on_result is not None:
            on_result(row)

    return LookupSummary(
        api_calls=len(misses),
        error_count=error_count,
        error_reasons=dict(reasons),
        cache_hits=len(cached),
    )


def probe_key(
    api_key: str,
    *,
    region_code: str,
    language_code: str,
    query: str = "Starbucks",
) -> PlacesAPIError | None:
    """Validate an API key with one cheap IDs-only call.

    Returns None if the key works, or the PlacesAPIError (with `.reason`) if it
    doesn't — used to decide whether to fall back to another key.
    """
    client = PlacesClient(
        api_key=api_key,
        field_mask="places.id",  # cheapest (IDs-only) tier
        region_code=region_code,
        language_code=language_code,
        max_retries=2,
    )
    try:
        client.search_text(query)
        return None
    except PlacesAPIError as exc:
        return exc
