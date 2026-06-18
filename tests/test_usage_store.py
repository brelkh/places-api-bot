"""Unit tests for the shared Upstash-backed usage counter.

The network (Upstash REST API) is stubbed — no real store or credentials are
needed. We patch ``requests.post`` on the module under test.
"""

import pytest

from places_bot import usage_store


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://example.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)


def _fake_post(responses):
    """Return a requests.post stub that maps the request path → JSON result.

    ``responses`` maps a path fragment (e.g. ``"incrby"``, ``"get/"``) to the
    raw Redis ``result`` value to echo back.
    """
    calls = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def post(url, headers=None, timeout=None):
        calls.append(url)
        for frag, result in responses.items():
            if frag in url:
                return _Resp({"result": result})
        return _Resp({"result": None})

    post.calls = calls
    return post


# --- configuration / graceful degradation ---
def test_not_configured_reads_null(monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    assert usage_store.is_configured() is False
    out = usage_store.get_api_usage()
    assert out["storage"] == "not_configured"
    assert out["monthly_api_calls"] is None
    assert out["total_api_calls"] is None


def test_increment_noops_when_unconfigured(monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    # Should not even try to hit the network.
    monkeypatch.setattr(
        usage_store.requests, "post",
        lambda *a, **k: pytest.fail("should not call the network"),
    )
    usage_store.increment_api_calls(5)


def test_kv_env_vars_are_accepted_as_fallback(monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example.io")
    monkeypatch.setenv("KV_REST_API_TOKEN", "tok")
    assert usage_store.is_configured() is True


def test_prefixed_integration_vars_resolve_by_suffix(monkeypatch):
    """Vercel's integration may add a prefix, e.g.
    UPSTASH_REDIS_REST_KV_REST_API_URL — these still resolve."""
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.delenv("KV_REST_API_URL", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    monkeypatch.setenv("UPSTASH_REDIS_REST_KV_REST_API_URL", "https://p.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_KV_REST_API_TOKEN", "write-tok")
    monkeypatch.setenv("UPSTASH_REDIS_REST_KV_URL", "redis://ignored")
    cfg = usage_store._config()
    assert cfg == ("https://p.upstash.io", "write-tok")


def test_read_only_token_is_never_used(monkeypatch):
    """A read-only token can't INCRBY, so it must not be selected even if it's
    the only *_TOKEN present."""
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.delenv("KV_REST_API_TOKEN", raising=False)
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example.io")
    monkeypatch.setenv(
        "UPSTASH_REDIS_REST_KV_REST_API_READ_ONLY_TOKEN", "readonly-tok"
    )
    # No usable write token → not configured (we won't write with a RO token).
    assert usage_store._config() is None
    # With a write token present too, the write token wins.
    monkeypatch.setenv("UPSTASH_REDIS_REST_KV_REST_API_TOKEN", "write-tok")
    assert usage_store._config()[1] == "write-tok"


# --- reads + writes when configured ---
def test_increment_calls_monthly_and_total(configured, monkeypatch):
    post = _fake_post({"incrby": 7})
    monkeypatch.setattr(usage_store.requests, "post", post)
    usage_store.increment_api_calls(5)
    month, month_key = usage_store.current_month_key()
    assert any(f"incrby/{month_key}/5" in u for u in post.calls)
    assert any(f"incrby/{usage_store.TOTAL_KEY}/5" in u for u in post.calls)


def test_increment_ignores_non_positive(configured, monkeypatch):
    monkeypatch.setattr(
        usage_store.requests, "post",
        lambda *a, **k: pytest.fail("should not call the network"),
    )
    usage_store.increment_api_calls(0)
    usage_store.increment_api_calls(-3)


def test_get_api_usage_returns_counts(configured, monkeypatch):
    _, month_key = usage_store.current_month_key()
    post = _fake_post({month_key: "42", usage_store.TOTAL_KEY: "99"})
    monkeypatch.setattr(usage_store.requests, "post", post)
    out = usage_store.get_api_usage()
    assert out["storage"] == "upstash_redis"
    assert out["monthly_api_calls"] == 42
    assert out["total_api_calls"] == 99


def test_get_api_usage_treats_missing_key_as_zero(configured, monkeypatch):
    # Upstash returns result=null for an unset key → 0, not a crash.
    post = _fake_post({})
    monkeypatch.setattr(usage_store.requests, "post", post)
    out = usage_store.get_api_usage()
    assert out["monthly_api_calls"] == 0
    assert out["total_api_calls"] == 0


def test_network_error_degrades_gracefully(configured, monkeypatch):
    def boom(*a, **k):
        raise usage_store.requests.RequestException("down")

    monkeypatch.setattr(usage_store.requests, "post", boom)
    # Reads fall back to 0; writes swallow the error.
    out = usage_store.get_api_usage()
    assert out["monthly_api_calls"] == 0
    usage_store.increment_api_calls(3)  # must not raise
