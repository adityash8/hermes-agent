"""Bounded requester continuation through native Slack ingress, without live traffic."""
import asyncio
import copy
import json

import pytest

from tests.gateway.test_client_context import corpus, runner, wire  # noqa: F401
from tests.gateway.test_slack_channel_isolation import (  # noqa: F401
    CHANNEL, drain_native, native_text,
)

TEAM = "T07MMEX5658"
BOT = "U0B2X1D8R17"
QUESTIONS = (
    "can you investigate churn and the fixes we shipped last week?",
    "what was voluntary versus involuntary and what were the deltas?",
    "find it in the channel",
)


async def incoming(native_text, text, ts, **changes):
    adapter, _, original = native_text
    payload = {**original, "text": text, "ts": ts, **changes}
    await adapter._handle_slack_message(payload, {"team_id": TEAM})
    await drain_native(adapter)


@pytest.mark.asyncio
async def test_native_completed_request_accepts_only_its_requesters_followup_sequence(native_text, wire):
    adapter, client, _ = native_text
    await incoming(native_text, f"<@{BOT}> {QUESTIONS[0]}", "100.1")
    assert not adapter._participations()
    await incoming(native_text, QUESTIONS[1], "101.1")
    assert not adapter._participations()
    await incoming(native_text, QUESTIONS[2], "102.1")
    assert [json.loads(call["messages"][1]["content"])["current_request"]
            for call in wire.captured] == list(QUESTIONS)
    assert client.chat_postMessage.await_count == 3
    assert not adapter._participations()
    adapter._hydrate_thread_context.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "other-user", "other-thread", "other-channel", "bot", "offboarded", "human-addressed",
    "stale", "policy", "client", "token", "bot-identity", "expired", "stopped", "deleted",
])
async def test_followup_receipt_never_crosses_identity_lifetime_or_revocation(native_text, runner, wire, monkeypatch, change):
    adapter, client, _ = native_text
    await incoming(native_text, f"<@{BOT}> {QUESTIONS[0]}", "100.1")
    fields = {}
    text = QUESTIONS[1]
    ts = "101.1"
    if change == "other-user":
        fields["user"] = "UOTHER"
        client.users_info.return_value["user"]["id"] = "UOTHER"
    elif change == "other-thread":
        fields["thread_ts"] = "99.0"
    elif change == "other-channel":
        fields["channel"] = "COTHER"
    elif change == "bot":
        fields["bot_id"] = "BOTHER"
    elif change == "offboarded":
        client.users_info.return_value["user"]["deleted"] = True
    elif change == "human-addressed":
        text = "<@UOTHER> can you investigate the deltas?"
    elif change == "stale":
        ts = "99.9"
    elif change == "policy":
        runner.config.client_context["codewords_read_channels"][CHANNEL]["generation"] = "changed"
    elif change == "client":
        adapter._team_clients[TEAM] = copy.copy(client)
    elif change == "token":
        client.token = "rotated-synthetic"
    elif change == "bot-identity":
        adapter._team_bot_user_ids[TEAM] = "UOTHERBOT"
    elif change == "expired":
        from plugins.platforms.slack import codewords_followups

        monkeypatch.setattr(codewords_followups, "monotonic", lambda: float("inf"))
    elif change == "stopped":
        await incoming(native_text, "no reply needed", "100.2")
    elif change == "deleted":
        await adapter._handle_slack_message({
            "type": "message", "subtype": "message_deleted", "channel": CHANNEL,
            "deleted_ts": "200.1", "ts": "100.2", "previous_message": {
                "ts": "200.1", "thread_ts": "100.0", "user": BOT,
            },
        }, {"team_id": TEAM})
    await incoming(native_text, text, ts, **fields)
    assert len(wire.captured) == 1
    assert client.chat_postMessage.await_count == 1


@pytest.mark.asyncio
async def test_expiry_and_followup_budget_are_non_sliding_and_fresh_summon_reopens(native_text, wire, monkeypatch):
    from plugins.platforms.slack import codewords_followups

    now = [1000.0]
    monkeypatch.setattr(codewords_followups, "monotonic", lambda: now[0])
    await incoming(native_text, f"<@{BOT}> {QUESTIONS[0]}", "100.1")
    for index in range(8):
        now[0] += 1
        await incoming(native_text, QUESTIONS[1], f"{101 + index}.1")
    assert len(wire.captured) == 9
    await incoming(native_text, QUESTIONS[2], "110.1")
    assert len(wire.captured) == 9
    await incoming(native_text, f"<@{BOT}> start a fresh comparison", "111.1")
    now[0] += 899
    await incoming(native_text, QUESTIONS[1], "112.1")
    assert len(wire.captured) == 11
    now[0] += 2
    await incoming(native_text, QUESTIONS[2], "113.1")
    assert len(wire.captured) == 11


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["no reply needed", "/stop", "stop"])
async def test_native_stop_cancels_only_its_requester_and_requires_fresh_summon(native_text, wire, stop):
    adapter, client, _ = native_text
    entered = asyncio.Event()
    original_handler = adapter._message_handler

    async def pending(event):
        entered.set()
        await asyncio.Event().wait()

    adapter._message_handler = pending
    _, _, original = native_text
    await adapter._handle_slack_message(original, {"team_id": TEAM})
    await asyncio.wait_for(entered.wait(), timeout=2)
    task = next(iter(adapter._session_tasks.values()))
    try:
        client.users_info.return_value["user"]["id"] = "UOTHER"
        await adapter._handle_slack_message(
            {**original, "user": "UOTHER", "text": stop, "ts": "100.2"}, {"team_id": TEAM})
        assert not task.done()
        client.users_info.return_value["user"]["id"] = original["user"]
        await adapter._handle_slack_message(
            {**original, "text": stop, "ts": "101.1"}, {"team_id": TEAM})
        assert task.done()
        adapter._message_handler = original_handler
        await incoming(native_text, QUESTIONS[1], "102.1")
        assert wire.captured == []
        await incoming(native_text, f"<@{BOT}> start fresh", "103.1")
        assert len(wire.captured) == 1
        await incoming(native_text, QUESTIONS[1], "104.1")
        assert len(wire.captured) == 2
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["what happened?", "> <@U0B2X1D8R17> quoted request", "`<@U0B2X1D8R17>` quoted request"])
async def test_history_and_quoted_summons_never_create_a_followup_receipt(native_text, wire, text):
    adapter, _, _ = native_text
    adapter._register_mentioned_thread("100.0", team_id=TEAM)
    adapter._bot_message_ts.add("100.0")
    await incoming(native_text, text, "100.1")
    await incoming(native_text, QUESTIONS[1], "101.1")
    assert wire.captured == []


@pytest.mark.asyncio
async def test_bounded_receipt_eviction_never_creates_authority(native_text, wire, monkeypatch):
    from plugins.platforms.slack import codewords_followups

    monkeypatch.setattr(codewords_followups, "_RECEIPTS", 2)
    for index in range(3):
        await incoming(native_text, f"<@{BOT}> {QUESTIONS[0]}", f"{100 + index}.1",
                       thread_ts=f"{100 + index}.0")
    assert len(wire.captured) == 3
    await incoming(native_text, QUESTIONS[1], "110.1", thread_ts="100.0")
    assert len(wire.captured) == 3
    await incoming(native_text, QUESTIONS[1], "111.1", thread_ts="102.0")
    assert len(wire.captured) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("while_active", [False, True])
async def test_old_stop_cannot_revoke_a_newer_explicit_summon(native_text, wire, while_active):
    adapter, _, original = native_text
    await incoming(native_text, f"<@{BOT}> original question", "100.1")
    await incoming(native_text, "no reply needed", "101.1")
    entered, release = asyncio.Event(), asyncio.Event()
    handler = adapter._message_handler

    async def pending(event):
        entered.set()
        await release.wait()
        return await handler(event)

    if while_active:
        adapter._message_handler = pending
        await adapter._handle_slack_message(
            {**original, "text": f"<@{BOT}> fresh question", "ts": "102.1"}, {"team_id": TEAM})
        await asyncio.wait_for(entered.wait(), timeout=2)
    else:
        await incoming(native_text, f"<@{BOT}> fresh question", "102.1")
    try:
        await adapter._handle_slack_message(
            {**original, "text": "no reply needed", "ts": "101.1"}, {"team_id": TEAM})
        if while_active:
            assert not next(iter(adapter._session_tasks.values())).done()
            release.set()
            await drain_native(adapter)
            adapter._message_handler = handler
        await incoming(native_text, QUESTIONS[1], "103.1")
        assert len(wire.captured) == 3
    finally:
        release.set()
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["strict_mention", "thread_require_mention"])
@pytest.mark.parametrize("stage", ["admission", "worker"])
async def test_explicit_mention_controls_veto_continuation_but_allow_fresh_summons(native_text, wire, setting, stage):
    adapter, _, original = native_text
    await incoming(native_text, f"<@{BOT}> {QUESTIONS[0]}", "100.1")
    followup = {**original, "text": QUESTIONS[1], "ts": "101.1"}
    if stage == "admission":
        adapter.config.extra[setting] = True
    await adapter._handle_slack_message(followup, {"team_id": TEAM})
    if stage == "worker":
        adapter.config.extra[setting] = True
    await drain_native(adapter)
    assert len(wire.captured) == 1
    await incoming(native_text, f"<@{BOT}> fresh explicit request", "102.1")
    assert len(wire.captured) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("setting,value", [
    ("allowed_channels", ["COTHER"]), ("ignored_channels", [CHANNEL]),
])
async def test_channel_revocation_blocks_an_already_queued_followup(native_text, wire, setting, value):
    adapter, _, original = native_text
    await incoming(native_text, f"<@{BOT}> {QUESTIONS[0]}", "100.1")
    await adapter._handle_slack_message(
        {**original, "text": QUESTIONS[1], "ts": "101.1"}, {"team_id": TEAM})
    adapter.config.extra[setting] = value
    await drain_native(adapter)
    assert len(wire.captured) == 1
    assert not adapter._active_sessions
    assert not adapter._session_tasks
    assert not adapter._admissions()
    assert not adapter._participations()
    await incoming(native_text, f"<@{BOT}> blocked explicit request", "102.1")
    assert len(wire.captured) == 1
    adapter.config.extra.pop(setting)
    await incoming(native_text, f"<@{BOT}> fresh explicit request", "103.1")
    assert len(wire.captured) == 2
