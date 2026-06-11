import io

import pytest

from places_bot.client import PlacesAPIError, PlacesClient

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
    # fresh limiter state per test
    web._login_limiter._hits.clear()
    web._process_limiter._hits.clear()
    web.app.testing = True
    return web.app.test_client()


def _csv(text="query\nMcDonald's ARC\nGone Forever\n"):
    return {"file": (io.BytesIO(text.encode("utf-8")), "restaurants.csv")}


def _token(client):
    resp = client.post("/api/verify", json={"password": "secret"})
    assert resp.status_code == 200
    return resp.get_json()["token"]


# --- fields catalog ---
def test_fields_catalog_is_public(client):
    resp = client.get("/api/fields")
    assert resp.status_code == 200
    ids = [f["id"] for f in resp.get_json()["fields"]]
    assert "businessStatus" in ids


# --- verify / auth ---
def test_verify_rejects_wrong_password(client):
    resp = client.post("/api/verify", json={"password": "nope"})
    assert resp.status_code == 401


def test_verify_returns_token(client):
    resp = client.post("/api/verify", json={"password": "secret"})
    assert resp.status_code == 200 and resp.get_json()["token"]


def test_process_requires_auth(client):
    resp = client.post("/api/process", data=_csv(), content_type="multipart/form-data")
    assert resp.status_code == 401


def test_process_with_token(client):
    token = _token(client)
    resp = client.post(
        "/api/process",
        data=_csv(),
        content_type="multipart/form-data",
        headers={"X-App-Token": token},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["api_calls"] == 2
    labels = [r["business_status_label"] for r in body["rows"]]
    assert labels == ["Open", "Permanently closed"]
    assert body["key_used"] == "the app's key"
    assert body["error_count"] == 0


def test_password_fallback_still_works(client):
    # token-less but correct password is accepted by /api/process
    data = _csv()
    data["password"] = "secret"
    resp = client.post("/api/process", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200


# --- field selection ---
def test_field_selection_changes_columns(client):
    token = _token(client)
    data = _csv("query\nMcDonald's ARC\n")
    data["fields"] = ["businessStatus", "location"]
    resp = client.post(
        "/api/process", data=data, content_type="multipart/form-data",
        headers={"X-App-Token": token},
    )
    cols = resp.get_json()["columns"]
    assert "latitude" in cols and "longitude" in cols
    assert "matched_name" not in cols  # displayName not selected


# --- errors / validation ---
def test_rejects_when_no_file(client):
    token = _token(client)
    resp = client.post(
        "/api/process", data={}, content_type="multipart/form-data",
        headers={"X-App-Token": token},
    )
    assert resp.status_code == 400


def test_enforces_row_limit(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_ROWS", 1)
    token = _token(client)
    resp = client.post(
        "/api/process", data=_csv(), content_type="multipart/form-data",
        headers={"X-App-Token": token},
    )
    assert resp.status_code == 413


# --- user-supplied key fallback ---
def test_user_key_failure_falls_back(client, monkeypatch):
    def boom(api_key, **kw):
        return PlacesAPIError("bad", reason="auth")

    monkeypatch.setattr(web.service, "probe_key", boom)
    token = _token(client)
    data = _csv("query\nMcDonald's ARC\n")
    data["api_key"] = "user-bad-key"
    resp = client.post(
        "/api/process", data=data, content_type="multipart/form-data",
        headers={"X-App-Token": token},
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert "your key failed" in body["key_used"]
    assert body["key_warning"] and "rejected" in body["key_warning"]


# --- error banner surfaces quota ---
def test_quota_error_surfaces_banner(client, monkeypatch):
    def quota(self, query):
        raise PlacesAPIError("limit", reason="quota")

    monkeypatch.setattr(PlacesClient, "search_text", quota)
    token = _token(client)
    resp = client.post(
        "/api/process", data=_csv("query\nFoo\n"),
        content_type="multipart/form-data", headers={"X-App-Token": token},
    )
    body = resp.get_json()
    assert body["error_count"] == 1
    assert "quota" in body["error_banner"].lower()


# --- rate limiting on verify ---
def test_verify_rate_limited_after_failures(client):
    for _ in range(5):
        client.post("/api/verify", json={"password": "wrong"})
    resp = client.post("/api/verify", json={"password": "wrong"})
    assert resp.status_code == 429
    assert resp.get_json()["retry_after"] > 0
