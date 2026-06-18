"""Unit tests for the web-only day-cache. The Upstash REST API is stubbed."""

import json

import pytest

from places_bot import cache


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://example.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    monkeypatch.delenv("PLACE_CACHE_TTL", raising=False)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# --- graceful degradation ---
def test_disabled_is_noop(monkeypatch):
    for var in (
        "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
        "KV_REST_API_URL", "KV_REST_API_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        cache.requests, "post", lambda *a, **k: pytest.fail("must not hit network")
    )
    assert cache.is_enabled() is False
    assert cache.get_many(["a singapore"]) == {}
    cache.set_many({"a singapore": {"businessStatus": "OPERATIONAL"}})  # no-op


def test_network_error_degrades(configured, monkeypatch):
    def boom(*a, **k):
        raise cache.requests.RequestException("down")

    monkeypatch.setattr(cache.requests, "post", boom)
    assert cache.get_many(["A singapore"]) == {}
    cache.set_many({"A singapore": {"x": 1}})  # must not raise


# --- reads ---
def test_get_many_parses_mget_and_normalises_keys(configured, monkeypatch):
    captured = {}

    def post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        return _Resp(
            {"result": [json_dumps({"businessStatus": "OPERATIONAL"}), None]}
        )

    json_dumps = json.dumps
    monkeypatch.setattr(cache.requests, "post", post)
    out = cache.get_many(["McDonald's  ARC singapore", "Missing singapore"])
    # First key hit, second (null) absent.
    assert out == {"McDonald's  ARC singapore": {"businessStatus": "OPERATIONAL"}}
    assert captured["body"][0] == "MGET"
    # case-folded + whitespace-collapsed key.
    assert captured["body"][1] == "place_cache:mcdonald's arc singapore"
    assert captured["url"] == "https://example.upstash.io"  # single-command endpoint


# --- writes ---
def test_set_many_uses_pipeline_with_ttl(configured, monkeypatch):
    captured = {}

    def post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        return _Resp([{"result": "OK"}])

    monkeypatch.setattr(cache.requests, "post", post)
    cache.set_many({"A singapore": {"businessStatus": "OPERATIONAL"}})
    assert captured["url"].endswith("/pipeline")
    cmd = captured["body"][0]
    assert cmd[0] == "SET"
    assert cmd[1] == "place_cache:a singapore"
    assert json.loads(cmd[2]) == {"businessStatus": "OPERATIONAL"}
    assert cmd[3] == "EX" and cmd[4] == str(cache.DEFAULT_TTL_SECONDS)


def test_ttl_env_override(configured, monkeypatch):
    monkeypatch.setenv("PLACE_CACHE_TTL", "60")
    assert cache._ttl_seconds() == 60
    monkeypatch.setenv("PLACE_CACHE_TTL", "0")
    assert cache._ttl_seconds() == cache.DEFAULT_TTL_SECONDS
    monkeypatch.setenv("PLACE_CACHE_TTL", "garbage")
    assert cache._ttl_seconds() == cache.DEFAULT_TTL_SECONDS
