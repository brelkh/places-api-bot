"""Configuration and constants for the Places API bot.

All cost-sensitive settings live here. The lookup uses a two-step pattern to
minimise cost:
  1. Text Search (IDs-only) — free tier, returns only places.id.
  2. Place Details (Pro)    — billable, returns the requested fields.

This halves the cost vs a single Text Search Pro call, because the IDs-only
Text Search tier is free and Place Details Pro is cheaper than Text Search Pro.
"""

from __future__ import annotations

import os

# Endpoint for Places API (New) Text Search (always called with IDs-only mask).
SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"

# Base URL for Places API (New) Place Details: append /{place_id}.
PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places"

# Text Search is always IDs-only (free tier) — the actual fields come from a
# subsequent Place Details call.  This constant exists for PlacesClient's
# default and for the probe_key helper.
FIELD_MASK = "places.id"

# Defaults for the search request body.
DEFAULT_REGION_CODE = "SG"
DEFAULT_LANGUAGE_CODE = "en"

# Appended to every query to disambiguate restaurants to Singapore.
DEFAULT_QUERY_SUFFIX = " singapore"

# Cost guardrail: warn before a run is likely to push the current calendar
# month past this many calls. Google's typical free monthly allowance is
# 10,000 calls, so that is the default threshold.
DEFAULT_CALL_THRESHOLD = 10_000

# Where the monthly usage counter is persisted (best-effort local estimate).
DEFAULT_USAGE_FILE = ".places_usage.json"

# Environment variable that holds the API key (swap this via GitHub Secrets).
API_KEY_ENV_VAR = "GOOGLE_MAPS_API_KEY"


def get_api_key() -> str:
    """Return the API key from the environment, or raise a helpful error."""
    key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    if not key:
        raise RuntimeError(
            f"Missing API key. Set the {API_KEY_ENV_VAR} environment variable "
            f"(locally via a .env file or your shell; in CI via GitHub Secrets)."
        )
    return key
