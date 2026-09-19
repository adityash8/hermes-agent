"""Saved BI reports through real gateway, HTTP transport and provider SDK boundaries."""

import copy
import hashlib
import json

import httpx
import pytest

from gateway import client_context as cc
from gateway import client_context_reads as reads
from tests.gateway.test_client_context import corpus, event, runner, wire
from tests.gateway.test_client_context_turn import extended, read_then_answer, tool_call


def uid(n):
    return f"00000000-0000-0000-0000-{n:012d}"


@pytest.fixture(params=["posthog", "tableau"])
def bi(request, corpus, extended, monkeypatch):
    vendor = request.param
    extended["platform_toolsets"]["slack"].append("web")
    query = {"kind": "TrendsQuery", "interval": "day",
             "dateRange": {"date_from": "2020-01-01", "date_to": "2020-01-02"},
             "series": [{"kind": "EventsNode", "event": "$pageview", "math": "total"}]}
    query_sha = hashlib.sha256(json.dumps(query, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    grants = []
    for i, letter in enumerate(("a", "b"), 1):
        csv = f"date,value\n2020-01-01,{i * 10}\n2020-01-02,{i * 20}\n"
        params = {"region": "us", "report_id": str(i * 100), "definition_sha256": query_sha} if vendor == "posthog" else {
            "pod": "10ay", "workbook_id": uid(i * 100), "view_updated_at": "2020-01-03T00:00:00Z",
            "workbook_updated_at": "2020-01-03T00:00:00Z", "report_sha256": hashlib.sha256(csv.encode()).hexdigest()}
        grants.append({"id": f"{vendor}-{letter}", "scope_id": "workspace-synthetic", "chat_id": f"channel-{letter}",
            "client": f"client-{letter}", "audience": "internal", "account_id": uid(i),
            "resource_id": str(i * 10) if vendor == "posthog" else uid(i * 10),
            "credential_slot": f"SYNTHETIC_{letter.upper()}", "time_zone": "UTC", "start_date": "2020-01-01",
            "end_date": "2020-01-02", "expires_at": "2099-01-01T00:00:00Z", "operation": ("scoped_posthog_saved_insight" if vendor == "posthog" else "scoped_tableau_saved_view"),
            "status": "approved", "approval_evidence": f"operator-{letter}", "sharing_evidence": None, "parameters": params})
        monkeypatch.setenv(f"CLIENT_CONTEXT_{vendor.upper()}_SYNTHETIC_{letter.upper()}_ACCESS_TOKEN", f"SECRET_{letter}")
    corpus.raw["analytics"] = grants
    corpus.save()
    seen, state = [], {"change": None, "hook": None, "query": query}

    def receive(req):
        seen.append(req)
        assert req.method == "GET"
        if vendor == "posthog":
            assert req.url.host == "us.posthog.com"
            resource = req.url.path.split("/")[3 if "/insights/" in req.url.path else 5]
            g = next(g for g in grants if g["resource_id"] == resource)
            assert req.headers["authorization"] == f"Bearer SECRET_{g['client'][-1]}"
            i = int(resource) // 10
            if "/insights/" in req.url.path:
                assert req.url.path == f"/api/projects/{resource}/insights/{g['parameters']['report_id']}/"
                assert dict(req.url.params) == {"refresh": "force_cache"}
                body = {"id": int(g["parameters"]["report_id"]), "deleted": False, "hasMore": False,
                        "query": copy.deepcopy(query), "last_refresh": "2020-01-03T00:00:00.123456+00:00",
                        "result": [{"days": ["2020-01-01", "2020-01-02"], "data": [i * 10, i * 20],
                                    "label": "PRIVATE_REPORT_CANARY"}], "description": "PRIVATE_REPORT_CANARY"}
            else:
                assert req.url.path == f"/api/organizations/{g['account_id']}/projects/{resource}/"
                body = {"id": int(resource), "organization": g["account_id"], "timezone": "UTC",
                        "api_token": "PRIVATE_ADMIN_CANARY"}
        else:
            assert req.url.host == "10ay.online.tableau.com"
            account = req.url.path.split("/")[4]
            g = next(g for g in grants if g["account_id"] == account)
            assert req.headers["x-tableau-auth"] == f"SECRET_{g['client'][-1]}"
            i = int(account[-12:])
            if req.url.path.endswith("/data"):
                assert dict(req.url.params) == {"maxAge": "1"}
                body = f"date,value\n2020-01-01,{i * 10}\n2020-01-02,{i * 20}\n"
            elif "/views/" in req.url.path:
                body = {"view": {"id": g["resource_id"], "workbook": {"id": g["parameters"]["workbook_id"]},
                                 "updatedAt": "2020-01-03T00:00:00Z", "name": "PRIVATE_ADMIN_CANARY"}}
            else:
                body = {"workbook": {"id": g["parameters"]["workbook_id"], "updatedAt": "2020-01-03T00:00:00Z",
                                     "name": "PRIVATE_REPORT_CANARY"}}
        if state["hook"]:
            state["hook"](req)
        if state["change"]:
            body = state["change"](req, body)
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, text=body, headers={"content-type": "text/csv"}) if isinstance(body, str) else httpx.Response(200, json=body)

    monkeypatch.setattr(reads, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(receive),
                                                                  follow_redirects=False, trust_env=False))
    return vendor, grants, seen, state


def args(grant):
    return {k: grant[k] for k in ("scope_id", "chat_id", "client", "audience", "account_id", "resource_id", "start_date", "end_date")} | {"grant_id": grant["id"]}


@pytest.mark.asyncio
async def test_saved_reports_cross_real_gateway_and_sdk_without_cross_client_or_secret_output(runner, bi, wire):
    vendor, grants, seen, _ = bi
    for i, letter in enumerate(("a", "b"), 1):
        read_then_answer(wire, tool_call(grants[i - 1]["operation"], args(grants[i - 1])))
        assert await runner._handle_message(event(letter))
        output = json.loads(wire.captured[-1]["messages"][-1]["content"])
        assert output["account_id"] == grants[i - 1]["account_id"]
        assert output["result"]["rows"] == [{"date": "2020-01-01", "value": i * 10}, {"date": "2020-01-02", "value": i * 20}]
        assert output["status"] == "observation" and output["approval"] == "not_a_decision_approval"
    assert len(seen) == (8 if vendor == "posthog" else 10)
    for canary in ("SECRET_a", "SECRET_b", "PRIVATE_REPORT_CANARY", "PRIVATE_ADMIN_CANARY", "credential_slot"):
        assert canary not in json.dumps(wire.captured)
    assert grants[1]["account_id"] not in json.dumps(wire.captured[:2])
    runner._run_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["binding", "sql", "url", "grant_url", "foreign_identity", "changed_definition",
    "secret_data", "redirect", "grant_revoke", "source_revoke", "late_identity", "late_definition",
    "duplicate_date", "extra_column", "partial_report", "oversize", "bad_timestamp", "mutable_project_filter"])
async def test_saved_report_adversarial_boundaries_fail_closed(runner, corpus, bi, wire, attack):
    vendor, grants, seen, state = bi
    arguments = args(grants[0])
    if attack == "binding":
        arguments["resource_id"] = grants[1]["resource_id"]
    elif attack in {"sql", "url"}:
        arguments[attack] = "SELECT * FROM everything" if attack == "sql" else "https://evil.invalid"
    elif attack == "grant_url":
        grants[0]["parameters"]["region" if vendor == "posthog" else "pod"] = "evil.invalid/../"
        corpus.save()
    elif attack == "mutable_project_filter":
        if vendor == "posthog":
            state["query"]["filterTestAccounts"] = True
            grants[0]["parameters"]["definition_sha256"] = hashlib.sha256(
                json.dumps(state["query"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        else:
            grants[0]["parameters"]["filter"] = "mutable-server-default"
        corpus.save()
    elif attack in {"grant_revoke", "source_revoke"}:
        def revoke(req):
            if attack == "grant_revoke":
                grants[0]["parameters"]["report_id" if vendor == "posthog" else "workbook_id"] = "changed"
            else:
                corpus.raw["sources"][0]["status"] = "proposed"
            corpus.save()
        state["hook"] = revoke
    else:
        def corrupt(req, body):
            if attack == "oversize":
                return httpx.Response(200, content=b" " * 65537, headers={"content-type": "application/json"})
            if attack == "redirect":
                return httpx.Response(302, headers={"location": "https://evil.invalid/SECRET_a"})
            if vendor == "posthog":
                if "organization" in body and (attack == "foreign_identity" or (attack == "late_identity" and len(seen) > 1)):
                    body["organization"] = grants[1]["account_id"]
                elif "query" in body:
                    if attack == "changed_definition" or (attack == "late_definition" and len(seen) > 2):
                        body["query"]["series"][0]["event"] = "other_tenant_event"
                    elif attack == "secret_data":
                        body["result"][0]["data"][0] = "SECRET_a"
                    elif attack == "duplicate_date":
                        body["result"][0]["days"][1] = "2020-01-01"
                    elif attack == "extra_column":
                        body["result"].append(copy.deepcopy(body["result"][0]))
                    elif attack == "partial_report":
                        body["hasMore"] = True
                    elif attack == "bad_timestamp":
                        body["last_refresh"] = "SECRET_a"
            elif isinstance(body, dict) and "view" in body:
                if attack == "foreign_identity" or (attack == "late_identity" and len(seen) > 3):
                    body["view"]["workbook"]["id"] = uid(999)
                elif attack in {"changed_definition", "bad_timestamp"} or (attack == "late_definition" and len(seen) > 3):
                    body["view"]["updatedAt"] = "2020-01-04T00:00:00Z"
            elif isinstance(body, str):
                if attack == "secret_data":
                    body = body.replace("10", "SECRET_a")
                elif attack == "duplicate_date":
                    body = body.replace("2020-01-02", "2020-01-01")
                elif attack == "extra_column":
                    body = body.replace("date,value", "date,value,SECRET_a")
                elif attack == "partial_report":
                    body = "date,value\n2020-01-01,10\n"
            return body
        state["change"] = corrupt
    read_then_answer(wire, tool_call(grants[0]["operation"], arguments))
    assert await runner._handle_message(event()) == cc.DENIED
    assert not getattr(runner, "_client_context_history", {})
    assert len(wire.captured) <= 1
    assert "SECRET_a" not in json.dumps(wire.captured)
    if attack in {"binding", "sql", "url", "grant_url"}:
        assert not seen
