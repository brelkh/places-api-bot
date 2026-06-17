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

    def search_text(self, query):
        self.calls.append(query)
        if query in self.fail_for:
            raise PlacesAPIError("boom", reason=self.fail_reason)
        if query not in self.results:
            return []
        return [{"id": query}]  # use query string as the fake place ID

    def get_place_details(self, place_id, detail_field_mask):
        if place_id in self.fail_for:
            raise PlacesAPIError("boom", reason=self.fail_reason)
        return self.results[place_id]


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
