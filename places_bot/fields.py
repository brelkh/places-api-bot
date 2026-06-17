"""Selectable output fields, all within the Places API **Pro** pricing tier.

This is the single catalog the CLI, the web app, and the API field mask are
built from. Every field here is in the IDs-only or **Pro** tier — nothing in
the more expensive Enterprise tier (opening hours, phone, rating, website, …).
Because callers can only pick fields by id from this catalog, a request can
never escalate into a higher-priced tier (see `resolve_fields`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# Google Maps' raw businessStatus values -> friendly labels.
BUSINESS_STATUS_LABELS = {
    "OPERATIONAL": "Open",
    "CLOSED_TEMPORARILY": "Temporarily closed",
    "CLOSED_PERMANENTLY": "Permanently closed",
}


def _text(value: object) -> str:
    """Pull `.text` out of a localized-text object like displayName."""
    return value.get("text", "") if isinstance(value, dict) else ""


def _extract_business_status(place: dict) -> dict[str, str]:
    raw = place.get("businessStatus", "")
    if raw:
        label = BUSINESS_STATUS_LABELS.get(raw, raw)
    else:
        # Matched a place but Google has no status for it.
        raw, label = "UNKNOWN", "Unknown"
    return {"business_status": raw, "business_status_label": label}


def _extract_location(place: dict) -> dict[str, str]:
    loc = place.get("location") or {}
    lat, lng = loc.get("latitude"), loc.get("longitude")
    return {
        "latitude": "" if lat is None else f"{lat}",
        "longitude": "" if lng is None else f"{lng}",
    }


@dataclass(frozen=True)
class FieldSpec:
    id: str  # stable identifier used by the API/UI and as the form value
    label: str  # human label shown in the UI checkbox
    columns: tuple[str, ...]  # output CSV column(s) this field produces
    mask: str  # the Places API field-mask path
    extract: Callable[[dict], dict[str, str]]
    required: bool = False  # always included, can't be unticked
    default: bool = False  # ticked by default
    description: str = ""


# Ordered catalog. Output columns follow this order.
FIELD_CATALOG: list[FieldSpec] = [
    FieldSpec(
        "businessStatus",
        "Business status",
        ("business_status", "business_status_label"),
        "places.businessStatus",
        _extract_business_status,
        required=True,
        default=True,
        description="Open / temporarily closed / permanently closed — the core result.",
    ),
    FieldSpec(
        "displayName",
        "Matched name",
        ("matched_name",),
        "places.displayName",
        lambda p: {"matched_name": _text(p.get("displayName"))},
        default=True,
        description="The name Google matched — check it's the right place.",
    ),
    FieldSpec(
        "formattedAddress",
        "Full address",
        ("matched_address",),
        "places.formattedAddress",
        lambda p: {"matched_address": p.get("formattedAddress", "")},
        default=True,
    ),
    FieldSpec(
        "googleMapsUri",
        "Google Maps link",
        ("google_maps_uri",),
        "places.googleMapsUri",
        lambda p: {"google_maps_uri": p.get("googleMapsUri", "")},
        default=True,
    ),
    FieldSpec(
        "shortFormattedAddress",
        "Short address",
        ("short_address",),
        "places.shortFormattedAddress",
        lambda p: {"short_address": p.get("shortFormattedAddress", "")},
    ),
    FieldSpec(
        "location",
        "Coordinates (lat/lng)",
        ("latitude", "longitude"),
        "places.location",
        _extract_location,
    ),
    FieldSpec(
        "primaryTypeDisplayName",
        "Primary category",
        ("primary_type",),
        "places.primaryTypeDisplayName",
        lambda p: {"primary_type": _text(p.get("primaryTypeDisplayName"))},
    ),
    FieldSpec(
        "types",
        "Categories",
        ("types",),
        "places.types",
        lambda p: {"types": ", ".join(p.get("types") or [])},
        description="All category tags Google lists (e.g. restaurant, cafe).",
    ),
    FieldSpec(
        "plusCode",
        "Plus code",
        ("plus_code",),
        "places.plusCode",
        lambda p: {"plus_code": (p.get("plusCode") or {}).get("globalCode", "")},
    ),
    FieldSpec(
        "id",
        "Place ID",
        ("place_id",),
        "places.id",
        lambda p: {"place_id": p.get("id", "")},
    ),
]

FIELD_BY_ID: dict[str, FieldSpec] = {f.id: f for f in FIELD_CATALOG}
DEFAULT_FIELD_IDS: list[str] = [f.id for f in FIELD_CATALOG if f.default]
REQUIRED_FIELD_IDS: list[str] = [f.id for f in FIELD_CATALOG if f.required]
STATUS_FIELD_ID = "businessStatus"


def resolve_fields(ids: list[str] | None = None) -> list[FieldSpec]:
    """Turn requested field ids into ordered FieldSpecs.

    Unknown ids are ignored (a whitelist — this is what keeps a request from
    ever requesting a more expensive tier). Required fields are always added.
    With no ids, the default selection is used.
    """
    if not ids:
        ids = DEFAULT_FIELD_IDS
    wanted = {i for i in ids if i in FIELD_BY_ID} | set(REQUIRED_FIELD_IDS)
    return [f for f in FIELD_CATALOG if f.id in wanted]


def build_field_mask(fields: list[FieldSpec]) -> str:
    """Comma-joined `X-Goog-FieldMask` for Text Search (includes 'places.' prefix)."""
    return ",".join(f.mask for f in fields)


def build_details_field_mask(fields: list[FieldSpec]) -> str:
    """Comma-joined `X-Goog-FieldMask` for Place Details calls.

    Place Details paths drop the leading 'places.' that Text Search paths carry.
    """
    return ",".join(f.mask.removeprefix("places.") for f in fields)


def field_columns(fields: list[FieldSpec]) -> list[str]:
    """Ordered, de-duplicated output columns for the chosen fields."""
    cols: list[str] = []
    for f in fields:
        for col in f.columns:
            if col not in cols:
                cols.append(col)
    return cols


def catalog_for_ui() -> list[dict]:
    """Serialisable catalog for the /api/fields endpoint."""
    return [
        {
            "id": f.id,
            "label": f.label,
            "description": f.description,
            "required": f.required,
            "default": f.default,
            "columns": list(f.columns),
        }
        for f in FIELD_CATALOG
    ]
