"""Synthetic Google/Stripe protocol fixtures through the scoped gateway."""
import copy
import json
import httpx
import pytest
from gateway import client_context as cc
from gateway import client_context_reads as reads
from gateway import client_context_google_stripe as vendor
from tests.gateway.test_client_context import corpus, event, runner, wire
from tests.gateway.test_client_context_turn import extended, read_then_answer, tool_call


def grant_for(operation):
    identity = {vendor.SEARCH_CONSOLE: "sc-domain:synthetic.example", vendor.GOOGLE_ADS: "1234567890", vendor.STRIPE: "acct_Synthetic"}[operation]
    return {"id": "provider-a", "scope_id": "workspace-synthetic", "chat_id": "channel-a", "client": "client-a", "audience": "internal", "operation": operation, "account_id": identity, "resource_id": identity, "credential_slot": "SYNTHETIC_A", "time_zone": "America/Los_Angeles", "start_date": "2020-01-01", "end_date": "2020-01-02", "expires_at": "2099-01-01T00:00:00Z", "status": "approved", "approval_evidence": "operator-a", "sharing_evidence": None, "parameters": {"livemode": True} if operation == vendor.STRIPE else {}}


def args(grant):
    return {k: grant[k] for k in ("scope_id", "chat_id", "client", "audience", "account_id", "resource_id", "start_date", "end_date")} | {"grant_id": grant["id"]}


@pytest.fixture(params=list(vendor.OPERATIONS))
def transport(request, corpus, extended, monkeypatch):
    grant = grant_for(request.param)
    corpus.raw["analytics"] = [grant]
    corpus.save()
    extended["platform_toolsets"]["slack"].append("web")
    prefix = {vendor.SEARCH_CONSOLE: "SEARCH_CONSOLE", vendor.GOOGLE_ADS: "GOOGLE_ADS", vendor.STRIPE: "STRIPE"}[grant["operation"]]
    monkeypatch.setenv(f"CLIENT_CONTEXT_{prefix}_SYNTHETIC_A_ACCESS_TOKEN", "SYNTHETIC_SECRET")
    monkeypatch.setenv(f"CLIENT_CONTEXT_{prefix}_SYNTHETIC_A_DEVELOPER_TOKEN", "SYNTHETIC_DEVELOPER")
    seen, state = [], {"change": None, "hook": None}
    def receive(req):
        seen.append(req)
        assert req.headers["authorization"] == "Bearer SYNTHETIC_SECRET"
        operation = grant["operation"]
        if operation == vendor.SEARCH_CONSOLE:
            assert req.url.host == "www.googleapis.com"
            base = "/webmasters/v3/sites/" + grant["resource_id"]
            if req.method == "GET":
                assert req.url.path == base
                body = {"siteUrl": grant["resource_id"], "permissionLevel": "siteFullUser", "private": "PRIVATE_PROVIDER_CANARY"}
            else:
                assert req.method == "POST" and req.url.path == base + "/searchAnalytics/query"
                assert json.loads(req.content) == {"startDate": "2020-01-01", "endDate": "2020-01-02", "dimensions": ["date"], "type": "web", "dataState": "final", "aggregationType": "byProperty", "rowLimit": 31}
                body = {"responseAggregationType": "byProperty", "rows": [{"keys": ["2020-01-01"], "clicks": 2.0, "impressions": 10.0, "ctr": 0.2, "position": 3.0}]}
        elif operation == vendor.GOOGLE_ADS:
            assert req.method == "POST" and req.url.host == "googleads.googleapis.com"
            assert req.url.path == f"/v25/customers/{grant['account_id']}/googleAds:search"
            assert req.headers["developer-token"] == "SYNTHETIC_DEVELOPER"
            assert req.headers["login-customer-id"] == grant["resource_id"]
            customer = {"id": grant["account_id"], "resourceName": "customers/" + grant["account_id"], "timeZone": grant["time_zone"], "currencyCode": "USD"}
            query = json.loads(req.content)["query"]
            if "segments.date" in query:
                assert "BETWEEN '2020-01-01' AND '2020-01-02'" in query
                body = {"results": [{"customer": customer, "segments": {"date": "2020-01-01"}, "metrics": {"impressions": "10", "clicks": "2", "costMicros": "1234567"}}]}
            else:
                assert query == "SELECT customer.id, customer.currency_code, customer.time_zone FROM customer LIMIT 1"
                body = {"results": [{"customer": customer}]}
        else:
            assert req.method == "GET" and req.url.host == "api.stripe.com"
            assert req.headers["stripe-account"] == grant["account_id"] and req.headers["stripe-version"] == "2024-06-20"
            if req.url.path == "/v1/account":
                body = {"object": "account", "id": grant["account_id"], "email": "PRIVATE_PROVIDER_CANARY"}
            else:
                assert req.url.path == "/v1/balance"
                body = {"object": "balance", "livemode": True, "available": [{"currency": "usd", "amount": 100}], "pending": [{"currency": "usd", "amount": -10}], "private": "PRIVATE_PROVIDER_CANARY"}
        if state["hook"]:
            state["hook"](req)
        if state["change"]:
            body = state["change"](req, body)
        return body if isinstance(body, httpx.Response) else httpx.Response(200, json=body)
    monkeypatch.setattr(reads, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(receive), follow_redirects=False, trust_env=False))
    return grant, seen, state


@pytest.mark.asyncio
async def test_normalized_result_through_real_gateway(runner, transport, wire):
    grant, seen, _ = transport
    calls = wire.captured
    read_then_answer(wire, tool_call(grant["operation"], args(grant)))
    await runner._handle_message(event())
    assert len(calls) == 2 and len(seen) == 3
    text = json.dumps(calls[1])
    assert "PRIVATE_PROVIDER_CANARY" not in text and "SYNTHETIC_SECRET" not in text
    result = json.loads(calls[1]["messages"][-1]["content"])["result"]
    if grant["operation"] == vendor.STRIPE:
        assert result["available"] == [{"currency": "usd", "amount_minor_units": 100}]
        assert "not_revenue_or_mrr" in result["coverage"]
    else:
        assert len(result["rows"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("account_id", "wrong-account"), ("resource_id", "wrong-resource"), ("scope_id", "wrong-workspace"), ("client", "client-b"), ("chat_id", "channel-b"), ("audience", "shared"), ("grant_id", "wrong-grant"), ("start_date", "2019-12-31"), ("end_date", "2020-01-03")])
async def test_tampering_denied_before_http(runner, transport, wire, field, value):
    grant, seen, _ = transport
    calls = wire.captured
    read_then_answer(wire, tool_call(grant["operation"], args(grant) | {field: value}))
    assert await runner._handle_message(event()) == cc.DENIED
    assert len(calls) == 1 and seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
async def test_transport_errors_denied(runner, transport, wire, status):
    grant, _, state = transport
    state["change"] = lambda req, body: httpx.Response(status, headers={"Location": "https://wrong.example/"}, text="PRIVATE_PROVIDER_CANARY")
    calls = wire.captured
    read_then_answer(wire, tool_call(grant["operation"], args(grant)))
    assert await runner._handle_message(event()) == cc.DENIED
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_revoked_grant_never_reaches_model(runner, transport, wire, corpus):
    grant, _, state = transport
    def revoke(req):
        grant["status"] = "revoked"
        corpus.save()
    state["hook"] = revoke
    calls = wire.captured
    read_then_answer(wire, tool_call(grant["operation"], args(grant)))
    assert await runner._handle_message(event()) == cc.DENIED
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_wrong_account_metadata_denied(runner, transport, wire):
    grant, _, state = transport
    def corrupt(req, body):
        body = copy.deepcopy(body)
        if grant["operation"] == vendor.SEARCH_CONSOLE:
            body["siteUrl"] = "sc-domain:wrong.example"
        elif grant["operation"] == vendor.GOOGLE_ADS:
            body["results"][0]["customer"]["id"] = "0000000000"
        else:
            body["id"] = "acct_Wrong"
        return body
    state["change"] = corrupt
    calls = wire.captured
    read_then_answer(wire, tool_call(grant["operation"], args(grant)))
    assert await runner._handle_message(event()) == cc.DENIED
    assert len(calls) == 1
