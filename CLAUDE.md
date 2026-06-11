# CLAUDE.md

Ramp-up notes for working in this repo. Read this first — it should save you
from having to scan the whole codebase.

## What this is

Look up the Google Maps **business status** (open / temporarily closed /
permanently closed) for a list of restaurants. Ships two front-ends over one
shared engine:

- **CLI** — `restaurants.csv` → `restaurant_status.csv`.
- **Web app** — upload a CSV, see a results table, download the CSV. Hosted on
  Vercel at https://places-api-bot.vercel.app/ (production branch: `main`).

## Architecture in 30 seconds

```
CLI (places_bot/cli.py) ─┐
                         ├─► places_bot/service.py  ──►  places_bot/client.py ──► Google Places API
Web (api/process.py)  ───┘     lookup_statuses()           PlacesClient.search_text()
                                     │
                                     └─► places_bot/processor.py  (CSV in/out, status labels)
```

`places_bot/service.py::lookup_statuses()` is the **single source of truth** for
the lookup. Both the CLI and the web function call it. **Do not duplicate lookup
logic** — extend the service instead.

Data flow: parse CSV rows → `lookup_statuses` builds the de-duplicated set of
queries (each = `"<name>" + suffix`, suffix defaults to `" singapore"`) → calls
`client.search_text` (concurrently in the web path) → `processor.summarize_places`
maps the API result to output columns → rows are written back to CSV.

## File map

| Path | What lives here |
| --- | --- |
| `places_bot/fields.py` | **Field catalog** — Pro-tier `FieldSpec`s, `resolve_fields`, `build_field_mask`, `field_columns`, status labels. The whitelist that caps the pricing tier. |
| `places_bot/config.py` | Constants + `get_api_key()`. `FIELD_MASK` (default selection), endpoint URL, defaults, threshold. |
| `places_bot/client.py` | `PlacesClient` — Text Search call, retry/backoff, **thread-local sessions**. `PlacesAPIError.reason` + `classify_error` (quota/auth/invalid_request/network). |
| `places_bot/service.py` | `lookup_statuses()` (dedupe + concurrency, returns `LookupSummary` with error reasons) and `probe_key()`. The shared engine. |
| `places_bot/processor.py` | CSV read/write (file + in-memory). `summarize_places`/`error_summary`/`output_fieldnames`/`rows_to_csv` all take `fields`. `detect_query_column`. |
| `places_bot/cli.py` | argparse CLI (`--fields`), cost-threshold prompt, progress, usage tracking, error summary. |
| `places_bot/usage.py` | `UsageTracker` — best-effort local monthly call counter (`.places_usage.json`). |
| `api/process.py` | Flask function for Vercel. 3 routes: `GET /api/fields`, `POST /api/verify` (password→token, rate-limited), `POST /api/process`. User-key fallback via `probe_key`. In-memory `RateLimiter`. |
| `public/index.html` | Vanilla-JS single-page UI (no build step). Verifies password first, then uploads. |
| `vercel.json` | Bundles `places_bot/**` with the function, 60s `maxDuration`. |
| `tests/` | `test_processor.py`, `test_service.py`, `test_cli.py`, `test_web.py`. All stub the network. |
| `.github/workflows/places-status.yml` | `workflow_dispatch` CI run that uploads the output CSV as an artifact. |

## Invariants — don't break these

1. **Output fields stay in the Pro pricing tier.** All selectable fields live in
   `places_bot/fields.py::FIELD_CATALOG` (IDs-only/Pro only). Callers pick fields
   **by id**, and `resolve_fields` ignores unknown ids — so a request can never
   escalate into the pricier **Enterprise** tier (opening hours, phone, rating,
   website). To add a field: add a `FieldSpec` with its mask + extractor, after
   verifying it is Pro/IDs-only. Never add an Enterprise field here.
2. **One engine.** CLI and web both go through `service.lookup_statuses`, which is
   field-driven (`summarize_places`/`error_summary`/CSV helpers all take the
   resolved `fields`). Keep it that way.
3. **Concurrency safety.** The web path runs `search_text` across threads. The
   client keeps a `requests.Session` per thread (`threading.local`). Don't add
   shared mutable state across threads (the rate limiter uses a lock).
4. **Secrets via env, never committed.** `GOOGLE_MAPS_API_KEY` (CLI + web) and
   `APP_PASSWORD` (web; also signs the verify token). Local: `.env` / shell.
   Vercel: dashboard env vars. CI: GitHub Secrets. `.env` and
   `restaurant_status.csv` are git-ignored.
5. **Dedupe = cost control.** Identical queries are looked up once. `LookupSummary.
   api_calls` ≤ rows (+1 if a user key is probed).
6. **Auth before upload.** The browser calls `POST /api/verify` first and only
   uploads the CSV on success; the server still re-checks (token or password) on
   `POST /api/process`. Keep this order — don't let `/api/process` parse a body
   before authorizing.

## Commands

```bash
# Tests (stub the network — no key/quota needed)
pip install -r requirements-dev.txt
pytest

# CLI
export GOOGLE_MAPS_API_KEY="your-key"
python -m places_bot -i restaurants.csv -o restaurant_status.csv

# Web app locally
export GOOGLE_MAPS_API_KEY="your-key" APP_PASSWORD="local-pass"
python api/process.py            # API on http://localhost:5000
# full static+function experience: `npm i -g vercel && vercel dev`
```

Deploy is automatic: Vercel redeploys on push to `main`. Python 3.10+ (code uses
`X | None` syntax).

## Change workflow — required for every change

After making a code change, do these in order:

1. **Add/update tests** if the change adds or alters behavior (tests live in
   `tests/`, stub the network — see existing ones for the monkeypatch pattern).
2. **Regression test** — run the full `pytest` suite and confirm it passes.
3. **Update `README.md`** if user-facing behavior, commands, options, or setup
   changed.
4. **Update this `CLAUDE.md`** if the architecture, file map, invariants, or
   workflow changed.

## Conventions & gotchas

- **Commits:** author as `Claude <noreply@anthropic.com>` (a stop-hook enforces
  this; set `git config user.email noreply@anthropic.com` / `user.name Claude`).
- **Frontend has no build step** — plain HTML/CSS/JS in `public/index.html`.
  Vercel serves `public/` at `/` and `api/*.py` as functions automatically.
- **`MAX_ROWS` (default 750)** caps a single web upload so it finishes inside the
  60s Vercel timeout. Large batches → split, or use the CLI (uncapped).
- **Output columns** come from the selected `FieldSpec`s (`fields.field_columns`);
  the web UI renders whatever columns come back. To add output, add a `FieldSpec`
  in `fields.py` (see invariant #1).
- **Pushing:** on the user's local machine the git remote is `github.com` over
  HTTPS with no stored creds — the user pushes; an in-session sandbox pushes via
  its proxy. Don't assume you can `git push`.

## Roadmap / not done yet

- Friendlier flow for non-technical users (per-user keys, saved jobs).
- Surfacing the API-call/quota threshold warning in the web UI (the CLI has it).
