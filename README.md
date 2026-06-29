# places-api-bot

[![Live demo](https://img.shields.io/badge/Live_demo-Open_app-2563eb?logo=vercel&logoColor=white)](https://places-api-bot.vercel.app/)
[![Deployed on Vercel](https://img.shields.io/badge/Deployed_on-Vercel-000000?logo=vercel&logoColor=white)](https://places-api-bot.vercel.app/)

Look up the Google Maps **business status** (open / temporarily closed /
permanently closed) for a list of restaurants — as a **web app** or a **CLI**.

<!-- Note: screenshot predates the field-selection checkboxes and BYO-key UI. -->
![The Restaurant Status Lookup web app](docs/screenshot.png)

It reads a list of restaurant names, looks each one up with the Google
**Places API (New)** Text Search endpoint, and returns your original rows plus
the business status. The web app shows a results table you can download as a
CSV; the CLI reads `restaurants.csv` and writes `restaurant_status.csv`.

## Using the web app

**🔗 [Open the app](https://places-api-bot.vercel.app/)**
&nbsp;·&nbsp; ask the project owner for the shared **access password**.

1. **Enter the access password** (set by whoever deployed it, via the
   `APP_PASSWORD` setting). The password is checked **before** your CSV is
   uploaded, so the file is never sent on a wrong password.
2. **Choose or drag in a CSV** of restaurant names. It needs a header row and
   one name per line; **any column name works** — the query column is
   auto-detected, otherwise the first column is used. If several match, the
   priority is `query` → `restaurant` → `name` (so a file with both `query` and
   `name` uses `query`). Extra columns are kept and passed straight through. Not
   sure of the format? Click **Download a template CSV** and fill it in.
3. **Tick the information you want** (status, matched name, address, Maps link,
   coordinates, category…). Every option is in the same **Pro** price tier, so
   selecting more never moves you into a pricier tier. Business status is always
   included.
4. *(Optional)* Expand **Use your own Google API key** to run on your own key —
   it's tried first and falls back to the app's key if it fails (you're told
   why). It's used only for that request and never stored.
5. Click **Look up statuses**. The app processes your file in chunks of 75
   queries and shows a **live progress bar** ("X / N looked up · %"). Click
   **Cancel** at any time to stop after the current chunk — partial results are
   still shown and downloadable.
6. Results appear as a colour-coded table —
   <kbd>Open</kbd>, <kbd>Temporarily closed</kbd>, <kbd>Permanently closed</kbd>,
   plus `Not found` / `Unknown` / `Error` when applicable. A banner warns you if
   lookups failed and **why** (e.g. quota/limit exceeded, key rejected). Duplicate
   names in your CSV are looked up only once; a pill shows how many rows were
   skipped.
7. Click **Download CSV** to save the full results (`restaurant_status.csv`).

### Tips

- **Add a city/area to ambiguous names.** The app appends `" singapore"` to
  every query, but a generic name like *"McDonald's"* still matches many
  outlets — *"McDonald's ARC"* or *"Tian Tian Maxwell"* lands the right one.
- **Check the `matched_name` / `matched_address` columns** to confirm the right
  place was matched, and use the **map ↗** link to eyeball it on Google Maps.
- **`Not found`** usually means the name was too vague or misspelled — try
  adding the mall, street, or neighbourhood.
- **Duplicate rows:** identical query values are looked up once; a summary pill
  shows how many duplicate rows were skipped.
- **File format:** upload a real `.csv`. UTF-8 is recommended, but a
  Windows/Excel (cp1252) export with accented names is read correctly too.
  Spreadsheet/binary files (`.xlsx`, `.xls`, PDFs, …) are rejected with a prompt
  to export as CSV — so you don't spend API calls on a garbled file.
- **Large files (up to ~1 000 rows):** the web app handles them automatically
  via client-side chunking (75 queries per request). If you have more than 1 000
  unique names, you'll be asked to confirm the estimated API-call cost before
  processing starts.
- **Very large batches (e.g. 20 000 rows):** use the CLI locally — it is
  uncapped and processes the whole file in one run.
- **Rate limit:** the web app allows up to **5 000 rows per 10 minutes** per IP.
  Large jobs that need more should use the CLI.
- The summary pills show the **row count, number of API calls, and duplicates
  skipped** — calls ≤ unique queries.

## How it works

For each row it sends the query (with `" singapore"` appended) to the Text
Search endpoint, takes the best match, and records its `businessStatus`.

### Cost control

Every lookup uses a **two-step pattern** that minimises cost:

1. **Text Search (New) — IDs-only** (`places.id` only): free tier, returns
   just the matched place ID.
2. **Place Details (New) — Pro**: fetches the fields you actually requested
   (business status, name, address, …) for that place ID.

This is cheaper than a single Text Search Pro call because the IDs-only Text
Search tier is free and Place Details Pro is priced lower than Text Search Pro.
The usage widget's cost table is priced on the **Place Details Pro** SKU
($17.00 per 1,000 requests, 5,000 free/month, with Google's volume discounts) —
the billable step — shown per 1,000 queries the way Google charges.

**Day-cache (web app only).** When an Upstash Redis store is configured, found
places are cached for 24h (`PLACE_CACHE_TTL` seconds, default `86400`) in the
same store as the usage counter. Repeat lookups within the window are served
from cache and cost **zero** Google calls — the results table shows how many
were "served from cache". Each cached entry holds the full Pro payload, so a
later request for different fields still hits the cache. The CLI is **not**
cached (it never talks to Upstash). See [Deploy](#deploy).

All selectable fields live in [`places_bot/fields.py`](places_bot/fields.py)
and are in the **Pro** tier — never the pricier **Enterprise** tier (opening
hours, phone number, rating, website, …). Callers pick fields **by id**, and
unknown ids are ignored, so a request can **never** escalate to a higher-priced
tier.

> **Note on the original example query:** it requested
> `places.currentOpeningHours.openNow`, which is an **Enterprise-tier** field.
> It's intentionally excluded from the catalog. `businessStatus` already returns
> `OPERATIONAL`, `CLOSED_TEMPORARILY`, and `CLOSED_PERMANENTLY`.

The CLI keeps a **local monthly call counter** (`.places_usage.json`) and warns
before a run would push the current month past a threshold (default `10000`).
The web app shows a **live monthly usage widget** with a tiered cost calculator
that updates after each chunk. With an Upstash Redis store configured (see
[Deploy](#deploy)) it reads a **shared, app-wide counter** — so every user sees
the same total, matching how Google's free tier (5,000 calls/month) is shared
across the app's single key. The count is **app-wide and counts every call,
including personal (BYO) keys**, so it tracks total volume through the app
rather than one person's bill. Without the store it falls back to a per-browser
`localStorage` estimate. The cost-confirmation modal pre-checks the cache and
bases its estimate on the **cache misses** (the calls you'll actually pay for).
The Google Cloud console remains authoritative — these are guardrails.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then put your key in it, OR just export the variable:
export GOOGLE_MAPS_API_KEY="your-key"
```

The key is read from the `GOOGLE_MAPS_API_KEY` environment variable, so it's
swappable via a `.env` file locally or a **GitHub Secret** in CI.

## Usage

```bash
python -m places_bot --input restaurants.csv --output restaurant_status.csv
```

### Input format

A CSV with a header row. The query column is auto-detected by **priority**:
`query` → `restaurant` → `restaurant_name` → `name`; if none match, the first
column is used. So when more than one matches (e.g. both `query` and `name`),
`query` wins. Override with `--query-column` (CLI only). Any extra columns you
include are carried through to the output untouched.

Both the web upload and the CLI accept UTF-8 (with or without BOM) and fall back
to Windows-1252 (cp1252) for Excel exports, so emoji, accents (e.g. *café*), and
non-Latin scripts (e.g. *海底捞*) are preserved. Spreadsheet/binary files
(`.xlsx`, `.xls`, PDFs, …) are rejected with a clear message rather than read as
garbage.

```csv
query
McDonald's ARC
Tian Tian Hainanese Chicken Rice Maxwell
```

### Output

Your rows with the selected fields' columns appended. By default:

| column | meaning |
| --- | --- |
| `business_status` | raw Google value (`OPERATIONAL`, `CLOSED_TEMPORARILY`, `CLOSED_PERMANENTLY`, `NOT_FOUND`, `UNKNOWN`, `ERROR`) |
| `business_status_label` | friendly label (Open / Temporarily closed / …) |
| `matched_name` | name Google matched — check this looks right |
| `matched_address` | matched address |
| `google_maps_uri` | link to the place on Google Maps |

Pick which fields to include with `--fields` (web app: checkboxes). The full
catalog lives in [`places_bot/fields.py`](places_bot/fields.py) and is **all
within the Pro pricing tier** — `businessStatus` (always on), `displayName`,
`formattedAddress`, `googleMapsUri`, `shortFormattedAddress`, `location`,
`primaryTypeDisplayName`, `types`, `plusCode`, `id`. Selecting more fields never
moves you into a pricier tier.

### Useful options

| flag | description |
| --- | --- |
| `--fields a,b,c` | output fields to include (default: status + name + address + maps link) |
| `--suffix " singapore"` | text appended to every query |
| `--query-column NAME` | force which column holds the query |
| `--threshold N` | warn above N estimated calls this month (default 10000) |
| `--yes` | don't prompt at the threshold (used in CI) |
| `--no-dedupe` | call the API even for repeated queries (costs more) |

```bash
python -m places_bot --fields businessStatus,displayName,location,types
```

Duplicate queries are de-duplicated by default so you're never charged twice
for the same lookup in one run. If lookups fail, the run ends with a warning
that classifies why (e.g. `quota` = you may have hit your Google API limit).

## Running in GitHub Actions

A `workflow_dispatch` workflow (`.github/workflows/places-status.yml`) runs the
bot against a committed CSV and uploads `restaurant_status.csv` as an artifact.
Add your key under **Settings → Secrets and variables → Actions** as
`GOOGLE_MAPS_API_KEY`, then trigger it from the **Actions** tab.

## Web app (Vercel)

A browser version lives in [`public/index.html`](public/index.html) (the UI) and
[`api/process.py`](api/process.py) (a Flask serverless function that reuses the
same `places_bot` engine). Upload a CSV, get a results table, download the
output CSV — no terminal needed.

```
public/index.html   single-page UI (field checkboxes, optional key, results table)
api/process.py      serverless function: 4 endpoints (see below)
vercel.json         bundles the places_bot package with the function
```

Endpoints in `api/process.py`:

| route | purpose |
| --- | --- |
| `GET /api/fields` | the selectable Pro-tier field catalog (drives the checkboxes) |
| `GET /api/usage` | shared monthly API-call count (no auth); `not_configured` when the Upstash store is absent |
| `POST /api/verify` | checks the password **before** any CSV upload; returns a short-lived signed token. Rate-limited per IP. |
| `POST /api/process` | runs the lookups; gated by the token (or password). Accepts **multipart** (legacy single-shot CSV) or **JSON** (`{ "queries": [...], "fields": [...] }` for the browser's chunk loop; `{ "probe_only": true }` for a one-time key pre-check). |

The browser **parses the CSV and deduplicates queries client-side**, then sends
them to the server in chunks of 75. Each chunk is a JSON `POST /api/process`
request; the server keeps the `" singapore"` suffix, runs lookups concurrently
(`PLACES_MAX_WORKERS`, default 8), and returns results for that chunk. The
browser merges all chunks back into the original row order and builds the output
CSV — your API key never leaves the server.

### Abuse protection

`POST /api/verify` is rate-limited per IP (5 wrong passwords / 10 min → `429`),
and `POST /api/process` is capped per IP at **5 000 rows per 10 minutes**
(counting the rows in every request, JSON or multipart). **This limiter is
in-memory and per-instance** — because Vercel runs many ephemeral instances it
slows casual attacks but is not a hard guarantee. For real protection, enable
**Vercel Firewall / Attack Challenge Mode** (Project → Settings → Firewall) and
use a strong `APP_PASSWORD`. Uploads are also capped at 4 MB.

### Deploy

1. Push this repo to GitHub and import it at [vercel.com/new](https://vercel.com/new)
   (no build settings needed — Vercel detects `public/` and `api/`).
2. In **Project → Settings → Environment Variables**, add:
   - `GOOGLE_MAPS_API_KEY` — your Places API key
   - `APP_PASSWORD` — the shared password you give your team
   - *(optional)* `MAX_ROWS` (default `750`), `PLACES_MAX_WORKERS` (default `8`)
3. *(optional)* For the **shared usage counter and day-cache**, add an Upstash
   Redis store (**Storage → Marketplace → Upstash**, or **Storage → KV**) and
   connect it. Vercel populates the REST URL + token (the names vary —
   `KV_REST_API_*` and any prefixed `…KV_REST_API_*` variant are auto-detected);
   redeploy. The same store powers both features. Tune the cache lifetime with
   `PLACE_CACHE_TTL` (seconds, default `86400`). Skip the store entirely and the
   app still works — no caching, and the widget shows a per-browser estimate.
   The token is read server-side only.
4. Deploy. Share the URL + the password with your team.

Because it scales to zero, it costs nothing when idle; you only pay Google for
the Places API calls.

### Run the web app locally

```bash
pip install -r requirements.txt
export GOOGLE_MAPS_API_KEY="your-key" APP_PASSWORD="local-pass"
python api/process.py            # serves the API on http://localhost:5000
```

(For the full static-UI + function experience, `npm i -g vercel` then `vercel dev`.)

### Limits

`MAX_ROWS` (default 750) caps a single JSON chunk or multipart upload so a
request can't outrun Vercel's 60s timeout. The browser sends at most 75 queries
per chunk, which sits well under this cap. For very large batches (e.g. 20k
rows), use the CLI locally — it is uncapped.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests stub the network, so no API key or quota is consumed.

## Roadmap

- [x] Host as a cheap web app with a clean upload-and-download UI
- [ ] Friendlier flow for non-technical users (e.g. per-user keys, saved jobs)
- [ ] Tighter quota dashboards / alerting
