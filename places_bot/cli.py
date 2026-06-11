"""Command-line entry point: restaurants.csv -> restaurant_status.csv."""

from __future__ import annotations

import argparse
import sys

from . import config, processor, service
from .client import PlacesClient
from .usage import UsageTracker


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="places_bot",
        description=(
            "Look up Google Maps business status for a list of restaurants. "
            "Reads a CSV of queries and writes a copy with status columns added."
        ),
    )
    p.add_argument("-i", "--input", default="restaurants.csv", help="Input CSV path.")
    p.add_argument(
        "-o", "--output", default="restaurant_status.csv", help="Output CSV path."
    )
    p.add_argument(
        "--query-column",
        default=None,
        help="Column holding the restaurant query. Auto-detected if omitted.",
    )
    p.add_argument(
        "--suffix",
        default=config.DEFAULT_QUERY_SUFFIX,
        help=f'Text appended to every query (default: "{config.DEFAULT_QUERY_SUFFIX}").',
    )
    p.add_argument("--region-code", default=config.DEFAULT_REGION_CODE)
    p.add_argument("--language-code", default=config.DEFAULT_LANGUAGE_CODE)
    p.add_argument(
        "--threshold",
        type=int,
        default=config.DEFAULT_CALL_THRESHOLD,
        help="Warn if this month's estimated calls would exceed this number.",
    )
    p.add_argument(
        "--usage-file",
        default=config.DEFAULT_USAGE_FILE,
        help="Where to store the local monthly call-count estimate.",
    )
    p.add_argument(
        "--no-dedupe",
        action="store_true",
        help="Call the API for every row, even duplicate queries (costs more).",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt when over the cost threshold.",
    )
    return p


def _confirm_threshold(args, planned_calls: int, tracker: UsageTracker) -> bool:
    """Return True if it's OK to proceed given the cost threshold."""
    already = tracker.current_month_count()
    projected = already + planned_calls
    if projected <= args.threshold:
        return True

    print(
        f"\n⚠️  Cost warning: this run plans ~{planned_calls} API call(s).\n"
        f"   Estimated calls already made this month (local count): {already}\n"
        f"   Projected month total: {projected} (threshold: {args.threshold}).\n"
        f"   Note: the authoritative count lives in the Google Cloud console.",
        file=sys.stderr,
    )
    if args.yes:
        print("   Proceeding because --yes was passed.", file=sys.stderr)
        return True
    if not sys.stdin.isatty():
        print(
            "   Not a TTY and --yes was not passed; aborting to be safe.",
            file=sys.stderr,
        )
        return False
    answer = input("   Proceed anyway? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        api_key = config.get_api_key()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        fieldnames, rows = processor.read_rows(args.input)
    except (OSError, ValueError) as exc:
        print(f"Error reading {args.input}: {exc}", file=sys.stderr)
        return 2

    if not rows:
        print(f"No rows found in {args.input}; nothing to do.", file=sys.stderr)
        return 0

    query_col = args.query_column or processor.detect_query_column(fieldnames)
    if query_col not in fieldnames:
        print(
            f"Error: query column '{query_col}' not found. "
            f"Available columns: {', '.join(fieldnames)}",
            file=sys.stderr,
        )
        return 2
    print(f"Using '{query_col}' as the query column.", file=sys.stderr)

    # Work out the unique queries so we can estimate cost before running.
    queries = [
        service.full_query(r.get(query_col) or "", args.suffix)
        for r in rows
        if (r.get(query_col) or "").strip()
    ]
    unique_queries = list(dict.fromkeys(queries))
    planned_calls = len(queries) if args.no_dedupe else len(unique_queries)

    tracker = UsageTracker(args.usage_file)
    if not _confirm_threshold(args, planned_calls, tracker):
        print("Aborted.", file=sys.stderr)
        return 1

    client = PlacesClient(
        api_key=api_key,
        region_code=args.region_code,
        language_code=args.language_code,
    )

    total = len(rows)
    progress = {"n": 0}

    def on_result(row: dict[str, str]) -> None:
        progress["n"] += 1
        raw_query = (row.get(query_col) or "").strip() or "(empty)"
        print(
            f"  [{progress['n']}/{total}] {raw_query} -> "
            f"{row['business_status_label']}",
            file=sys.stderr,
        )

    calls_made = service.lookup_statuses(
        rows,
        query_col,
        suffix=args.suffix,
        client=client,
        dedupe=not args.no_dedupe,
        max_workers=1,  # sequential keeps CLI progress output in order
        on_result=on_result,
    )

    try:
        processor.write_rows(args.output, fieldnames, rows)
    except OSError as exc:
        print(f"Error writing {args.output}: {exc}", file=sys.stderr)
        return 2

    month_total = tracker.add(calls_made)
    print(
        f"\nDone. Wrote {len(rows)} rows to {args.output}.\n"
        f"API calls this run: {calls_made}. "
        f"Estimated calls this month (local): {month_total}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
