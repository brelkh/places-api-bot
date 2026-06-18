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
Web (api/process.py)  ───┘     lookup_statuses()           search_text()   (IDs-only, free)
                                     │                     get_place_details() (Pro, billable)
                                     └─► places_bot/processor.py  (CSV in/out, status labels)
```

`places_bot/service.py::lookup_statuses()` is the **single source of truth** for
the lookup. Both the CLI and the web function call it. **Do not duplicate lookup
logic** — extend the service instead.

Data flow: parse CSV rows → `lookup_statuses` builds the de-duplicated set of
queries (each = `"<name>" + suffix`, suffix defaults to `" singapore"`) →
**step 1** `client.search_text` with IDs-only mask (free tier) →
**step 2** `client.get_place_details` with the Pro field mask (concurrently in
the web path) → `processor.summarize_places` maps the place object to output
columns → rows are written back to CSV.

## File map

| Path | What lives here |
| --- | --- |
| `places_bot/fields.py` | **Field catalog** — Pro-tier `FieldSpec`s, `resolve_fields`, `build_field_mask`, `build_details_field_mask`, `field_columns`, status labels. The whitelist that caps the pricing tier. |
| `places_bot/config.py` | Constants + `get_api_key()`. `FIELD_MASK` = `"places.id"` (IDs-only for Text Search), `PLACE_DETAILS_URL`, endpoint URL, defaults, threshold. |
| `places_bot/client.py` | `PlacesClient` — `search_text` (IDs-only), `get_place_details` (Pro fields), retry/backoff, **thread-local sessions**. `PlacesAPIError.reason` + `classify_error`. |
| `places_bot/service.py` | `lookup_statuses()` (dedupe + concurrency, returns `LookupSummary` with error reasons) and `probe_key()`. The shared engine. |
| `places_bot/processor.py` | CSV read/write (file + in-memory). `summarize_places`/`error_summary`/`output_fieldnames`/`rows_to_csv` all take `fields`. `detect_query_column`. |
| `places_bot/cli.py` | argparse CLI (`--fields`), cost-threshold prompt, progress, usage tracking, error summary. |
| `places_bot/usage.py` | `UsageTracker` — best-effort local monthly call counter (`.places_usage.json`). CLI only. |
| `places_bot/usage_store.py` | Shared, durable monthly counter backed by **Upstash Redis** over its REST API (via `requests`). `increment_api_calls`/`get_api_usage`/`is_configured`. Web only; no-ops gracefully when env vars absent. Keys: `places_api_calls:<YYYY-MM>` + `places_api_calls:total`. |
| `api/process.py` | Flask function for Vercel. 4 routes: `GET /api/fields`, `GET /api/usage` (shared counter, no auth), `POST /api/verify` (password→token, rate-limited), `POST /api/process` (multipart legacy + JSON chunk mode + JSON `probe_only` key check). In-memory `RateLimiter` (counts rows, not requests). |
| `public/index.html` | Vanilla-JS single-page UI (no build step). Owns CSV parse/dedupe/chunk/merge entirely — the server is stateless between chunks. Verifies password first, then processes. Usage widget seeds from `GET /api/usage` (shared) and falls back to `localStorage` when the store isn't configured. |
| `vercel.json` | Bundles `places_bot/**` with the function, 60s `maxDuration`. |
| `tests/` | `test_processor.py`, `test_service.py`, `test_cli.py`, `test_web.py`, `test_usage_store.py`. All stub the network. |
| `.github/workflows/places-status.yml` | `workflow_dispatch` CI run that uploads the output CSV as an artifact. |

## Invariants — don't break these

1. **Output fields stay in the Pro pricing tier.** All selectable fields live in
   `places_bot/fields.py::FIELD_CATALOG` (IDs-only/Pro only). Callers pick fields
   **by id**, and `resolve_fields` ignores unknown ids — so a request can never
   escalate into the pricier **Enterprise** tier (opening hours, phone, rating,
   website). To add a field: add a `FieldSpec` with its `mask` (e.g.
   `places.businessStatus`) + extractor, after verifying it is Pro. The mask is
   used for Text Search display (with `places.` prefix) and Place Details (prefix
   stripped by `build_details_field_mask`). Never add an Enterprise field here.
2. **Two-step lookup pattern.** Each query makes two API calls: (a) Text Search
   IDs-only (free) to get the place ID, then (b) Place Details Pro to fetch the
   requested fields. **Never** revert to a single Text Search Pro call — it costs
   more for the same data. Both steps run inside the same `call()` closure in
   `service.lookup_statuses`.
3. **One engine.** CLI and web both go through `service.lookup_statuses`, which is
   field-driven (`summarize_places`/`error_summary`/CSV helpers all take the
   resolved `fields`). Keep it that way.
4. **Concurrency safety.** The web path runs the two-step lookup across threads.
   The client keeps a `requests.Session` per thread (`threading.local`). Don't add
   shared mutable state across threads (the rate limiter uses a lock).
5. **Secrets via env, never committed.** `GOOGLE_MAPS_API_KEY` (CLI + web),
   `APP_PASSWORD` (web; also signs the verify token), and the optional Upstash
   pair `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` (web shared
   counter). The resolver also accepts `KV_REST_API_URL` / `KV_REST_API_TOKEN`
   and any prefixed variant ending in those suffixes (Vercel's integration emits
   e.g. `UPSTASH_REDIS_REST_KV_REST_API_URL`); the `*_READ_ONLY_TOKEN` is never
   used since it can't `INCRBY`.
   Local: `.env` / shell. Vercel: dashboard env vars. CI: GitHub Secrets. `.env`
   and `restaurant_status.csv` are git-ignored. The Upstash REST token is read
   **server-side only** — never sent to the browser.
6. **Dedupe = cost control.** Identical queries are looked up once. `LookupSummary.
   api_calls` ≤ rows (+1 if a user key is probed). Each counted call = one Place
   Details Pro call (the IDs-only Text Search is free and not counted separately).
7. **Auth before upload.** The browser calls `POST /api/verify` first and only
   uploads the CSV on success; the server still re-checks (token or password) on
   `POST /api/process`. Keep this order — don't let `/api/process` parse a body
   before authorizing.
8. **Shared usage counter is best-effort and public-read.** `GET /api/usage`
   needs no auth (like `/api/fields`) — the page loads it before password entry.
   It returns counts only, never credentials. `POST /api/process` increments the
   counter by `summary.api_calls + probe_calls` (the same number it returns as
   `api_calls`) after each lookup. If Upstash env vars are absent the counter
   reads 0/`not_configured` and writes are silently skipped — never let a slow or
   missing store break a lookup.

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
- **Vercel routes functions by filename.** All `/api/*` paths are sent to the
  single `api/process.py` via the `rewrites` rule in `vercel.json`; Flask then
  dispatches by the (preserved) original path. If you add a new Flask route like
  `/api/foo`, it works locally but **404s on Vercel unless** the rewrite covers
  it (it covers `/api/(.*)`, so you're fine). Don't add a second `api/*.py`
  expecting the in-memory rate limiter to be shared — separate functions are
  separate instances.
- **`MAX_ROWS` (default 750)** caps a single JSON chunk or multipart upload so
  it finishes inside the 60s Vercel timeout. The browser sends ≤75 queries per
  chunk, well under this cap. Very large batches (e.g. 20k rows) → use the CLI
  (uncapped).
- **Browser owns CSV parse/dedupe/chunk/merge.** The frontend parses the CSV
  (RFC 4180, BOM-aware), detects the query column, deduplicates, and sends at
  most 75 unique queries per `POST /api/process` JSON request. The server is
  stateless between chunks. Merging and final CSV generation happen in the
  browser after all chunks complete.
- **Row-counting rate limiter.** `_process_limiter` counts rows (not requests):
  5 000 rows / 10 min per IP. Each JSON chunk charges `len(queries)` rows;
  multipart charges `len(rows)` rows. `_login_limiter` is unchanged (5 attempts
  per 10 min).
- **BYO-key under chunking.** The browser does a single `probe_only: true`
  request before the chunk loop. If accepted, subsequent chunks pass
  `skip_probe: true` (trusting the already-validated key). This avoids
  re-probing on every chunk.
- **Output columns** come from the selected `FieldSpec`s (`fields.field_columns`);
  the web UI renders whatever columns come back. To add output, add a `FieldSpec`
  in `fields.py` (see invariant #1).
- **Pushing:** on the user's local machine the git remote is `github.com` over
  HTTPS with no stored creds — the user pushes; an in-session sandbox pushes via
  its proxy. Don't assume you can `git push`.

## Roadmap / not done yet

- Friendlier flow for non-technical users (per-user keys, saved jobs).
- Surfacing the API-call/quota threshold warning in the web UI (the CLI has it).
