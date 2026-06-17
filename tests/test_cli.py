import csv

from places_bot import cli
from places_bot.client import PlacesClient


FAKE_RESULTS = {
    "McDonald's ARC singapore": {
        "businessStatus": "OPERATIONAL",
        "displayName": {"text": "McDonald's"},
        "formattedAddress": "1 Alexandra Rd",
        "googleMapsUri": "https://maps.google.com/x",
    },
    "Gone Forever singapore": {"businessStatus": "CLOSED_PERMANENTLY"},
}


def test_main_end_to_end(tmp_path, monkeypatch):
    src = tmp_path / "restaurants.csv"
    with src.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["query"])
        w.writerow(["McDonald's ARC"])
        w.writerow(["Gone Forever"])
        w.writerow(["McDonald's ARC"])  # duplicate -> should be deduped

    out = tmp_path / "out.csv"
    usage = tmp_path / "usage.json"

    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")

    calls = {"n": 0}

    def fake_search(self, text_query):
        calls["n"] += 1
        return [{"id": text_query}] if text_query in FAKE_RESULTS else []

    def fake_details(self, place_id, detail_mask):
        return FAKE_RESULTS[place_id]

    monkeypatch.setattr(PlacesClient, "search_text", fake_search)
    monkeypatch.setattr(PlacesClient, "get_place_details", fake_details)

    rc = cli.main(
        [
            "--input",
            str(src),
            "--output",
            str(out),
            "--usage-file",
            str(usage),
        ]
    )
    assert rc == 0

    # Duplicate query should only have triggered 2 unique API calls.
    assert calls["n"] == 2

    with out.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    assert rows[0]["business_status_label"] == "Open"
    assert rows[1]["business_status_label"] == "Permanently closed"
    assert rows[2]["business_status_label"] == "Open"  # from cache


def test_main_missing_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    src = tmp_path / "restaurants.csv"
    src.write_text("query\nFoo\n", encoding="utf-8")
    rc = cli.main(["--input", str(src), "--output", str(tmp_path / "o.csv")])
    assert rc == 2
    assert "Missing API key" in capsys.readouterr().err
