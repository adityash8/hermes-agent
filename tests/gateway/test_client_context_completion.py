"""Whole-goal acceptance: all grants, reviewed cross-channel context and owner paths."""

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock

import pytest

from gateway import client_context as cc
from gateway import client_context_analytics as ga
from gateway import client_context_policy as policy
from gateway import client_context_reads as reads
from gateway import client_context_refresh as refresh
from gateway import client_context_turn as turn
from gateway.config import Platform
from tests.gateway.test_client_context import corpus, event, forbidden, runner, wire
from tests.gateway.test_client_context_turn import (
    approved_records, args, extended, read_then_answer, tool_call,
)


@pytest.fixture
def all_grants(corpus, extended, monkeypatch):
    extended["platform_toolsets"]["slack"].append("web")
    base = {"id": "grant", "scope_id": "workspace-synthetic", "chat_id": "channel-a", "client": "client-a",
            "audience": "internal", "credential_slot": "SYNTHETIC_A", "time_zone": "UTC",
            "start_date": "2020-01-01", "end_date": "2020-01-02", "expires_at": "2099-01-01T00:00:00Z",
            "status": "approved", "approval_evidence": "operator-review", "sharing_evidence": None}
    uid = "00000000-0000-0000-0000-000000000001"
    definitions = {
        "scoped_meta_daily_insights": ("101", "201", {"api_version": "v26.0", "currency": "USD"}),
        "scoped_mixpanel_daily_event_count": ("102", "202", {"region": "us", "event": "Signup"}),
        "scoped_tableau_saved_view": (uid, uid, {"pod": "10ay", "workbook_id": uid,
            "view_updated_at": "2020-01-03T00:00:00Z", "workbook_updated_at": "2020-01-03T00:00:00Z",
            "report_sha256": "1" * 64}),
        "scoped_posthog_saved_insight": (uid, "203", {"region": "us", "report_id": "303", "definition_sha256": "2" * 64}),
        "scoped_stripe_balance": ("acct_SYNTHETIC", "acct_SYNTHETIC", {"livemode": False}),
        "scoped_search_console_daily_report": ("sc-domain:synthetic.example", "sc-domain:synthetic.example", {}),
        "scoped_google_ads_daily_report": ("1234567890", "9876543210", {}),
    }
    grants = [{**base, "id": "ga4", "operation": ga.TOOL, "account_id": "103", "property_id": "203"}]
    for name, (account, resource, parameters) in definitions.items():
        grants.append({**base, "id": name.removeprefix("scoped_"), "operation": name,
                       "account_id": account, "resource_id": resource, "parameters": parameters,
                       "time_zone": "America/Los_Angeles" if "search_console" in name else "UTC"})
    corpus.raw["analytics"] = grants
    corpus.save()
    monkeypatch.setattr(reads, "_http_client", forbidden)
    monkeypatch.setattr(reads, "_credential", forbidden)
    monkeypatch.setattr(ga, "_http_client", forbidden)
    monkeypatch.setattr(ga, "_credential", forbidden)
    return grants


def test_offline_capabilities_cover_all_grants_and_preserve_immutable_correlations(corpus, all_grants, capsys):
    assert cc.main(["capabilities", "--manifest", str(corpus.path)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert set(receipt["capabilities"]) == {g["operation"] for g in all_grants}
    assert not receipt["credentials_checked"] and receipt["network_calls"] == 0
    assert not receipt["live_effective_policy_checked"]
    a, b = receipt["routes"]
    assert all(c["manifest_ready"] and c["policy_permits"] is None for c in a["analytics"])
    assert all(not c["manifest_ready"] and c["trust_blocker"] for c in b["analytics"])
    command = ["capabilities", "--manifest", str(corpus.path), "--effective-toolset", "web"]
    for grant in all_grants:
        command.extend(["--allow-tool", grant["operation"]])
    assert cc.main(command) == 0
    assert all(c["policy_permits"] for c in json.loads(capsys.readouterr().out)["routes"][0]["analytics"])
    registry = policy.load_registry(str(corpus.path))
    grant = next(g for g in registry.analytics if g.operation == "scoped_mixpanel_daily_event_count")
    grant.parameters["event"] = "INJECTED"
    assert grant.parameters["event"] == "Signup"
    with pytest.raises(FrozenInstanceError):
        grant.account_id = "foreign"
    schema = reads.schemas(registry.analytics, grant.operation)[0]["function"]["parameters"]
    assert schema["properties"]["scope_id"]["enum"] == ["workspace-synthetic"]
    assert schema["properties"]["chat_id"]["enum"] == ["channel-a"]
    assert "credential_slot" not in json.dumps(schema) and "parameters" not in schema["properties"]


@pytest.mark.parametrize("attack", ["duplicate_id", "foreign_owner", "foreign_slot", "proposed_approval",
                                    "wrong_route", "expires_after_route", "unknown_operation", "extra_field"])
def test_mixed_grants_reject_invalid_trust_bindings_before_sources(corpus, all_grants, attack, monkeypatch):
    grant = copy.deepcopy(all_grants[1])
    grant["id"] = "another"
    if attack == "duplicate_id": grant["id"] = "ga4"
    if attack == "foreign_owner": grant.update(chat_id="channel-b", client="client-b", credential_slot="SYNTHETIC_B")
    if attack == "foreign_slot": grant["account_id"] = "999"
    if attack == "proposed_approval": grant["status"] = "proposed"
    if attack == "wrong_route": grant["client"] = "client-b"
    if attack == "expires_after_route": grant["expires_at"] = "2100-01-01T00:00:00Z"
    if attack == "unknown_operation": grant["operation"] = "mcp_read_anything"
    if attack == "extra_field": grant["url"] = "https://evil.invalid"
    corpus.raw["analytics"].append(grant)
    corpus.save()
    original = policy.read_file

    def forbid_sources(root, path, limit):
        if root == str(corpus.root):
            pytest.fail("invalid grants reached source contents")
        return original(root, path, limit)
    monkeypatch.setattr(policy, "read_file", forbid_sources)
    with pytest.raises((policy.ContextDenied, ValueError)):
        policy.load_registry(str(corpus.path))


@pytest.mark.asyncio
async def test_cross_channel_approved_context_refresh_supersession_and_owner_preservation(
        runner, corpus, all_grants, wire, tmp_path):
    approved_records(corpus)
    second = copy.deepcopy(corpus.raw["routes"][0])
    second["chat_id"], second["grant_evidence"] = "channel-a-research", "cross-channel-review"
    corpus.raw["routes"].append(second)
    corpus.raw["owner_private"] = [{"scope_id": "workspace-synthetic", "chat_id": "owner-dm",
        "user_id": "owner-user", "chat_type": "dm", "grant_evidence": "owner-grant",
        "expires_at": "2099-01-01T00:00:00Z"}]
    corpus.save()
    for channel, thread in (("channel-a", "original-thread"), ("channel-a-research", "other-thread")):
        read_then_answer(wire, tool_call("scoped_source_read", args(source_id="decision-new")))
        assert await runner._handle_message(event(chat_id=channel, thread_id=thread))
        current = json.loads(wire.captured[-1]["messages"][-1]["content"])
        assert current["content"] == "CURRENT_APPROVED_DECISION" and current["status"] == "approved"
        read_then_answer(wire, tool_call("scoped_history_read", args(source_id="decision-old")))
        assert await runner._handle_message(event(chat_id=channel, thread_id=thread))
        old = json.loads(wire.captured[-1]["messages"][-1]["content"])
        assert old["content"] == "EXPLICIT_APPROVED_HISTORY" and old["lifecycle"] == "superseded"
    # A channel without the successor grant must never revive the stale decision.
    second["source_ids"].remove("decision-new")
    corpus.save()
    count = len(wire.captured)
    assert await runner._handle_message(event(chat_id=second["chat_id"])) == cc.DENIED
    assert len(wire.captured) == count
    second["source_ids"].append("decision-new")
    corpus.save()
    # Prepare/review a separate synthetic source tree; proposals cannot activate.
    candidate = tmp_path.resolve() / "candidate"
    candidate.mkdir()
    for path in corpus.root.iterdir():
        (candidate / path.name).write_bytes(path.read_bytes())
    (candidate / "decision-new.txt").write_text("REVIEWED_RESEARCH_DECISION")
    proposal = refresh.propose(str(corpus.path), str(candidate))
    assert proposal["changes"][0]["status"] == "proposed"
    read_then_answer(wire, tool_call("scoped_source_read", args(source_id="decision-new")))
    assert await runner._handle_message(event(chat_id=second["chat_id"]))
    assert "REVIEWED_RESEARCH_DECISION" not in json.dumps(wire.captured)
    decisions = tmp_path.resolve() / "decisions.json"
    decisions.write_text(json.dumps({"base_digest": proposal["base_digest"], "candidate_digest": proposal["candidate_digest"],
        "reviews": [{"id": "decision-new", "sha256": hashlib.sha256(b"REVIEWED_RESEARCH_DECISION").hexdigest(),
            "review_evidence": "reviewed-change", "approval_evidence": "new-explicit-approval", "sharing_evidence": None,
            "observed_at": "2022-01-01T00:00:00Z", "effective_at": "2022-01-01T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z"}]}))
    output = tmp_path.resolve() / "reviewed.json"
    refresh.review(str(corpus.path), str(candidate), str(decisions), str(output))
    reviewed = json.loads(output.read_text())
    assert reviewed["analytics"] == all_grants
    assert reviewed["routes"] == corpus.raw["routes"]
    assert "CURRENT_APPROVED_DECISION" in (corpus.root / "decision-new.txt").read_text()
    runner.config.client_context["manifest"] = str(output)  # Synthetic harness activation only.
    assert await runner._handle_message(event(chat_id=second["chat_id"]))
    latest = wire.captured[-1]
    assert "REVIEWED_RESEARCH_DECISION" in json.dumps(latest)
    assert "CURRENT_APPROVED_DECISION" not in json.dumps(latest)
    read_then_answer(wire, tool_call("scoped_history_read", args(source_id="decision-old")))
    assert await runner._handle_message(event("b")) == cc.DENIED
    assert "EXPLICIT_APPROVED_HISTORY" not in json.dumps(wire.captured[-1])
    # Existing real owner/native tool preservation tests run in the 21-file union.
    runner._hm_admit_event = AsyncMock(return_value=None)
    captured = len(wire.captured)
    assert await runner._handle_message(event(chat_id="owner-dm", user_id="owner-user", chat_type="dm")) is None
    assert await runner._handle_message(event(platform=Platform.TELEGRAM)) is None
    assert runner._hm_admit_event.await_count == 2 and len(wire.captured) == captured
    runner._run_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", reads.VENDORS)
async def test_each_named_operation_requires_source_effective_policy(runner, corpus, all_grants, extended, wire, name):
    # A valid grant cannot restore a platform tool disabled by channel/base policy.
    extended["platform_toolsets"]["slack"].remove("web")
    grant = next(g for g in all_grants if g["operation"] == name)
    params = {k: grant[k] for k in ("scope_id", "chat_id", "account_id", "resource_id", "client", "audience", "start_date", "end_date")}
    params["grant_id"] = grant["id"]
    read_then_answer(wire, tool_call(name, params))
    assert await runner._handle_message(event()) == cc.DENIED
    assert name not in {t["function"]["name"] for t in wire.captured[0]["tools"]}
    assert len(wire.captured) == 1
    assert set(turn.CAPABILITIES) >= set(reads.VENDORS)


@pytest.mark.parametrize("expired", ["analytics", "source", "history", "route"])
def test_offline_check_rejects_expired_grant_types_without_live_reads(corpus, all_grants, capsys, expired):
    approved_records(corpus)
    if expired == "analytics":
        corpus.raw["analytics"][1]["expires_at"] = "2019-01-01T00:00:00Z"
    elif expired == "source":
        corpus.raw["sources"][0]["expires_at"] = "2019-01-01T00:00:00Z"
    elif expired == "history":
        corpus.raw["sources"][-2]["expires_at"] = "2020-12-31T00:00:00Z"
    else:
        corpus.raw["routes"][0]["expires_at"] = "2019-01-01T00:00:00Z"
    corpus.save()
    assert cc.main(["capabilities", "--manifest", str(corpus.path)]) == 1
    assert not capsys.readouterr().out
