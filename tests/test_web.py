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


# --------------------------------------------------------------------------- #
# JSON lookup mode
# --------------------------------------------------------------------------- #
def test_json_happy_path(client):
    token = _token(client)
    resp = client.post(
        "/api/process",
        json={"queries": ["McDonald's ARC", "Gone Forever"]},
        headers={"X-App-Token": token},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body["results"]) == 2
    assert body["results"][0]["query"] == "McDonald's ARC"
    assert body["results"][0]["business_status_label"] == "Open"
    assert body["results"][1]["business_status_label"] == "Permanently closed"
    assert body["api_calls"] == 2
    assert body["error_count"] == 0
    assert body["key_used"] == "the app's key"


def test_json_field_selection(client):
    token = _token(client)
    resp = client.post(
        "/api/process",
        json={"queries": ["McDonald's ARC"], "fields": ["businessStatus", "location"]},
        headers={"X-App-Token": token},
    )
    assert resp.status_code == 200
    result = resp.get_json()["results"][0]
    assert "latitude" in result and "longitude" in result
    assert "matched_name" not in result  # displayName not selected


def test_json_error_reasons(client, monkeypatch):
    def quota_search(self, query):
        raise PlacesAPIError("limit", reason="quota")

    monkeypatch.setattr(PlacesClient, "search_text", quota_search)
    token = _token(client)
    resp = client.post(
        "/api/process",
        json={"queries": ["Some Place"]},
        headers={"X-App-Token": token},
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["error_count"] == 1
    assert body["error_reasons"].get("quota") == 1


def test_json_byo_key_fallback(client, monkeypatch):
    def boom(api_key, **kw):
        return PlacesAPIError("bad", reason="auth")

    monkeypatch.setattr(web.service, "probe_key", boom)
    token = _token(client)
    resp = client.post(
        "/api/process",
        json={"queries": ["McDonald's ARC"], "api_key": "bad-user-key"},
        headers={"X-App-Token": token},
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert "your key failed" in body["key_used"]
    assert body["key_warning"] and "rejected" in body["key_warning"]


def test_json_probe_only_returns_key_info(client):
    token = _token(client)
    resp = client.post(
        "/api/process",
        json={"probe_only": True},
        headers={"X-App-Token": token},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["key_used"] == "the app's key"
    assert "results" not in body  # no lookup performed


def test_json_requires_auth(client):
    resp = client.post("/api/process", json={"queries": ["McDonald's ARC"]})
    assert resp.status_code == 401


# --- row-counting rate limiter ---
def test_json_row_counting_rate_limiter(client, monkeypatch):
    """process limiter counts rows; 429 after budget exhausted."""
    monkeypatch.setattr(web, "_process_limiter", web.RateLimiter(max_events=5, window_seconds=600))
    token = _token(client)
    # 5 rows — exactly at the limit, should be allowed
    resp = client.post(
        "/api/process",
        json={"queries": [f"Place {i}" for i in range(5)]},
        headers={"X-App-Token": token},
    )
    assert resp.status_code == 200
    # 1 more row — total 6 > 5, should be rejected
    resp = client.post(
        "/api/process",
        json={"queries": ["One More Place"]},
        headers={"X-App-Token": token},
    )
    assert resp.status_code == 429
    assert resp.get_json()["retry_after"] > 0
