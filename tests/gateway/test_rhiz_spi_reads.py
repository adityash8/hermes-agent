"""Exercise fixed SPI reads through the real bounded MCP transport, without network."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from agent import secret_scope
from gateway import client_context_rhiz_reads as reads


@pytest.fixture
def dates():
    end = datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=1)
    return {"start_date": (end - timedelta(days=4)).isoformat(), "end_date": end.isoformat()}


@pytest.fixture
def transport(monkeypatch):
    requests = []
    state = {"error": False, "oversized": False, "after_report": lambda: None}
    token = secret_scope.set_secret_scope({"COMPOSIO_CONSUMER_API_KEY": "test-only-key"})

    def handle(request):
        body = json.loads(request.content)
        requests.append((request, body))
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-03-26"}
        else:
            slug = body["params"]["arguments"]["tools"][0]["tool_slug"]
            data = ({"metricHeaders": [{"name": "sessions", "type": "TYPE_INTEGER"}],
                     "rows": [{"metricValues": [{"value": "1103"}]}], "rowCount": 1}
                    if slug == reads._GA4 else
                    {"rows": [{"clicks": 149, "impressions": 10937, "ctr": 149 / 10937,
                               "position": 12.5}], "responseAggregationType": "byProperty"})
            state.get("change_data", lambda _: None)(data)
            envelope = {"successful": True, "data": {"results": [{"tool_slug": slug,
                        "response": {"successful": True, "data": data}}]}}
            if state["error"]:
                envelope = {"successful": False, "error": "sensitive-remote-error"}
            result = {"content": [{"type": "text", "text": json.dumps(envelope)}]}
            state["after_report"]()
        if state["oversized"]:
            return httpx.Response(200, headers={"content-type": "application/json"}, content=b"x" * 65537)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    monkeypatch.setattr(reads, "_http_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handle), follow_redirects=False, trust_env=False))
    yield requests, state
    secret_scope.reset_secret_scope(token)


@pytest.mark.parametrize("name,alias,resource,expected", [
    ("spi_ga4_report", "google_analytics_lench-tenter", "properties/549700824", {"sessions": 1103}),
    ("spi_gsc_report", "google_search_console_beck-fanner", "https://www.screenprintindia.com/",
     {"clicks": 149, "impressions": 10937}),
])
def test_actual_transport_pins_alias_resource_operation_and_sanitizes_output(transport, dates, name, alias, resource, expected):
    calls, _ = transport
    checks = []
    result = json.loads(reads.execute(name, json.dumps(dates), lambda: checks.append(True)))
    assert all(result[k] == v for k, v in expected.items())
    assert result["start_date"] == dates["start_date"]
    assert len(checks) > 3
    assert all(str(request.url) == "https://connect.composio.dev/mcp" for request, _ in calls)
    _, request = calls[-1]
    assert request["params"]["name"] == "COMPOSIO_MULTI_EXECUTE_TOOL"
    assert request["params"]["arguments"]["sync_response_to_workbench"] is False
    tools = request["params"]["arguments"]["tools"]
    assert len(tools) == 1 and tools[0]["account"] == alias
    args = tools[0]["arguments"]
    if name == "spi_ga4_report":
        assert tools[0]["tool_slug"] == "GOOGLE_ANALYTICS_RUN_REPORT"
        assert args == {"property": resource, "dateRanges": [{"startDate": dates["start_date"],
                        "endDate": dates["end_date"]}], "metrics": [{"name": "sessions"}], "limit": 1}
    else:
        assert tools[0]["tool_slug"] == "GOOGLE_SEARCH_CONSOLE_SEARCH_ANALYTICS_QUERY"
        assert args == {"site_url": resource, **dates, "dimensions": [], "search_type": "web",
                        "data_state": "final", "aggregation_type": "byProperty", "row_limit": 1, "start_row": 0}
    assert "test-only-key" not in json.dumps(result)


@pytest.mark.parametrize("extra", ["property", "site_url", "account", "tool_slug", "endpoint", "metrics",
                                  "dimensions", "method", "credential", "user", "team", "guard"])
def test_unknown_fields_cannot_override_connection_or_identity(transport, dates, extra):
    calls, _ = transport
    for name in ("spi_ga4_report", "spi_gsc_report"):
        with pytest.raises(ValueError):
            reads.validate(name, json.dumps({**dates, extra: "foreign-resource"}))
        result = reads.execute(name, json.dumps({**dates, extra: "foreign-resource"}), lambda: None)
        assert "error" in json.loads(result)
    assert not calls


@pytest.mark.parametrize("name", ["COMPOSIO_MULTI_EXECUTE_TOOL", "tool_call", "execute_code", "tool_search",
                                   "GOOGLE_ANALYTICS_DELETE_PROPERTY", "GOOGLE_SEARCH_CONSOLE_ADD_SITE", "terminal"])
def test_unknown_mutation_admin_and_generic_tools_never_reach_network(transport, dates, name):
    with pytest.raises(ValueError):
        reads.validate(name, json.dumps(dates))
    assert "error" in json.loads(reads.execute(name, json.dumps(dates), lambda: None))
    assert not transport[0]


@pytest.mark.parametrize("kind", ["missing", "duplicate", "extra", "datetime", "invalid", "future",
                                   "today", "old", "long", "reversed", "nan", "oversized", "null"])
def test_dates_and_json_are_strict_and_bounded(transport, dates, kind):
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    args = dict(dates)
    if kind == "missing":
        args.pop("start_date")
    elif kind in {"datetime", "invalid", "null"}:
        args["start_date"] = {"datetime": "2026-01-01T00:00:00Z", "invalid": "2026-02-30", "null": None}[kind]
    elif kind in {"future", "today"}:
        args["end_date"] = (today + timedelta(days=kind == "future")).isoformat()
    elif kind == "old":
        args = {"start_date": (today - timedelta(days=367)).isoformat(), "end_date": (today - timedelta(days=366)).isoformat()}
    elif kind == "long":
        args["start_date"] = (today - timedelta(days=32)).isoformat()
    elif kind == "reversed":
        args["start_date"], args["end_date"] = args["end_date"], args["start_date"]
    elif kind == "extra":
        args["limit"] = 1000
    encoded = json.dumps(args)
    if kind == "duplicate":
        encoded = encoded[:-1] + ',"start_date":"2026-01-01"}'
    elif kind == "nan":
        encoded = '{"start_date":NaN,"end_date":"2026-01-01"}'
    elif kind == "oversized":
        encoded += " " * 1025
    with pytest.raises(ValueError):
        reads.validate("spi_ga4_report", encoded)
    assert "error" in json.loads(reads.execute("spi_ga4_report", encoded, lambda: None))
    assert not transport[0]


@pytest.mark.parametrize("when", ["before", "during", "secret_revoked"])
def test_admission_and_live_revocation_discard_data(transport, dates, when):
    calls, state = transport
    active = [when != "before"]
    token = None

    def revoke():
        nonlocal token
        if when == "secret_revoked":
            token = secret_scope.set_secret_scope({"COMPOSIO_CONSUMER_API_KEY": "changed-key"})
        else:
            active[0] = False

    state["after_report"] = revoke
    try:
        result = reads.execute("spi_ga4_report", json.dumps(dates), lambda: active[0])
        assert "error" in json.loads(result) and "1103" not in result
        assert bool(calls) is (when != "before")
    finally:
        if token is not None:
            secret_scope.reset_secret_scope(token)


@pytest.mark.parametrize("failure", ["error", "oversized", "invalid_number"])
def test_vendor_errors_and_untrusted_output_are_not_exposed(transport, dates, failure):
    _, state = transport
    if failure == "invalid_number":
        state["change_data"] = lambda data: data["rows"][0]["metricValues"][0].update(value="sensitive-remote-error")
    else:
        state[failure] = True
    result = reads.execute("spi_ga4_report", json.dumps(dates), lambda: None)
    assert "error" in json.loads(result)
    assert "sensitive-remote-error" not in result and "test-only-key" not in result


def test_multiplex_missing_secret_never_borrows_other_profile(monkeypatch, transport, dates):
    monkeypatch.setenv("COMPOSIO_CONSUMER_API_KEY", "foreign-profile")
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    token = secret_scope.set_secret_scope({})
    try:
        assert "error" in json.loads(reads.execute("spi_ga4_report", json.dumps(dates), lambda: None))
        assert not transport[0]
    finally:
        secret_scope.reset_secret_scope(token)


def test_batch_validation_is_credential_free(monkeypatch, dates):
    def forbidden(*args, **kwargs):
        pytest.fail("validation must not resolve credentials or contact the network")

    monkeypatch.setattr(reads, "get_secret", forbidden)
    monkeypatch.setattr(reads, "_http_client", forbidden)
    for name in ("spi_ga4_report", "spi_gsc_report"):
        start, end = reads.validate(name, json.dumps(dates))
        assert (start.isoformat(), end.isoformat()) == (dates["start_date"], dates["end_date"])
