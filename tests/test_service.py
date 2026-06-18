from places_bot import fields as fields_mod
from places_bot import service
from places_bot.client import PlacesAPIError, PlacesClient

DEFAULT = fields_mod.resolve_fields()


class FakeClient(PlacesClient):
    """Two-step fake: search_text returns a place ID; get_place_details returns fields.

    `results` maps query string → place-details dict.  An absent key means not
    found (search returns []).  `fail_for` can contain query strings (to fail at
    the search step) or place IDs (to fail at the details step).
    """

    def __init__(self, results, fail_for=None, fail_reason="unknown"):
        super().__init__(api_key="x")
        self.results = results
        self.fail_for = fail_for or set()
        self.fail_reason = fail_reason
        self.calls = []  # tracks search_text calls (= unique queries)
        self.detail_masks = []  # tracks the field mask used for each details call

    def search_text(self, query):
        self.calls.append(query)
        if query in self.fail_for:
            raise PlacesAPIError("boom", reason=self.fail_reason)
        if query not in self.results:
            return []
        return [{"id": query}]  # use query string as the fake place ID

    def get_place_details(self, place_id, detail_field_mask):
        self.detail_masks.append(detail_field_mask)
        if place_id in self.fail_for:
            raise PlacesAPIError("boom", reason=self.fail_reason)
        return self.results[place_id]


class FakeCache:
    """In-memory stand-in for places_bot.cache (query → place payload)."""

    def __init__(self, store=None, enabled=True):
        self.store = dict(store or {})
        self.enabled = enabled
        self.set_calls = []  # each set_many payload, in order

    def is_enabled(self):
        return self.enabled

    def get_many(self, queries):
        return {q: self.store[q] for q in queries if q in self.store}

    def set_many(self, places):
        self.set_calls.append(dict(places))
        self.store.update(places)


def _rows(*queries):
    return [{"query": q} for q in queries]


def _run(client, rows, **kw):
    return service.lookup_statuses(
        rows, "query", suffix=" singapore", client=client, fields=DEFAULT, **kw
    )


def test_dedupe_shares_one_call():
    client = FakeClient({"A singapore": {"businessStatus": "OPERATIONAL"}})
    rows = _rows("A", "A", "A")
    summary = _run(client, rows, dedupe=True)
    assert summary.api_calls == 1
    assert len(client.calls) == 1
    assert {r["business_status_label"] for r in rows} == {"Open"}


def test_no_dedupe_calls_each_row():
    client = FakeClient({"A singapore": {"businessStatus": "OPERATIONAL"}})
    rows = _rows("A", "A")
    summary = _run(client, rows, dedupe=False)
    assert summary.api_calls == 2


def test_empty_query_costs_no_call():
    client = FakeClient({})
    rows = _rows("", "  ")
    summary = _run(client, rows)
    assert summary.api_calls == 0
    assert all(r["business_status"] == "ERROR" for r in rows)
    assert summary.error_count == 2


def test_api_error_becomes_error_row_with_reason():
    client = FakeClient({}, fail_for={"Bad singapore"}, fail_reason="quota")
    rows = _rows("Bad")
    summary = _run(client, rows)
    assert rows[0]["business_status"] == "ERROR"
    assert summary.error_reasons == {"quota": 1}


def test_concurrent_matches_sequential_order():
    results = {f"R{i} singapore": {"businessStatus": "OPERATIONAL"} for i in range(20)}
    client = FakeClient(results)
    rows = _rows(*[f"R{i}" for i in range(20)])
    summary = _run(client, rows, max_workers=8)
    assert summary.api_calls == 20
    assert all(r["business_status_label"] == "Open" for r in rows)


# --- caching (web-only path) ---
def test_cache_hit_skips_google_call():
    client = FakeClient({})  # client would find nothing; cache provides the data
    cache = FakeCache({"A singapore": {"businessStatus": "OPERATIONAL"}})
    rows = _rows("A")
    summary = _run(client, rows, dedupe=False, cache=cache)
    assert summary.cache_hits == 1
    assert summary.api_calls == 0
    assert client.calls == []  # no Text Search, no Place Details
    assert rows[0]["business_status_label"] == "Open"


def test_cache_miss_fetches_then_stores():
    client = FakeClient({"A singapore": {"businessStatus": "OPERATIONAL"}})
    cache = FakeCache()
    rows = _rows("A")
    summary = _run(client, rows, dedupe=False, cache=cache)
    assert summary.api_calls == 1 and summary.cache_hits == 0
    # The found place was written to the cache for next time.
    assert cache.store["A singapore"] == {"businessStatus": "OPERATIONAL"}
    # A second run is now a pure cache hit.
    client.calls.clear()
    summary2 = _run(client, _rows("A"), dedupe=False, cache=cache)
    assert summary2.cache_hits == 1 and summary2.api_calls == 0
    assert client.calls == []


def test_cache_miss_fetches_full_pro_mask():
    client = FakeClient({"A singapore": {"businessStatus": "OPERATIONAL"}})
    cache = FakeCache()
    # DEFAULT fields exclude 'location', but caching fetches the full catalog.
    _run(client, _rows("A"), dedupe=False, cache=cache)
    assert "location" in client.detail_masks[0]


def test_cache_not_found_is_not_stored():
    client = FakeClient({})  # "A singapore" not found
    cache = FakeCache()
    rows = _rows("A")
    summary = _run(client, rows, dedupe=False, cache=cache)
    assert summary.api_calls == 1 and summary.cache_hits == 0
    assert cache.store == {}  # nothing cached for a not-found query
    assert rows[0]["business_status"] == "NOT_FOUND"


def test_cache_hit_returns_only_requested_fields():
    """A cache entry holds the full Pro payload, but a request for a subset of
    fields must return only those columns — not the whole cached record."""
    full_place = {
        "businessStatus": "OPERATIONAL",
        "displayName": {"text": "McDonald's"},
        "formattedAddress": "1 Alexandra Rd",
        "googleMapsUri": "https://maps.google.com/x",
        "location": {"latitude": 1.23, "longitude": 4.56},
        "types": ["restaurant", "cafe"],
        "id": "place123",
    }
    client = FakeClient({})  # everything comes from cache
    cache = FakeCache({"A singapore": full_place})
    only_status = fields_mod.resolve_fields(["businessStatus"])

    rows = [{"query": "A"}]
    summary = service.lookup_statuses(
        rows, "query", suffix=" singapore", client=client,
        fields=only_status, dedupe=False, cache=cache,
    )

    assert summary.cache_hits == 1 and summary.api_calls == 0
    # Requested columns are present...
    assert rows[0]["business_status"] == "OPERATIONAL"
    assert rows[0]["business_status_label"] == "Open"
    # ...and nothing from the rest of the cached payload leaked in.
    for leaked in (
        "matched_name", "matched_address", "google_maps_uri",
        "latitude", "longitude", "types", "place_id",
    ):
        assert leaked not in rows[0]


def test_disabled_cache_falls_back_to_normal_path():
    client = FakeClient({"A singapore": {"businessStatus": "OPERATIONAL"}})
    cache = FakeCache(enabled=False)
    summary = _run(client, _rows("A"), dedupe=True, cache=cache)
    assert summary.api_calls == 1 and summary.cache_hits == 0
    assert cache.set_calls == []  # cache untouched when disabled


def test_probe_key_ok_and_failure():
    ok_client = FakeClient({"Starbucks": [{"id": "x"}]})
    # probe_key builds its own client, so patch the method on the class for the probe.
    import places_bot.service as svc

    class _Probe(PlacesClient):
        def __init__(self, *a, **k):
            super().__init__(api_key="x")

        def search_text(self, q):
            if bad_key:
                raise PlacesAPIError("nope", reason="auth")
            return [{"id": "x"}]

    bad_key = False
    svc.PlacesClient = _Probe
    try:
        assert svc.probe_key("k", region_code="SG", language_code="en") is None
        bad_key = True
        err = svc.probe_key("k", region_code="SG", language_code="en")
        assert err is not None and err.reason == "auth"
    finally:
        svc.PlacesClient = PlacesClient
