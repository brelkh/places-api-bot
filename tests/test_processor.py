import pytest

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


# --- binary / encoding handling (decode_csv_bytes + looks_binary) ---
def test_looks_binary_detects_known_magic_and_nul():
    assert processor.looks_binary(b"PK\x03\x04rest-of-xlsx")        # zip/xlsx
    assert processor.looks_binary(b"%PDF-1.7\n...")                  # pdf
    assert processor.looks_binary(b"\xd0\xcf\x11\xe0legacy-xls")     # OLE2 .xls
    assert processor.looks_binary(b"query\nFoo\x00Bar\n")           # NUL byte
    assert not processor.looks_binary(b"query\nFoo,Bar\n")          # plain CSV


def test_decode_csv_bytes_plain_utf8_and_bom():
    assert processor.decode_csv_bytes(b"query\nFoo\n") == "query\nFoo\n"
    # A UTF-8 BOM is stripped so the first header isn't "﻿query".
    out = processor.decode_csv_bytes("﻿query\nFoo\n".encode("utf-8"))
    assert out.startswith("query")


def test_decode_csv_bytes_recovers_cp1252_export():
    # An Excel-on-Windows export of "Café" is cp1252 (0xE9), not UTF-8.
    out = processor.decode_csv_bytes("query\nCafé Foo\n".encode("cp1252"))
    assert "Café" in out


def test_decode_csv_bytes_rejects_binary():
    with pytest.raises(ValueError):
        processor.decode_csv_bytes(b"PK\x03\x04\x14\x00binary-xlsx-bytes")
    # UTF-16 carries NUL bytes for ASCII text → treated as binary/unsupported.
    with pytest.raises(ValueError):
        processor.decode_csv_bytes("query\nFoo\n".encode("utf-16"))


def test_decode_csv_bytes_empty_is_blank():
    # Empty upload decodes to "" so the caller reports the empty-CSV case.
    assert processor.decode_csv_bytes(b"") == ""


# Names with emoji, accents, punctuation, and CJK must survive decode + parse.
UNICODE_NAMES = [
    "Burger 🍔 Joint",                       # emoji (4-byte UTF-8)
    "Café Crème",                            # accented Latin
    "海底捞 Vivocity",                        # Chinese
    "Al-Ameen@Hillview - Bamboo Grove Park",  # hyphen + @
    "#Foodcoholic - 40 Circular Road",        # leading #
]


@pytest.mark.parametrize("name", UNICODE_NAMES)
def test_decode_and_parse_preserve_unicode_utf8(name):
    raw = f"query\n{name}\n".encode("utf-8")
    assert processor.decode_csv_bytes(raw) == f"query\n{name}\n"
    _, rows = processor.read_rows_from_text(processor.decode_csv_bytes(raw))
    assert rows[0]["query"] == name


def test_read_rows_handles_unicode_and_cp1252(tmp_path):
    p = tmp_path / "in.csv"
    # UTF-8 file with emoji + Chinese (CLI path goes through decode_csv_bytes).
    p.write_bytes("query\nBurger 🍔\n海底捞 Vivocity\n".encode("utf-8"))
    _, rows = processor.read_rows(str(p))
    assert [r["query"] for r in rows] == ["Burger 🍔", "海底捞 Vivocity"]
    # Excel-on-Windows cp1252 export with accents.
    p.write_bytes("query\nCafé Olé\n".encode("cp1252"))
    _, rows = processor.read_rows(str(p))
    assert rows[0]["query"] == "Café Olé"


def test_read_rows_rejects_binary_file(tmp_path):
    p = tmp_path / "in.csv"
    p.write_bytes(b"PK\x03\x04\x14\x00fake-xlsx-renamed-to-csv")
    with pytest.raises(ValueError):
        processor.read_rows(str(p))


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
