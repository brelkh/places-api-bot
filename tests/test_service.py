from places_bot import service
from places_bot.client import PlacesAPIError, PlacesClient


class FakeClient(PlacesClient):
    def __init__(self, results, fail_for=None):
        super().__init__(api_key="x")
        self.results = results
        self.fail_for = fail_for or set()
        self.calls = []

    def search_text(self, query):
        self.calls.append(query)
        if query in self.fail_for:
            raise PlacesAPIError("boom")
        return self.results.get(query, [])


def _rows(*queries):
    return [{"query": q} for q in queries]


def test_dedupe_shares_one_call():
    client = FakeClient({"A singapore": [{"businessStatus": "OPERATIONAL"}]})
    rows = _rows("A", "A", "A")
    calls = service.lookup_statuses(
        rows, "query", suffix=" singapore", client=client, dedupe=True
    )
    assert calls == 1
    assert len(client.calls) == 1
    assert {r["business_status_label"] for r in rows} == {"Open"}


def test_no_dedupe_calls_each_row():
    client = FakeClient({"A singapore": [{"businessStatus": "OPERATIONAL"}]})
    rows = _rows("A", "A")
    calls = service.lookup_statuses(
        rows, "query", suffix=" singapore", client=client, dedupe=False
    )
    assert calls == 2


def test_empty_query_costs_no_call():
    client = FakeClient({})
    rows = _rows("", "  ")
    calls = service.lookup_statuses(
        rows, "query", suffix=" singapore", client=client
    )
    assert calls == 0
    assert all(r["business_status"] == "ERROR" for r in rows)


def test_api_error_becomes_error_row():
    client = FakeClient({}, fail_for={"Bad singapore"})
    rows = _rows("Bad")
    service.lookup_statuses(rows, "query", suffix=" singapore", client=client)
    assert rows[0]["business_status"] == "ERROR"


def test_concurrent_matches_sequential_order():
    results = {f"R{i} singapore": [{"businessStatus": "OPERATIONAL"}] for i in range(20)}
    client = FakeClient(results)
    rows = _rows(*[f"R{i}" for i in range(20)])
    calls = service.lookup_statuses(
        rows, "query", suffix=" singapore", client=client, max_workers=8
    )
    assert calls == 20
    assert all(r["business_status_label"] == "Open" for r in rows)
