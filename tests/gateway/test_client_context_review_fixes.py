"""Behavior regressions from the independent final connector review."""
import hashlib
import json
from datetime import date
import httpx
import pytest
from gateway import client_context as cc
from gateway import client_context_marketing as meta
from tests.gateway.test_client_context import corpus, event, runner, wire
from tests.gateway.test_client_context_turn import extended, read_then_answer, tool_call
from tests.gateway.test_client_context_bi import bi, args as bi_args
from tests.gateway.test_client_context_marketing import _grant, _payloads
from tests.gateway.test_client_context_provider_protocol import transport, grant_for, args, vendor


@pytest.mark.parametrize("empty", [False, True])
def test_meta_marks_missing_dates_without_inventing_zero(empty):
    grant, seen = _grant(meta.META), []
    payloads = _payloads(meta.META)
    if empty:
        payloads[1]["data"] = []
    def receive(request):
        seen.append(request)
        return httpx.Response(200, json=payloads[len(seen) - 1])
    with httpx.Client(transport=httpx.MockTransport(receive)) as client:
        def request(method, url, **kwargs):
            return client.request(method, url, **kwargs).json()
        result = meta._meta(grant, date(2026, 1, 1), date(2026, 1, 2), request, lambda: "synthetic")
    assert result["complete"] is False
    assert result["missing_dates"] == (["2026-01-01", "2026-01-02"] if empty else ["2026-01-02"])
    assert len(result["rows"]) == (0 if empty else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("bi", ["tableau"], indirect=True)
@pytest.mark.parametrize("value,accepted", [("999999999999999.999999", False), ("0.25", True)])
async def test_tableau_never_rounds_approved_decimal(runner, corpus, bi, wire, value, accepted):
    _, grants, _, state = bi
    csv = f"date,value\n2020-01-01,{value}\n2020-01-02,20\n"
    grants[0]["parameters"]["report_sha256"] = hashlib.sha256(csv.encode()).hexdigest()
    corpus.save()
    state["change"] = lambda request, body: csv if request.url.path.endswith("/data") else body
    calls = wire.captured
    read_then_answer(wire, tool_call(grants[0]["operation"], bi_args(grants[0])))
    result = await runner._handle_message(event())
    if accepted:
        assert len(calls) == 2
        assert json.loads(calls[1]["messages"][-1]["content"])["result"]["rows"][0]["value"] == 0.25
    else:
        assert result == cc.DENIED and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [vendor.SEARCH_CONSOLE], indirect=True)
@pytest.mark.parametrize("property_id", ["https://example.com/blog.v2/", "https://example.com/docs%20archive/", "https://xn--bcher-kva.de/", "sc-domain:xn--bcher-kva.de"])
async def test_search_console_preserves_valid_property_id(runner, corpus, transport, wire, property_id):
    grant, seen, _ = transport
    grant["account_id"] = grant["resource_id"] = property_id
    corpus.save()
    calls = wire.captured
    read_then_answer(wire, tool_call(grant["operation"], args(grant)))
    await runner._handle_message(event())
    assert len(calls) == 2 and len(seen) == 3
    assert all(request.url.host == "www.googleapis.com" for request in seen)


@pytest.mark.parametrize("value", ["https://example.com@evil.example/", "https://example.com/#fragment", "https://example.com/?query", "https://example.com/%zz/", "https://example.com/\n", "sc-domain:-bad.example", "sc-domain:example..com"])
def test_invalid_search_console_property_remains_denied(value):
    grant = grant_for(vendor.SEARCH_CONSOLE)
    grant["account_id"] = grant["resource_id"] = value
    with pytest.raises(Exception):
        vendor.validate_parameters(grant)
