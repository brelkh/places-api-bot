import io

import pytest

from places_bot.client import PlacesClient

flask = pytest.importorskip("flask")
from api import process as web  # noqa: E402


FAKE = {
    "McDonald's ARC singapore": [
        {
            "businessStatus": "OPERATIONAL",
            "displayName": {"text": "McDonald's"},
            "formattedAddress": "1 Alexandra Rd",
            "googleMapsUri": "https://maps.google.com/x",
        }
    ],
    "Gone Forever singapore": [{"businessStatus": "CLOSED_PERMANENTLY"}],
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "secret")
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "key")

    def fake_search(self, query):
        return FAKE.get(query, [])

    monkeypatch.setattr(PlacesClient, "search_text", fake_search)
    web.app.testing = True
    return web.app.test_client()


def _csv(text="query\nMcDonald's ARC\nGone Forever\n"):
    return {"file": (io.BytesIO(text.encode("utf-8")), "restaurants.csv")}


def test_rejects_missing_password(client):
    resp = client.post("/api/process", data=_csv(), content_type="multipart/form-data")
    assert resp.status_code == 401


def test_rejects_wrong_password(client):
    data = _csv()
    data["password"] = "nope"
    resp = client.post("/api/process", data=data, content_type="multipart/form-data")
    assert resp.status_code == 401


def test_processes_csv(client):
    data = _csv()
    data["password"] = "secret"
    resp = client.post("/api/process", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["query_column"] == "query"
    assert body["api_calls"] == 2
    labels = [r["business_status_label"] for r in body["rows"]]
    assert labels == ["Open", "Permanently closed"]
    assert "business_status" in body["csv"].splitlines()[0]
    assert body["summary"]["Open"] == 1


def test_rejects_when_no_file(client):
    resp = client.post(
        "/api/process",
        data={"password": "secret"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


def test_enforces_row_limit(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_ROWS", 1)
    data = _csv()
    data["password"] = "secret"
    resp = client.post("/api/process", data=data, content_type="multipart/form-data")
    assert resp.status_code == 413
