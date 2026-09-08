"""Account-bound analytics through real gateway, httpx and OpenAI SDK rounds."""

import asyncio
import copy
import json
import threading
from dataclasses import asdict
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway import client_context as cc
from gateway import client_context_analytics as ga
from gateway import client_context_policy as policy
from gateway import client_context_refresh as refresh
from tests.gateway.test_client_context import corpus, event, forbidden, runner, wire
from tests.gateway.test_client_context_turn import extended, read_then_answer, tool_call


@pytest.fixture
def analytics(corpus, extended, monkeypatch):
    extended["platform_toolsets"]["slack"].append("web")
    grants = []
    for letter, account, prop in (("a", "101", "1001"), ("b", "202", "2002")):
        grants.append({"id": f"ga-{letter}", "scope_id": "workspace-synthetic", "chat_id": f"channel-{letter}",
            "client": f"client-{letter}", "audience": "internal", "account_id": account, "property_id": prop,
            "credential_slot": f"SYNTHETIC_{letter.upper()}", "time_zone": "UTC", "start_date": "2020-01-01",
            "end_date": "2020-12-31", "expires_at": "2099-01-01T00:00:00Z", "operation": ga.TOOL,
            "status": "approved", "approval_evidence": f"operator-{letter}", "sharing_evidence": None})
        monkeypatch.setenv(f"CLIENT_CONTEXT_GA4_SYNTHETIC_{letter.upper()}_ACCESS_TOKEN", f"SECRET_{letter}")
    corpus.raw["analytics"] = grants
    corpus.save()
    seen = []
    state = {"change": None, "hook": None}

    def receive(request):
        seen.append(request)
        prop = request.url.path.split("/")[-1].split(":")[0]
        grant = next(g for g in grants if g["property_id"] == prop)
        assert request.headers["authorization"] == f"Bearer SECRET_{grant['client'][-1]}"
        assert request.url.scheme == "https"
        if request.method == "GET":
            assert request.url.host == "analyticsadmin.googleapis.com"
            response = {"name": f"properties/{prop}", "account": f"accounts/{grant['account_id']}",
                        "parent": f"accounts/{grant['account_id']}", "timeZone": "UTC",
                        "propertyType": "PROPERTY_TYPE_ORDINARY", "displayName": "PRIVATE_ADMIN_CANARY"}
        else:
            assert request.method == "POST" and request.url.host == "analyticsdata.googleapis.com"
            assert request.url.path == f"/v1beta/properties/{prop}:runReport"
            body = json.loads(request.content)
            assert body == {"dateRanges": [{"startDate": "2020-01-01", "endDate": "2020-01-02"}],
                            "dimensions": [{"name": "date"}], "metrics": [{"name": m} for m in ga.METRICS],
                            "limit": "31", "keepEmptyRows": False}
            response = {"kind": "analyticsData#runReport", "dimensionHeaders": [{"name": "date"}],
                "metricHeaders": [{"name": m, "type": "TYPE_INTEGER"} for m in ga.METRICS],
                "rows": [{"dimensionValues": [{"value": "20200101"}],
                          "metricValues": [{"value": str(int(grant["account_id"]) + i)} for i in range(3)]}],
                "rowCount": 1, "metadata": {"timeZone": "UTC"}}
        if state["hook"]:
            state["hook"](request)
        if state["change"]:
            changed = state["change"](request, response)
            if isinstance(changed, httpx.Response):
                return changed
        return httpx.Response(200, json=response)

    monkeypatch.setattr(ga, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(receive),
                                                              follow_redirects=False, trust_env=False))
    return seen, state


def arguments(client="a", **changes):
    return {"grant_id": f"ga-{client}", "client": f"client-{client}", "audience": "internal",
            "account_id": "101" if client == "a" else "202", "property_id": "1001" if client == "a" else "2002",
            "start_date": "2020-01-01", "end_date": "2020-01-02", **changes}


def report_round(wire, client="a", **changes):
    read_then_answer(wire, tool_call(ga.TOOL, arguments(client, **changes)))


@pytest.mark.asyncio
async def test_real_sdk_rounds_isolate_two_clients_and_preserve_scoped_prefix(runner, analytics, wire):
    seen, _ = analytics
    report_round(wire)
    assert await runner._handle_message(event()) == "dated evidence only. [brief-a]"
    assert [r.method for r in seen] == ["GET", "POST", "GET"]
    result = json.loads(wire.captured[1]["messages"][-1]["content"])
    assert result["rows"] == [{"date": "2020-01-01", "sessions": 101, "activeUsers": 102, "screenPageViews": 103}]
    assert result["status"] == "observation" and result["approval"] == "not_a_decision_approval"
    assert result["property_id"] == "1001"
    assert await runner._handle_message(event())
    prefix = wire.captured[1]["messages"]
    assert wire.captured[2]["messages"][:len(prefix)] == prefix
    report_round(wire, "b")
    assert await runner._handle_message(event("b"))
    result_b = json.loads(wire.captured[-1]["messages"][-1]["content"])
    assert result_b["account_id"] == "202" and result_b["rows"][0]["sessions"] == 202
    assert "CODEWORDS_A_SYNTHETIC" not in json.dumps(wire.captured[-2:])
    assert '"account_id": "202"' not in json.dumps(wire.captured[:4])
    for canary in ("SECRET_a", "SECRET_b", "PRIVATE_ADMIN_CANARY", "credential_slot", "operator-a"):
        assert canary not in json.dumps(wire.captured)
    runner._run_agent.assert_not_called()
    runner._hmwa_resolve_session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"account_id": "202"}, {"property_id": "2002"}, {"client": "client-b"}, {"audience": "shared"},
    {"grant_id": "ga-b"}, {"url": "https://evil.invalid"}, {"sql": "select *"}, {"method": "DELETE"},
    {"tool": "mcp_ga4"}, {"metrics": ["userId"]}, {"limit": 100000}, {"offset": 31},
    {"start_date": "yesterday"}, {"end_date": "2020-02-01"}, {"start_date": "2019-12-31"},
    {"end_date": "2021-01-01"}, {"start_date": "2020-02-30"}, {"end_date": None},
    {"end_date": "2019-01-01"}, {"account_id": 101},
])
async def test_model_request_cannot_select_resources_or_expand_bounds(runner, analytics, wire, change):
    report_round(wire, **change)
    assert await runner._handle_message(event()) == cc.DENIED
    assert not analytics[0] and len(wire.captured) == 1
    assert not getattr(runner, "_client_context_history", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["foreign_client", "foreign_account_owner", "duplicate", "unknown_field",
    "unapproved", "missing_sharing", "expired", "date_window", "wrong_operation", "credential_path", "bad_zone"])
async def test_invalid_grants_deny_before_provider_or_network(runner, corpus, analytics, wire, change):
    grants = corpus.raw["analytics"]
    grant = grants[0]
    if change == "foreign_client":
        grant["client"] = "client-b"
    elif change == "foreign_account_owner":
        grants[1]["property_id"] = grant["property_id"]
    elif change == "duplicate":
        grants.append(copy.deepcopy(grant))
    elif change == "unknown_field":
        grant["endpoint"] = "https://evil.invalid"
    elif change == "unapproved":
        grant["approval_evidence"] = None
    elif change == "missing_sharing":
        corpus.raw["routes"][0]["audience"] = grant["audience"] = "shared"
        corpus.raw["sources"][0]["audiences"] = ["shared"]
        corpus.raw["sources"][0]["sharing_evidence"] = "shared-source"
    elif change == "expired":
        grant["expires_at"] = "2020-01-01T00:00:00Z"
    elif change == "date_window":
        grant["end_date"] = "2022-01-01"
    elif change == "wrong_operation":
        grant["operation"] = "run_sql"
    elif change == "credential_path":
        grant["credential_slot"] = "../../.env"
    else:
        grant["time_zone"] = "not/a/zone"
    corpus.save()
    assert await runner._handle_message(event()) == cc.DENIED
    assert not wire.captured and not analytics[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["proposed", "no_grant", "no_web", "no_allow", "channel_narrowed", "disabled"])
async def test_closed_allowlist_intersection_and_proposals(runner, corpus, extended, analytics, wire, mode):
    if mode == "proposed":
        corpus.raw["analytics"][0].update(status="proposed", approval_evidence=None)
    elif mode == "no_grant":
        corpus.raw.pop("analytics")
    elif mode == "no_web":
        extended["platform_toolsets"]["slack"].remove("web")
    elif mode == "no_allow":
        runner.config.client_context["read_tools"]["allow"].remove(ga.TOOL)
    elif mode == "channel_narrowed":
        runner._adapter_for_source.return_value.toolsets_for_source = lambda source: ["file"]
    else:
        extended["agent"]["disabled_toolsets"] = ["web"]
    corpus.save()
    report_round(wire)
    assert await runner._handle_message(event()) == cc.DENIED
    assert ga.TOOL not in {t["function"]["name"] for t in wire.captured[0]["tools"]}
    assert not analytics[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["account", "property", "parent", "timezone", "rollup", "deleted", "after_account",
    "headers", "rowcount", "too_many_rows", "duplicate_dates", "foreign_date", "nan", "negative", "text_metric",
    "unknown_field", "report_timezone", "threshold", "sampled", "restricted", "oversize", "redirect", "http_error",
    "invalid_json", "duplicate_json", "compressed", "content_type", "malformed_metadata"])
async def test_upstream_canaries_and_malformed_outputs_never_reach_provider(runner, analytics, wire, fault):
    seen, state = analytics

    def corrupt(request, response):
        if request.method == "GET":
            key_value = {"account": ("account", "accounts/202"), "property": ("name", "properties/2002"),
                "parent": ("parent", "accounts/202"), "timezone": ("timeZone", "Europe/London"),
                "rollup": ("propertyType", "PROPERTY_TYPE_ROLLUP"), "deleted": ("deleteTime", "2020-01-01")}
            if fault in key_value:
                key, value = key_value[fault]
                response[key] = value
            if fault == "after_account" and len(seen) == 3:
                response["account"] = "accounts/202"
            return
        if fault == "headers":
            response["metricHeaders"][0]["name"] = "FOREIGN_CLIENT_CANARY"
        elif fault == "rowcount":
            response["rowCount"] = 30
        elif fault in {"too_many_rows", "duplicate_dates"}:
            response["rows"] *= 32 if fault == "too_many_rows" else 2
            response["rowCount"] = len(response["rows"])
        elif fault == "foreign_date":
            response["rows"][0]["dimensionValues"][0]["value"] = "20201231"
        elif fault in {"nan", "negative", "text_metric"}:
            response["rows"][0]["metricValues"][0]["value"] = {
                "nan": "NaN", "negative": "-1", "text_metric": "FOREIGN_CLIENT_CANARY"}[fault]
        elif fault == "unknown_field":
            response["foreignAccount"] = "FOREIGN_CLIENT_CANARY"
        elif fault == "report_timezone":
            response["metadata"]["timeZone"] = "Europe/London"
        elif fault == "threshold":
            response["metadata"]["subjectToThresholding"] = True
        elif fault == "sampled":
            response["metadata"]["samplingMetadatas"] = [{"samplesReadCount": "1"}]
        elif fault == "restricted":
            response["metadata"]["schemaRestrictionResponse"] = {"activeMetricRestrictions": ["sessions"]}
        elif fault == "malformed_metadata":
            response["metadata"]["samplingMetadatas"] = None
        elif fault == "oversize":
            return httpx.Response(200, content=b" " * (ga.MAX_RESPONSE + 1), headers={"Content-Type": "application/json"})
        elif fault == "redirect":
            return httpx.Response(302, headers={"Location": "https://evil.invalid"})
        elif fault == "http_error":
            return httpx.Response(403, json={"error": "SECRET_a FOREIGN_CLIENT_CANARY"})
        elif fault in {"invalid_json", "duplicate_json"}:
            return httpx.Response(200, content=b'{"x":1,"x":2}' if fault == "duplicate_json" else b'not json',
                                  headers={"Content-Type": "application/json"})
        elif fault == "compressed":
            return httpx.Response(200, json=response, headers={"Content-Encoding": "unsupported"})
        elif fault == "content_type":
            return httpx.Response(200, content=b"FOREIGN_CLIENT_CANARY", headers={"Content-Type": "text/html"})
    state["change"] = corrupt
    report_round(wire)
    assert await runner._handle_message(event()) == cc.DENIED
    assert len(wire.captured) == 1
    assert "FOREIGN_CLIENT_CANARY" not in json.dumps(wire.captured)
    assert not getattr(runner, "_client_context_history", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [1, 2, 3, "continuation", "release"])
@pytest.mark.parametrize("change", ["grant", "source", "policy"])
async def test_revocation_at_every_boundary_discards_results(runner, corpus, extended, analytics, wire, boundary, change):
    seen, state = analytics

    def revoke(*args):
        if change == "grant":
            corpus.raw["analytics"][0]["account_id"] = "303"
            corpus.save()
        elif change == "source":
            (corpus.root / "a.txt").write_text("UNAPPROVED_NEW_BYTES")
        else:
            extended["platform_toolsets"]["slack"].remove("web")
    report_round(wire)
    if isinstance(boundary, int):
        state["hook"] = lambda request: revoke() if len(seen) == boundary else None
    elif boundary == "continuation":
        original = wire.state.callback

        def continuation(body):
            original(body)
            if len(wire.captured) == 2:
                revoke()
        wire.state.callback = continuation
    else:
        async def release(*args):
            revoke()
        runner._hmwa_stop_typing_for_turn = AsyncMock(side_effect=release)
    reply = await runner._handle_message(event())
    assert reply == cc.DENIED if isinstance(boundary, int) else reply is None
    if isinstance(boundary, int):
        assert len(seen) == boundary and len(wire.captured) == 1
    assert not getattr(runner, "_client_context_history", {})


@pytest.mark.asyncio
async def test_changed_grant_invalidates_old_analytics_history(runner, corpus, analytics, wire):
    report_round(wire)
    assert await runner._handle_message(event())
    corpus.raw["analytics"][0]["status"] = "proposed"
    corpus.raw["analytics"][0]["approval_evidence"] = None
    corpus.save()
    wire.state.callback = None
    wire.state.tool_calls = None
    wire.state.text = "no analytics grant"
    wire.state.finish = "stop"
    assert await runner._handle_message(event()) == "no analytics grant"
    assert all(m["role"] != "tool" for m in wire.captured[-1]["messages"])
    assert ga.TOOL not in {t["function"]["name"] for t in wire.captured[-1]["tools"]}
    corpus.raw["analytics"][0].update(status="approved", approval_evidence="operator-a")
    corpus.save()
    assert await runner._handle_message(event())
    assert all(m["role"] != "tool" for m in wire.captured[-1]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "scope_miss", "cancel", "revoke"])
async def test_credential_resolution_cannot_fall_back_or_start_late(runner, corpus, analytics, wire, monkeypatch, mode):
    from agent.secret_scope import reset_secret_scope, set_secret_scope

    report_round(wire)
    if mode in {"missing", "scope_miss"}:
        if mode == "missing":
            monkeypatch.delenv("CLIENT_CONTEXT_GA4_SYNTHETIC_A_ACCESS_TOKEN")
        token = set_secret_scope({}) if mode == "scope_miss" else None
        try:
            assert await runner._handle_message(event()) == cc.DENIED
        finally:
            if token is not None:
                reset_secret_scope(token)
    else:
        started, released, finished = threading.Event(), threading.Event(), threading.Event()

        def delayed(grant):
            started.set()
            try:
                assert released.wait(10)
                return "SECRET_a"
            finally:
                finished.set()
        monkeypatch.setattr(ga, "_credential", delayed)
        task = asyncio.create_task(runner._handle_message(event()))
        try:
            assert await asyncio.to_thread(started.wait, 10)
            if mode == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                corpus.raw["analytics"][0]["account_id"] = "303"
                corpus.save()
            released.set()
            assert await asyncio.to_thread(finished.wait, 10)
            if mode == "revoke":
                assert await task == cc.DENIED
        finally:
            released.set()
            if not task.done():
                task.cancel()
    assert not analytics[0]
    assert len(wire.captured) == 1


def test_offline_validation_and_refresh_preserve_grants_without_execution(corpus, analytics, capsys, tmp_path):
    assert cc.main(["validate", "--manifest", str(corpus.path)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["approved_analytics_grants"] == 2
    assert not receipt["analytics_credentials_checked"]
    assert receipt["activation"] == "not_performed" and not analytics[0]
    candidate = tmp_path.resolve() / "candidate"
    candidate.mkdir()
    for path in corpus.root.iterdir():
        (candidate / path.name).write_bytes(path.read_bytes())
    (candidate / "a.txt").write_text("NEW_PROPOSED_SOURCE")
    proposal = refresh.propose(str(corpus.path), str(candidate))
    changed = proposal["changes"][0]
    assert changed["status"] == "proposed" and changed["approval_evidence"] is None
    decisions = tmp_path.resolve() / "decisions.json"
    decisions.write_text(json.dumps({"base_digest": proposal["base_digest"], "candidate_digest": proposal["candidate_digest"],
        "reviews": [{"id": changed["id"], "sha256": changed["sha256"], "review_evidence": "review-a",
                     "approval_evidence": None, "sharing_evidence": None,
                     "observed_at": "2020-01-01T00:00:00Z", "effective_at": "2020-01-01T00:00:00Z",
                     "expires_at": "2099-01-01T00:00:00Z"}]}))
    output = tmp_path.resolve() / "reviewed.json"
    refresh.review(str(corpus.path), str(candidate), str(decisions), str(output))
    assert [asdict(g) for g in policy.load_registry(str(output)).analytics] == corpus.raw["analytics"]
    assert policy.load_registry(str(output)).sources["brief-a"]["status"] == "observation"
    assert not analytics[0]


@pytest.mark.asyncio
async def test_revocation_on_small_http_chunk_stops_consumption(runner, corpus, analytics, wire, monkeypatch):
    consumed = []

    class RevokingStream(httpx.SyncByteStream):
        def __iter__(self):
            consumed.append("first")
            corpus.raw["analytics"][0]["account_id"] = "303"
            corpus.save()
            yield b'{'
            consumed.append("second")
            yield b'"private":"CANARY"}'

    def receive(request):
        return httpx.Response(200, stream=RevokingStream(), headers={"Content-Type": "application/json"})

    monkeypatch.setattr(ga, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(receive)))
    report_round(wire)
    assert await runner._handle_message(event()) == cc.DENIED
    assert consumed == ["first"]
    assert len(wire.captured) == 1
    assert not getattr(runner, "_client_context_history", {})


@pytest.mark.asyncio
async def test_owner_and_unprotected_paths_keep_legacy_tools(runner, corpus, analytics, wire):
    from gateway.config import Platform

    corpus.raw["owner_private"] = [{"scope_id": "workspace-synthetic", "chat_id": "owner-dm",
        "user_id": "owner-user", "chat_type": "dm", "grant_evidence": "owner-grant",
        "expires_at": "2099-01-01T00:00:00Z"}]
    corpus.save()
    runner._hm_admit_event = AsyncMock(return_value=None)
    assert await runner._handle_message(event(chat_id="owner-dm", user_id="owner-user", chat_type="dm")) is None
    assert await runner._handle_message(event(platform=Platform.TELEGRAM)) is None
    runner.config.client_context = {"enabled": False}
    assert await runner._handle_message(event()) is None
    assert runner._hm_admit_event.await_count == 3
    assert not wire.captured and not analytics[0]
