"""Configuration and constants for the Places API bot.

All cost-sensitive settings live here. The field mask is deliberately
restricted to the Places API (New) **Pro** SKU tier so that calls stay in
the cheapest billable tier that still returns business status.
"""

from __future__ import annotations

import os

# Endpoint for Places API (New) Text Search.
SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"

# Field mask.
#
# IMPORTANT (cost control): every field below is in the "IDs Only" or "Pro"
# pricing tier. We intentionally do NOT request opening-hours fields
# (e.g. places.currentOpeningHours / places.regularOpeningHours) because those
# are in the higher-priced **Enterprise** tier. `places.businessStatus` already
# tells us OPERATIONAL / CLOSED_TEMPORARILY / CLOSED_PERMANENTLY, which is the
# information this tool needs.
#
# If you ever add fields here, check the tier first:
# https://developers.google.com/maps/documentation/places/web-service/place-data-fields
FIELD_MASK = ",".join(
    [
        "places.id",  # IDs-only tier
        "places.displayName",  # Pro tier — lets you verify the match
        "places.formattedAddress",  # Pro tier — lets you verify the match
        "places.businessStatus",  # Pro tier — the field we actually need
        "places.googleMapsUri",  # Pro tier — Google Maps link for spot checks
    ]
)

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
