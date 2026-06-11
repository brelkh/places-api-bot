# places-api-bot

Reads a list of restaurant names from **`restaurants.csv`**, looks each one up
with the Google **Places API (New)** Text Search endpoint, and writes
**`restaurant_status.csv`** — your original rows plus the Google Maps
**business status** (open / temporarily closed / permanently closed).

## How it works

For each row it sends the query (with `" singapore"` appended) to the Text
Search endpoint, takes the best match, and records its `businessStatus`.

### Cost control

The request field mask is restricted to the **Pro** pricing tier — the cheapest
tier that still returns business status:

```
places.id, places.displayName, places.formattedAddress,
places.businessStatus, places.googleMapsUri
```

> **Note on the original example query:** it requested
> `places.currentOpeningHours.openNow`, which is an **Enterprise-tier** field and
> would have charged every call at the higher rate. It's intentionally left out.
> `businessStatus` already returns `OPERATIONAL`, `CLOSED_TEMPORARILY`, and
> `CLOSED_PERMANENTLY`, which is exactly what we need. `displayName` and
> `formattedAddress` are kept (still Pro tier) so you can eyeball whether the
> right restaurant was matched.

It also keeps a **local monthly call counter** (`.places_usage.json`) and warns
before a run would push the current month past a threshold (default `10000`,
matching the usual free allowance). The Google Cloud console remains the
authoritative source — this is just a guardrail.

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

A CSV with a header row. The query column is auto-detected (it looks for a
column named `query`, `restaurant`, `restaurant_name`, or `name`, otherwise it
uses the first column). Override with `--query-column`. Any extra columns you
include are carried through to the output untouched.

```csv
query
McDonald's ARC
Tian Tian Hainanese Chicken Rice Maxwell
```

### Output

The same rows with these columns appended:

| column | meaning |
| --- | --- |
| `business_status` | raw Google value (`OPERATIONAL`, `CLOSED_TEMPORARILY`, `CLOSED_PERMANENTLY`, `NOT_FOUND`, `UNKNOWN`, `ERROR`) |
| `business_status_label` | friendly label (Open / Temporarily closed / …) |
| `matched_name` | name Google matched — check this looks right |
| `matched_address` | matched address |
| `google_maps_uri` | link to the place on Google Maps |

### Useful options

| flag | description |
| --- | --- |
| `--suffix " singapore"` | text appended to every query |
| `--query-column NAME` | force which column holds the query |
| `--threshold N` | warn above N estimated calls this month (default 10000) |
| `--yes` | don't prompt at the threshold (used in CI) |
| `--no-dedupe` | call the API even for repeated queries (costs more) |

Duplicate queries are de-duplicated by default so you're never charged twice
for the same lookup in one run.

## Running in GitHub Actions

A `workflow_dispatch` workflow (`.github/workflows/places-status.yml`) runs the
bot against a committed CSV and uploads `restaurant_status.csv` as an artifact.
Add your key under **Settings → Secrets and variables → Actions** as
`GOOGLE_MAPS_API_KEY`, then trigger it from the **Actions** tab.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests stub the network, so no API key or quota is consumed.

## Roadmap

- [ ] Host as a cheap web app with a clean upload-and-download UI
- [ ] Friendlier flow for non-technical users
- [ ] Tighter quota dashboards / alerting
