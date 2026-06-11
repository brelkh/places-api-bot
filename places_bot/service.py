"""Shared restaurant-status lookup, used by both the CLI and the web app.

This is the single source of truth for turning a list of rows + a query column
into rows enriched with business-status columns. It handles de-duplication of
identical queries and can run lookups concurrently (the web app uses this to
finish a CSV inside the request timeout; the CLI runs it sequentially).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import processor
from .client import PlacesAPIError, PlacesClient


def full_query(raw_query: str, suffix: str) -> str:
    """The exact text sent to the API (raw query + disambiguating suffix)."""
    return f"{raw_query.strip()}{suffix}"


def _execute(
    fn: Callable[[str], dict[str, str]], queries: list[str], max_workers: int
) -> list[dict[str, str]]:
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
    dedupe: bool = True,
    max_workers: int = 1,
    on_result: Callable[[dict[str, str]], None] | None = None,
) -> int:
    """Enrich `rows` in place with status columns. Returns the API call count.

    Rows with an empty query get an error summary and cost no API call. When
    `dedupe` is True, identical queries are looked up once and shared.
    `on_result`, if given, is called with each row after it is filled (in row
    order), which the CLI uses for progress output.
    """

    def call(query: str) -> dict[str, str]:
        try:
            return processor.summarize_places(client.search_text(query))
        except PlacesAPIError as exc:
            return processor.error_summary(str(exc))

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

    for row in rows:
        raw = (row.get(query_col) or "").strip()
        if not raw:
            row.update(processor.error_summary("empty query"))
        elif dedupe:
            row.update(result_map[full_query(raw, suffix)])  # type: ignore[index]
        else:
            row.update(next(result_iter))
        if on_result is not None:
            on_result(row)

    return len(work)
