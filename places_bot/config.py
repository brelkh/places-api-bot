"""Configuration and constants for the Places API bot.

All cost-sensitive settings live here. The field mask is deliberately
restricted to the Places API (New) **Pro** SKU tier so that calls stay in
the cheapest billable tier that still returns business status.
"""

from __future__ import annotations

import os

from . import fields as fields_mod

# Endpoint for Places API (New) Text Search.
SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"

# Default field mask: the default selection from the Pro-tier catalog.
#
# IMPORTANT (cost control): the catalog in places_bot/fields.py contains only
# "IDs Only" and "Pro" tier fields — never opening-hours / phone / rating /
# website, which sit in the pricier **Enterprise** tier. Callers choose fields
# by id from that catalog, so a request can never escalate the pricing tier.
FIELD_MASK = fields_mod.build_field_mask(fields_mod.resolve_fields())

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
