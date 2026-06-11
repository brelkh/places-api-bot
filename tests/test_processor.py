from places_bot import fields as fields_mod
from places_bot import processor

DEFAULT = fields_mod.resolve_fields()


def test_detect_query_column_prefers_named_column():
    assert processor.detect_query_column(["id", "Query", "notes"]) == "Query"
    assert processor.detect_query_column(["restaurant_name"]) == "restaurant_name"


def test_detect_query_column_falls_back_to_first():
    assert processor.detect_query_column(["foo", "bar"]) == "foo"


def test_summarize_operational():
    places = [
        {
            "businessStatus": "OPERATIONAL",
            "displayName": {"text": "McDonald's"},
            "formattedAddress": "1 Alexandra Rd, Singapore",
            "googleMapsUri": "https://maps.google.com/?cid=123",
        }
    ]
    out = processor.summarize_places(places, DEFAULT)
    assert out["business_status"] == "OPERATIONAL"
    assert out["business_status_label"] == "Open"
    assert out["matched_name"] == "McDonald's"
    assert out["google_maps_uri"].startswith("https://")


def test_summarize_closed_variants():
    assert (
        processor.summarize_places([{"businessStatus": "CLOSED_TEMPORARILY"}], DEFAULT)[
            "business_status_label"
        ]
        == "Temporarily closed"
    )
    assert (
        processor.summarize_places([{"businessStatus": "CLOSED_PERMANENTLY"}], DEFAULT)[
            "business_status_label"
        ]
        == "Permanently closed"
    )


def test_summarize_no_results():
    out = processor.summarize_places([], DEFAULT)
    assert out["business_status"] == "NOT_FOUND"
    assert out["business_status_label"] == "Not found"
    # Other selected columns are present but blank.
    assert out["matched_name"] == ""


def test_summarize_match_without_status():
    out = processor.summarize_places([{"displayName": {"text": "X"}}], DEFAULT)
    assert out["business_status"] == "UNKNOWN"
    assert out["business_status_label"] == "Unknown"


def test_selected_fields_drive_columns():
    fields = fields_mod.resolve_fields(["location", "types"])
    out = processor.summarize_places(
        [
            {
                "businessStatus": "OPERATIONAL",
                "location": {"latitude": 1.3, "longitude": 103.8},
                "types": ["restaurant", "food"],
            }
        ],
        fields,
    )
    # businessStatus is always included (required); displayName is not selected.
    assert out["business_status"] == "OPERATIONAL"
    assert out["latitude"] == "1.3" and out["longitude"] == "103.8"
    assert out["types"] == "restaurant, food"
    assert "matched_name" not in out


def test_read_write_roundtrip_appends_columns(tmp_path):
    src = tmp_path / "in.csv"
    src.write_text("query\nFoo\n", encoding="utf-8")
    fieldnames, rows = processor.read_rows(str(src))
    rows[0].update(
        processor.summarize_places([{"businessStatus": "OPERATIONAL"}], DEFAULT)
    )

    dst = tmp_path / "out.csv"
    processor.write_rows(str(dst), fieldnames, rows, DEFAULT)
    content = dst.read_text(encoding="utf-8")
    assert "business_status" in content.splitlines()[0]
    assert "OPERATIONAL" in content
