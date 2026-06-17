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
from .fields import FieldSpec, build_details_field_mask


@dataclass
class LookupSummary:
    api_calls: int = 0
    error_count: int = 0
    error_reasons: dict[str, int] = field(default_factory=dict)


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
) -> LookupSummary:
    """Enrich `rows` in place with the selected columns. Returns a LookupSummary.

    Rows with an empty query get an error summary and cost no API call. When
    `dedupe` is True, identical queries are looked up once and shared.
    `on_result`, if given, is called with each row after it is filled (in row
    order), which the CLI uses for progress output.
    """

    # Pre-compute the Place Details field mask once for all calls in this batch.
    detail_mask = build_details_field_mask(fields)

    def call(query: str) -> tuple[dict[str, str], str | None]:
        try:
            # Step 1: Text Search IDs-only (free tier) — get the place ID.
            id_results = client.search_text(query)
            if not id_results:
                return processor.summarize_places([], fields), None
            place_id = id_results[0].get("id", "")
            if not place_id:
                return processor.summarize_places([], fields), None
            # Step 2: Place Details (Pro tier) — fetch the requested fields.
            place = client.get_place_details(place_id, detail_mask)
            return processor.summarize_places([place], fields), None
        except PlacesAPIError as exc:
            return processor.error_summary(fields, str(exc)), exc.reason

    # Build the list of queries to actually send (deduped or one per row).
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

    results = _execute(call, work, max_workers)
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
