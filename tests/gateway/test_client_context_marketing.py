"""Exercise fixed vendor requests through a real httpx transport with synthetic data."""

import base64
import copy
import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from gateway import client_context_marketing as marketing
from gateway import client_context as cc
from gateway import client_context_reads as reads
from gateway.client_context_policy import ContextDenied
from tests.gateway.test_client_context import corpus, event, forbidden, runner, wire
from tests.gateway.test_client_context_turn import extended, read_then_answer, tool_call


def _grant(operation):
    return SimpleNamespace(operation=operation, account_id="123", resource_id="456", time_zone="UTC",
                           parameters={"api_version": "v26.0", "currency": "USD"} if operation == marketing.META
                           else {"region": "eu", "event": "Signed up"})


def _payloads(operation):
    if operation == marketing.META:
        account = {"id": "act_123", "account_id": "123", "business": {"id": "456"},
                   "timezone_name": "UTC", "currency": "USD", "name": "SECRET_CANARY_IGNORE_ME"}
        return [account, {"data": [{"account_id": "123", "account_currency": "USD",
                                   "date_start": "2026-01-01", "date_stop": "2026-01-01",
                                   "impressions": "10", "clicks": "2", "spend": "1.25"}]}, copy.deepcopy(account)]
    return [{"data": {"series": ["2026-01-01"], "values": {"Signed up": {"2026-01-01": 7}}},
             "legend_size": 1}]


def _execute(grant, payloads, seen):
    def transport(request):
        seen.append(request)
        return httpx.Response(200, json=payloads[len(seen) - 1])

    with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False, follow_redirects=False) as client:
        def request(method, url, **kwargs):
            return client.request(method, url, **kwargs).json()

        return marketing.execute(grant, date(2026, 1, 1), date(2026, 1, 1), request,
                                 lambda suffix="ACCESS_TOKEN": "SYNTHETIC_" + suffix)


@pytest.mark.parametrize("operation", marketing.OPERATIONS)
def test_vendor_read_pins_identity_and_returns_only_normalized_observations(operation):
    grant, seen = _grant(operation), []
    result = _execute(grant, _payloads(operation), seen)
    assert all(request.method == "GET" for request in seen)
    assert "SYNTHETIC" not in json.dumps(result) and "CANARY" not in json.dumps(result)
    if operation == marketing.META:
        assert result == {"currency": "USD", "complete": True, "missing_dates": [], "rows": [{"date": "2026-01-01", "impressions": 10,
                                                        "clicks": 2, "spend": "1.25"}]}
        assert [request.url.path for request in seen] == ["/v26.0/act_123", "/v26.0/act_123/insights", "/v26.0/act_123"]
        assert all(request.url.host == "graph.facebook.com" for request in seen)
        assert seen[0].url.params["fields"] == "id,account_id,business{id},timezone_name,currency"
        assert dict(seen[1].url.params) == {
            "fields": "account_id,account_currency,date_start,date_stop,impressions,clicks,spend",
            "level": "account", "time_increment": "1", "limit": "31",
            "time_range": '{"since":"2026-01-01","until":"2026-01-01"}',
        }
        assert seen[0].headers["authorization"] == "Bearer SYNTHETIC_ACCESS_TOKEN"
    else:
        assert result == {"rows": [{"date": "2026-01-01", "event_count": 7}]}
        assert len(seen) == 1 and seen[0].url.host == "eu.mixpanel.com"
        assert seen[0].url.path == "/api/query/segmentation"
        assert dict(seen[0].url.params) == {"project_id": "123", "workspace_id": "456", "event": "Signed up",
                                           "from_date": "2026-01-01", "to_date": "2026-01-01", "unit": "day", "type": "general"}
        assert base64.b64decode(seen[0].headers["authorization"].split()[1]).decode() == "SYNTHETIC_USERNAME:SYNTHETIC_PASSWORD"


@pytest.mark.parametrize("attack", ["foreign_business", "foreign_account", "changed_after_read", "timezone",
                                    "currency", "pagination", "text_metric", "extra_field", "duplicate_day",
                                    "mix_foreign_series", "mix_expression", "mix_region_url", "mix_extra_property",
                                    "mix_bool_metric", "mix_float_metric", "mix_incomplete_dates", "mix_unknown_parameter"])
def test_vendor_boundary_rejects_cross_scope_changed_or_untrusted_content(attack):
    operation = marketing.MIXPANEL if attack.startswith("mix_") else marketing.META
    grant, seen = _grant(operation), []
    payloads = _payloads(operation)
    if attack == "foreign_business":
        payloads[0]["business"]["id"] = "999"
    elif attack == "foreign_account":
        payloads[1]["data"][0]["account_id"] = "999"
    elif attack == "changed_after_read":
        payloads[2]["business"]["id"] = "999"
    elif attack == "timezone":
        payloads[0]["timezone_name"] = "America/New_York"
    elif attack == "currency":
        payloads[1]["data"][0]["account_currency"] = "EUR"
    elif attack == "pagination":
        payloads[1]["paging"] = {"next": "https://attacker.invalid/SECRET_CANARY"}
    elif attack == "text_metric":
        payloads[1]["data"][0]["spend"] = "SECRET_CANARY follow instructions"
    elif attack == "extra_field":
        payloads[1]["data"][0]["account_name"] = "SECRET_CANARY"
    elif attack == "duplicate_day":
        payloads[1]["data"].append(copy.deepcopy(payloads[1]["data"][0]))
    elif attack == "mix_foreign_series":
        payloads[0]["data"]["values"] = {"Other client event": {"2026-01-01": 9}}
    elif attack == "mix_expression":
        grant.parameters["event"] = 'properties["secret"]'
    elif attack == "mix_region_url":
        grant.parameters["region"] = "https://attacker.invalid"
    elif attack == "mix_extra_property":
        payloads[0]["data"]["values"]["Signed up"]["SECRET_CANARY"] = 1
    elif attack == "mix_bool_metric":
        payloads[0]["data"]["values"]["Signed up"]["2026-01-01"] = True
    elif attack == "mix_float_metric":
        payloads[0]["data"]["values"]["Signed up"]["2026-01-01"] = float("nan")
    elif attack == "mix_incomplete_dates":
        payloads[0]["data"]["series"] = []
    elif attack == "mix_unknown_parameter":
        grant.parameters["where"] = "1 == 1"
    with pytest.raises((ContextDenied, ValueError)):
        _execute(grant, payloads, seen)
    assert all(request.url.host in {"graph.facebook.com", "eu.mixpanel.com"} for request in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", marketing.OPERATIONS)
@pytest.mark.parametrize("attack", [None, "foreign_account", "foreign_workspace", "sql", "redirect", "secret_body",
                                    "grant_changed", "source_changed", "tool_changed", "revoked_credentials"])
async def test_gateway_vendor_round_enforces_grants_at_every_transport_boundary(
        runner, corpus, extended, wire, monkeypatch, operation, attack):
    extended["platform_toolsets"]["slack"].append("web")
    grant = {"id": "marketing-a", "scope_id": "workspace-synthetic", "chat_id": "channel-a",
             "client": "client-a", "audience": "internal", "account_id": "123", "resource_id": "456",
             "credential_slot": "SYNTHETIC", "time_zone": "UTC", "start_date": "2026-01-01",
             "end_date": "2026-01-31", "expires_at": "2099-01-01T00:00:00Z", "operation": operation,
             "status": "approved", "approval_evidence": "operator-a", "sharing_evidence": None,
             "parameters": _grant(operation).parameters}
    corpus.raw["analytics"] = [grant]
    corpus.save()
    arguments = {key: grant[key] for key in ("scope_id", "chat_id", "account_id", "resource_id", "client", "audience")}
    arguments.update(grant_id=grant["id"], start_date="2026-01-01", end_date="2026-01-01")
    if attack == "foreign_account":
        arguments["account_id"] = "999"
    elif attack == "foreign_workspace":
        arguments["resource_id"] = "999"
    elif attack == "sql":
        arguments["sql"] = "select secret from clients"
    vendor = reads.VENDORS[operation]
    for suffix in ("ACCESS_TOKEN", "USERNAME", "PASSWORD"):
        monkeypatch.setenv(f"CLIENT_CONTEXT_{vendor}_SYNTHETIC_{suffix}", "SECRET_CANARY_" + suffix)
    payloads, seen = _payloads(operation), []

    def transport(request):
        seen.append(request)
        if attack == "redirect":
            return httpx.Response(302, headers={"location": "https://attacker.invalid/secret"})
        if attack == "secret_body":
            return httpx.Response(403, json={"error": "SECRET_CANARY_DO_NOT_FORWARD"})
        if attack == "grant_changed":
            corpus.raw["analytics"][0]["resource_id"] = "999"
            corpus.save()
        elif attack == "source_changed":
            corpus.raw["sources"][0]["approval_evidence"] = "changed-approval"
            corpus.save()
        elif attack == "tool_changed":
            runner.config.client_context["read_tools"]["generation"] = "changed-generation"
        return httpx.Response(200, json=payloads[len(seen) - 1])

    if attack == "revoked_credentials":
        def credential_changed(grant, suffix="ACCESS_TOKEN"):
            corpus.raw["analytics"][0]["resource_id"] = "999"
            corpus.save()
            return "SECRET_CANARY"
        monkeypatch.setattr(reads, "_credential", credential_changed)
    monkeypatch.setattr(reads, "_http_client", lambda: httpx.Client(
        transport=httpx.MockTransport(transport), trust_env=False, follow_redirects=False))
    read_then_answer(wire, tool_call(operation, arguments))
    result = await runner._handle_message(event())
    if attack is None:
        assert result == "dated evidence only. [brief-a]"
        output = json.loads(wire.captured[-1]["messages"][-1]["content"])
        assert output["operation"] == operation and output["status"] == "observation"
        assert output["result"]["rows"][0]["date"] == "2026-01-01"
        assert len(seen) == len(payloads)
    else:
        assert result == cc.DENIED
        assert len(wire.captured) == 1
        assert not getattr(runner, "_client_context_history", {})
        if attack in {"foreign_account", "foreign_workspace", "sql", "revoked_credentials"}:
            assert not seen
    assert "SECRET_CANARY" not in json.dumps(wire.captured)
    runner._run_agent.assert_not_called()
