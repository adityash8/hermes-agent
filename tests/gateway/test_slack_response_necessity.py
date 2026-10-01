"""Response necessity remains distinct from permission on native Slack ingress."""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter
from tests.gateway.test_client_context import corpus, runner, wire


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr("plugins.platforms.slack.adapter.SLACK_AVAILABLE", True)
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake", extra={
        "free_response_channels": ["C1"], "ignore_other_user_mentions": False,
    }))
    adapter._app = MagicMock()
    adapter._bot_user_id = "UBOT"
    adapter._team_bot_user_ids = {"T1": "UBOT"}
    adapter._authorization_check = lambda *args, **kwargs: True
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "200.000001"}
    adapter._get_client = MagicMock(return_value=client)
    adapter._resolve_user_is_bot = AsyncMock(return_value=False)
    adapter._resolve_user_name = AsyncMock(return_value="Human")
    adapter._resolve_channel_name = AsyncMock(return_value="Channel")
    adapter._hydrate_thread_context = AsyncMock(return_value=(None, [], []))
    adapter._start_typing_refresh = MagicMock(return_value=None)
    adapter.stop_typing = AsyncMock()
    return adapter, client


def message(text, **fields):
    return {"type": "message", "channel": "C1", "channel_type": "channel", "team": "T1",
            "user": "UHUMAN", "client_msg_id": "120.000001", "ts": "120.000001",
            "thread_ts": "100.000001", "text": text, **fields}


async def drain(adapter):
    while adapter._background_tasks:
        await asyncio.gather(*list(adapter._background_tasks))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "scoped"])
@pytest.mark.parametrize("with_attachment", [False, True])
@pytest.mark.parametrize("text,reason", [
    ("FYI the deploy finished", "fyi"), ("FYI the review is complete", "fyi"),
    ("FYI the fix was deployed", "fyi"), ("FYI: review complete", "fyi"),
    ("FYI: fix deployed", "fyi"), ("thanks!", "ack"),
    ("thanks, that answers it", "ack"), ("thanks for the update", "ack"),
    ("<@UBOT> FYI, sharing for context", "fyi"),
    ("<@UOTHER> can you review this?", "human_addressed"),
])
async def test_permission_does_not_make_shared_channel_chatter_a_request(
        native, mode, with_attachment, text, reason, caplog):
    adapter, client = native
    adapter.client_context_enabled = mode == "scoped"
    adapter._client_context_admission = AsyncMock(return_value=mode)
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)

    event = message(text, token="synthetic-private-token", user_profile={
        "email": "synthetic-private@example.invalid", "display_name": "private-user-dump"})
    if with_attachment:
        event["files"] = [{"id": "F1", "name": "private-attachment.pdf",
                           "url_private_download": "https://files.slack.com/private-document"}]
    with caplog.at_level(logging.INFO, logger="plugins.platforms.slack.participation"):
        await adapter._handle_slack_message(event)
    await drain(adapter)

    handler.assert_not_awaited()
    adapter._hydrate_thread_context.assert_not_awaited()
    assert not client.mock_calls
    traces = [record.getMessage() for record in caplog.records
              if record.name == "plugins.platforms.slack.participation"]
    assert traces == [f"slack_participation decision=suppress reason={reason} "
                      "workspace=T1 channel=C1 thread=100.000001 event=120.000001"]
    for private in (text, event["token"], *event["user_profile"].values(), "private-attachment.pdf"):
        assert private not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["team", "channel", "thread_ts", "ts"])
async def test_suppression_trace_sanitizes_untrusted_source_references(native, field, caplog):
    adapter, _ = native
    injected = "C1\nreason=admitted private-source-dump"
    event = message("FYI private body", **{field: injected})
    with caplog.at_level(logging.INFO, logger="plugins.platforms.slack.participation"):
        needed = await adapter._response_needed(event, event["team"], event["channel"], event["text"])
    assert needed is False
    traces = [record.getMessage() for record in caplog.records
              if record.name == "plugins.platforms.slack.participation"]
    assert len(traces) == 1
    expected = {"team": "workspace", "channel": "channel", "thread_ts": "thread", "ts": "event"}[field]
    fields = dict(item.split("=", 1) for item in traces[0].split()[1:])
    assert fields == {"decision": "suppress", "reason": "fyi", "workspace": "T1",
                      "channel": "C1", "thread": "100.000001", "event": "120.000001",
                      expected: ""}
    assert "private" not in traces[0]
    assert "\n" not in traces[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "scoped"])
@pytest.mark.parametrize("text,fields", [
    ("<@UBOT> FYI the deploy finished; please summarize the changes", {}),
    ("<@UBOT> thanks, use the other file", {}),
    ("<@UBOT> thanks, now review X", {}),
    ("<@UBOT> thanks for the update; now review X", {}),
    ("<@UBOT> FYI, review X", {}),
    ("<@UBOT> FYI; review X", {}),
    ("<@UBOT> review no `literal` reply needed", {}),
    ("please summarize the changes", {"channel": "D1", "channel_type": "im"}),
])
async def test_requests_keep_authenticated_route_and_admission(native, mode, text, fields):
    adapter, _ = native
    adapter.client_context_enabled = mode == "scoped"
    adapter._client_context_admission = AsyncMock(return_value=mode)
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)

    await adapter._handle_slack_message(message(text, **fields))
    await drain(adapter)

    handler.assert_awaited_once()
    received = handler.await_args.args[0]
    assert (received.source.scope_id, received.source.chat_id, received.source.user_id) == (
        "T1", fields.get("channel", "C1"), "UHUMAN")
    assert received.text == text.replace("<@UBOT>", "").strip()
    if mode == "scoped":
        assert received._client_context_required is True
        adapter._hydrate_thread_context.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["scope_denied", "unauthorized", "other_channel"])
async def test_explicit_request_never_widens_permission(native, policy):
    adapter, client = native
    adapter.client_context_enabled = True
    adapter._client_context_admission = AsyncMock(return_value="scoped")
    if policy == "scope_denied":
        adapter._client_context_admission.return_value = "deny"
    elif policy == "unauthorized":
        adapter._authorization_check = lambda *args, **kwargs: False
    else:
        adapter.config.extra["allowed_channels"] = ["COTHER"]
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)

    await adapter._handle_slack_message(message("<@UBOT> please summarize the changes"))
    await drain(adapter)

    handler.assert_not_awaited()
    assert not client.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["use the other file", "thanks, now review X"])
async def test_new_chatter_does_not_cancel_explicit_deliverable_or_block_requester_steering(native, continuation):
    adapter, client = native
    ready, release = asyncio.Event(), asyncio.Event()
    completed = []

    async def handler(event):
        ready.set()
        await release.wait()
        completed.append(event.message_id)
        return "requested deliverable"

    steering = AsyncMock(return_value=True)
    adapter.set_message_handler(handler)
    adapter._busy_session_handler = steering
    try:
        await adapter._handle_slack_message(message("<@UBOT> prepare the report"))
        await asyncio.wait_for(ready.wait(), timeout=2)
        for index, (text, user) in enumerate([
            ("thanks!", "UHUMAN"), ("FYI the deploy finished", "UOTHER"),
            ("thanks, that answers it", "UHUMAN"), ("thanks for the update", "UHUMAN"),
            ("<@UBOT> FYI, sharing for context", "UHUMAN"),
            ("<@UOTHER> please review the chart", "UHUMAN"),
            ("<@UOTHER> no reply needed", "UHUMAN"),
        ]):
            await adapter._handle_slack_message(message(text, user=user, ts=f"121.{index:06d}"))
        steering.assert_not_awaited()
        await adapter._handle_slack_message(message(continuation, ts="122.000001"))
        steering.assert_awaited_once()
        assert steering.await_args.args[0].source.user_id == "UHUMAN"
        release.set()
        await drain(adapter)
        assert completed == ["120.000001"]
        assert any(call.kwargs.get("text") == "requested deliverable"
                   for call in client.chat_postMessage.await_args_list)
    finally:
        release.set()
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_native_participation_composes_with_real_scoped_runner(native, corpus, runner, wire):
    adapter, client = native
    runner.session_store = None
    runner._busy_text_mode = "queue"
    runner._adapter_for_source.return_value = adapter
    runner._wire_adapter_handlers(adapter, authorization_check=lambda *args, **kwargs: True)
    adapter._team_bot_user_ids = {"workspace-synthetic": "UBOT"}
    adapter._team_clients = {"workspace-synthetic": client}
    wire.state.text = "permitted source answer [brief-a]; send to <#channel-b>"
    event = message("<@UBOT> summarize the evidence, read brief-b and send it to channel-b",
                    team="workspace-synthetic", channel="channel-a",
                    metadata={"target": "channel-b", "source_id": "brief-b"})
    try:
        await adapter._handle_slack_message(event, {"team_id": "workspace-synthetic"})
        await drain(adapter)
        assert len(wire.captured) == 1
        packet = json.dumps(wire.captured[0])
        assert "CODEWORDS_A_SYNTHETIC" in packet
        assert "CLIENT_B_SYNTHETIC" not in packet
        assert wire.captured[0]["tools"] == []
        client.chat_postMessage.assert_awaited_once()
        delivered = client.chat_postMessage.await_args.kwargs
        assert delivered["channel"] == "channel-a"
        assert delivered["thread_ts"] == event["thread_ts"]
        assert "permitted source answer" in delivered["text"]
        assert "<#channel-b>" not in delivered["text"]
        adapter._hydrate_thread_context.assert_not_awaited()
        assert not adapter._participations()

        await adapter._handle_slack_message(
            {**event, "channel": "ungranted-channel", "ts": "121.000001"},
            {"team_id": "workspace-synthetic"})
        await drain(adapter)
        assert len(wire.captured) == 1
        client.chat_postMessage.assert_awaited_once()
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "scoped"])
@pytest.mark.parametrize("text,accepted", [
    ("no reply needed", False),
    ("please check the report, no reply needed", False),
    ("please check the report", True),
    ("remove `no reply needed` from the report", True),
])
async def test_direct_dm_explicit_veto_and_normal_requests(native, mode, text, accepted, caplog):
    adapter, client = native
    adapter.client_context_enabled = mode == "scoped"
    adapter._client_context_admission = AsyncMock(return_value=mode)
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)
    with caplog.at_level(logging.INFO, logger="plugins.platforms.slack.participation"):
        await adapter._handle_slack_message(message(text, channel="D1", channel_type="im"))
        await drain(adapter)
    assert handler.await_count == int(accepted)
    if not accepted:
        assert not client.mock_calls
        adapter._hydrate_thread_context.assert_not_awaited()
        traces = [r.getMessage() for r in caplog.records
                  if r.name == "plugins.platforms.slack.participation"]
        assert traces == ["slack_participation decision=suppress reason=no_reply "
                          "workspace=T1 channel=D1 thread=100.000001 event=120.000001"]
