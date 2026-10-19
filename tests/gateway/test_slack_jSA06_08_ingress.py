"""Native Slack ingress regressions for JSA-06/07 and fail-closed routing for JSA-08.

Ingress cases dispatch through the installed Slack Bolt SDK after the adapter's
production ``connect``/listener-registration path. Network/API calls are disabled;
only authorization, identity enrichment, and the gateway handoff boundary are stubbed.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("slack_bolt.async_app")
from slack_bolt.async_app import AsyncApp
from slack_bolt.request.async_request import AsyncBoltRequest
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from gateway.config import PlatformConfig
from plugins.platforms.slack import adapter as slack_module


TEAM = "T1"
OTHER_TEAM = "T2"
CHANNEL = "C1"
OTHER_CHANNEL = "C2"
THIRD_CHANNEL = "C3"
MESSAGE_TS = "1787365409.908499"
EDIT_TS = "1787365412.000001"
USER = "UUSER"
BOT = "UBOT"


def _message(channel=CHANNEL, team=TEAM, ts=MESSAGE_TS, *, text=None):
    return {
        "type": "message",
        "channel": channel,
        "channel_type": "channel",
        "team": team,
        "user": USER,
        "client_msg_id": f"cmid-{team}-{channel}-{ts}",
        "ts": ts,
        "text": text or f"<@{BOT}> investigate this",
    }


def _edit(channel=CHANNEL, team=TEAM, message_ts=MESSAGE_TS, event_ts=EDIT_TS):
    return {
        "type": "message",
        "subtype": "message_changed",
        "channel": channel,
        "channel_type": "channel",
        "team": team,
        "ts": event_ts,
        "event_ts": event_ts,
        "message": {
            "type": "message",
            "channel": channel,
            "channel_type": "channel",
            "team": team,
            "user": USER,
            "client_msg_id": f"cmid-{team}-{channel}-{message_ts}",
            "ts": message_ts,
            "text": f"<@{BOT}> edited request",
            "edited": {"user": USER, "ts": event_ts},
        },
    }


def _body(event, *, team=TEAM, event_id="EvIngress"):
    return {
        "type": "event_callback",
        "team_id": team,
        "event_id": event_id,
        "authorizations": [{"team_id": team, "user_id": BOT, "is_bot": True}],
        "event": event,
    }


async def _dispatch(adapter, body):
    assert isinstance(adapter._app, AsyncApp), "must exercise the real installed Bolt SDK"
    return await adapter._app.async_dispatch(
        AsyncBoltRequest(body=body, mode="socket_mode"))


async def _connected_adapter(monkeypatch):
    """Connect the production adapter without network/config/credential access."""
    assert isinstance(AsyncApp, type)
    monkeypatch.setattr(
        AsyncWebClient, "api_call",
        AsyncMock(side_effect=AssertionError("unexpected Slack network call")))
    monkeypatch.setattr(
        AsyncWebClient, "auth_test",
        AsyncMock(return_value=AsyncSlackResponse(
            client=None, http_verb="POST", api_url="https://slack.com/api/auth.test",
            req_args={}, data={"ok": True, "team_id": TEAM, "user_id": BOT,
                               "bot_id": "BBOT", "team": "test", "user": "jarvis"},
            headers={}, status_code=200)))
    monkeypatch.setattr(slack_module, "_load_slack_bot_tokens", lambda raw, quiet: ["xoxb-fake"])
    monkeypatch.setattr(
        slack_module, "_get_scoped_secret",
        lambda name, default=None: "xapp-fake" if name == "SLACK_APP_TOKEN" else default or "")
    monkeypatch.setattr(slack_module, "_resolve_slack_proxy_url", lambda: None)
    monkeypatch.setattr(
        slack_module, "AsyncApp",
        lambda **kwargs: AsyncApp(name="jSA06-08-test", process_before_response=True, **kwargs))

    adapter = slack_module.SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    bolt_logger = logging.getLogger("slack_bolt.AsyncApp")
    monkeypatch.setattr(bolt_logger, "disabled", True)
    connection_errors = Mock()
    monkeypatch.setattr(slack_module.logger, "error", connection_errors)
    adapter.set_authorization_check(lambda user_id, *args, **kwargs: bool(user_id))
    monkeypatch.setattr(adapter, "_acquire_platform_lock", Mock(return_value=True))
    monkeypatch.setattr(adapter, "_release_platform_lock", Mock())
    monkeypatch.setattr(adapter, "_start_socket_mode_handler", Mock())
    monkeypatch.setattr(adapter, "_ensure_socket_watchdog", Mock())
    monkeypatch.setattr(adapter, "_stop_socket_mode_handler", AsyncMock())
    monkeypatch.setattr(adapter, "_close_workspace_clients", AsyncMock())
    monkeypatch.setattr(adapter, "_wire_plugin_handlers", Mock())
    monkeypatch.setattr(adapter, "stop_typing", AsyncMock())
    monkeypatch.setattr(adapter, "_resolve_user_name", AsyncMock(return_value="operator"))
    monkeypatch.setattr(adapter, "_resolve_channel_name", AsyncMock(return_value="test-channel"))
    monkeypatch.setattr(adapter, "_resolve_user_is_bot", AsyncMock(return_value=False))
    monkeypatch.setattr(adapter, "_reactions_enabled", Mock(return_value=False))
    connected = await adapter.connect()
    logged = connection_errors.call_args.args[1] if connection_errors.call_args else None
    traceback_frames = []
    trace = getattr(logged, "__traceback__", None)
    while trace is not None:
        traceback_frames.append((trace.tb_frame.f_code.co_filename, trace.tb_lineno))
        trace = trace.tb_next
    assert connected, f"production connect failed: {logged!r}; frames={traceback_frames}"
    assert adapter._app is not None
    adapter._app._framework_logger.disabled = True
    return adapter


@pytest.mark.asyncio
async def test_message_changed_same_timestamp_in_other_workspace_and_channel_is_independent(monkeypatch):
    adapter = await _connected_adapter(monkeypatch)
    # T2 and C2 are independent authenticated/event provenance, not SDK/network calls.
    adapter._team_clients[OTHER_TEAM] = object()
    adapter._team_bot_user_ids[OTHER_TEAM] = BOT
    adapter._team_bot_names[OTHER_TEAM] = "jarvis"
    received = []
    adapter.handle_message = AsyncMock(side_effect=lambda event: received.append(event))

    assert (await _dispatch(adapter, _body(_message(), event_id="EvOriginal"))).status == 200
    # Same original message ts and channel ID, but a different workspace.
    cross_workspace = _edit(CHANNEL, OTHER_TEAM)
    assert (await _dispatch(
        adapter, _body(cross_workspace, team=OTHER_TEAM, event_id="EvEditT2"))).status == 200
    # Same workspace and message ts in another channel. Keep the same edit
    # timestamp as the T2 case, so dedup itself must include the channel.
    cross_channel = _edit(OTHER_CHANNEL, TEAM, event_ts=EDIT_TS)
    assert (await _dispatch(adapter, _body(cross_channel, event_id="EvEditC2"))).status == 200
    # A second same-workspace channel with the same edit timestamp is also independent.
    third_channel = _edit(THIRD_CHANNEL, TEAM, event_ts=EDIT_TS)
    assert (await _dispatch(adapter, _body(third_channel, event_id="EvEditC3"))).status == 200

    assert len(received) == 4
    assert {(event.source.chat_id, event.source.scope_id) for event in received} == {
        (CHANNEL, TEAM), (CHANNEL, OTHER_TEAM), (OTHER_CHANNEL, TEAM), (THIRD_CHANNEL, TEAM)}
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_same_edit_event_duplicate_is_suppressed_by_real_bolt_dispatch(monkeypatch):
    adapter = await _connected_adapter(monkeypatch)
    received = []
    adapter.handle_message = AsyncMock(side_effect=lambda event: received.append(event))
    body = _body(_edit(), event_id="EvSameEdit")

    first = await _dispatch(adapter, body)
    duplicate = await _dispatch(adapter, body)

    assert first.status == duplicate.status == 200
    assert adapter.handle_message.await_count == 1
    assert len(received) == 1
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_downstream_failure_releases_only_its_claim_for_bolt_redelivery(monkeypatch):
    adapter = await _connected_adapter(monkeypatch)
    delivered = []
    attempts = []

    async def fail_then_deliver(event):
        attempts.append(event)
        if len(attempts) == 1:
            raise RuntimeError("synthetic gateway handoff failure")
        delivered.append(event)

    adapter.handle_message = AsyncMock(side_effect=fail_then_deliver)
    body = _body(_message(), event_id="EvRetry")

    await _dispatch(adapter, body)
    retry = await _dispatch(adapter, body)

    assert retry.status == 200
    assert adapter.handle_message.await_count == 2
    assert len(delivered) == 1
    assert (TEAM, CHANNEL, MESSAGE_TS) in adapter._processed_message_ts
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_cancelled_bolt_handler_releases_owned_claim_for_retry(monkeypatch):
    adapter = await _connected_adapter(monkeypatch)
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def cancelled_handoff(_event):
        entered.set()
        await hold.wait()

    adapter.handle_message = AsyncMock(side_effect=cancelled_handoff)
    body = _body(_message(), event_id="EvCancelled")
    dispatch = asyncio.create_task(_dispatch(adapter, body))
    await asyncio.wait_for(entered.wait(), timeout=3)
    dispatch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch

    assert (TEAM, CHANNEL, MESSAGE_TS) not in adapter._processed_message_ts
    adapter.handle_message = AsyncMock(return_value=None)
    retry = await _dispatch(adapter, body)
    assert retry.status == 200
    adapter.handle_message.assert_awaited_once()
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_true_duplicate_is_suppressed_while_first_bolt_invocation_is_in_flight(monkeypatch):
    adapter = await _connected_adapter(monkeypatch)
    entered = asyncio.Event()
    finish = asyncio.Event()
    delivered = []

    async def slow_handoff(event):
        delivered.append(event)
        entered.set()
        await finish.wait()

    adapter.handle_message = AsyncMock(side_effect=slow_handoff)
    body = _body(_message(), event_id="EvConcurrent")
    first = asyncio.create_task(_dispatch(adapter, body))
    await asyncio.wait_for(entered.wait(), timeout=3)
    second = await _dispatch(adapter, body)
    finish.set()
    first_response = await asyncio.wait_for(first, timeout=3)

    assert first_response.status == second.status == 200
    assert adapter.handle_message.await_count == 1
    assert len(delivered) == 1
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_failed_owner_does_not_leave_inflight_edit_claim_or_block_redrive(monkeypatch):
    adapter = await _connected_adapter(monkeypatch)
    entered = asyncio.Event()
    finish = asyncio.Event()
    attempts = []

    async def fail_after_wait(event):
        attempts.append(event)
        if len(attempts) == 1:
            entered.set()
            await finish.wait()
            raise RuntimeError("synthetic gateway handoff failure")

    adapter.handle_message = AsyncMock(side_effect=fail_after_wait)
    original_body = _body(_message(), event_id="EvOwnerFails")
    owner = asyncio.create_task(_dispatch(adapter, original_body))
    await asyncio.wait_for(entered.wait(), timeout=3)
    # A different edit event for the same channel/workspace/message is not the
    # owner's dedup event, but cannot take over or clear its live processed claim.
    edit_body = _body(_edit(event_ts="1787365412.000003"), event_id="EvConcurrentEdit")
    await _dispatch(adapter, edit_body)
    assert adapter.handle_message.await_count == 1
    finish.set()
    await asyncio.wait_for(owner, timeout=3)

    # Once the owner fails, both its processed claim and its newly acquired
    # dedup reservation are released; a new edit can now drive the message.
    retry = await _dispatch(adapter, edit_body)
    assert retry.status == 200
    assert adapter.handle_message.await_count == 2
    assert len(attempts) == 2
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_expired_duplicate_claim_cannot_poison_retry_after_inflight_owner_fails(monkeypatch):
    adapter = await _connected_adapter(monkeypatch)
    entered = asyncio.Event()
    finish = asyncio.Event()
    attempts = []

    async def fail_then_deliver(event):
        attempts.append(event)
        if len(attempts) == 1:
            entered.set()
            await finish.wait()
            raise RuntimeError("synthetic late handoff failure")

    adapter.handle_message = AsyncMock(side_effect=fail_then_deliver)
    body = _body(_message(), event_id="EvExpiredInFlight")
    owner = asyncio.create_task(_dispatch(adapter, body))
    await asyncio.wait_for(entered.wait(), timeout=3)
    key = adapter._workspace_event_id(TEAM, CHANNEL, MESSAGE_TS)
    adapter._dedup._seen[key] -= adapter._dedup._ttl + 1
    await _dispatch(adapter, body)
    assert adapter.handle_message.await_count == 1
    finish.set()
    await asyncio.wait_for(owner, timeout=3)

    await _dispatch(adapter, body)
    assert adapter.handle_message.await_count == 2
    assert (TEAM, CHANNEL, MESSAGE_TS) in adapter._processed_message_ts
    await adapter.disconnect()


def _routing_adapter():
    adapter = slack_module.SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    primary = SimpleNamespace(
        chat_postMessage=AsyncMock(), chat_postEphemeral=AsyncMock(), api_call=AsyncMock(),
        chat_startStream=AsyncMock(), chat_appendStream=AsyncMock())
    team_one = SimpleNamespace(
        chat_postMessage=AsyncMock(), chat_postEphemeral=AsyncMock(), api_call=AsyncMock(),
        chat_startStream=AsyncMock(), chat_appendStream=AsyncMock())
    team_two = SimpleNamespace(
        chat_postMessage=AsyncMock(), chat_postEphemeral=AsyncMock(), api_call=AsyncMock(),
        chat_startStream=AsyncMock(), chat_appendStream=AsyncMock())
    for index, client in enumerate((primary, team_one, team_two), start=1):
        message_ts = f"{index}.0"
        client.chat_postMessage.return_value = {"ok": True, "ts": message_ts}
        client.chat_postEphemeral.return_value = {"ok": True, "message_ts": message_ts}
        client.api_call.return_value = {"ok": True, "ts": message_ts}
        client.chat_startStream.return_value = {"ok": True, "ts": message_ts}
        client.chat_appendStream.return_value = {"ok": True, "ts": message_ts}
    adapter._app = SimpleNamespace(client=primary)
    adapter._native_stream_unsupported = False
    adapter._team_clients = {TEAM: team_one, OTHER_TEAM: team_two}
    adapter._team_bot_user_ids = {TEAM: BOT, OTHER_TEAM: BOT}
    adapter._channel_team = {CHANNEL: TEAM, OTHER_CHANNEL: OTHER_TEAM}
    adapter._channel_teams = {CHANNEL: {TEAM}, OTHER_CHANNEL: {OTHER_TEAM}}
    return adapter, primary, team_one, team_two


def test_workspace_client_routing_rejects_unknown_and_conflicting_explicit_provenance():
    adapter, _primary, team_one, team_two = _routing_adapter()

    assert adapter._get_client(CHANNEL, team_id=TEAM) is team_one
    assert adapter._get_client(OTHER_CHANNEL) is team_two
    assert adapter._get_client("C-UNMAPPED") is adapter._app.client
    with pytest.raises(ValueError, match="workspace"):
        adapter._get_client(OTHER_CHANNEL, team_id=TEAM)
    with pytest.raises(ValueError, match="workspace"):
        adapter._get_client(CHANNEL, team_id="STALE-T1")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["send", "private_notice", "draft"])
@pytest.mark.parametrize("provenance", ["unknown", "conflicting"])
async def test_invalid_workspace_provenance_aborts_send_paths_before_sdk_calls(
        monkeypatch, kind, provenance):
    adapter, _primary, team_one, team_two = _routing_adapter()
    # Keep only workspace provenance under test; outbound SDK clients are network boundaries.
    monkeypatch.setattr(adapter, "_check_deletion_scope", Mock())
    adapter._resolve_user_name = AsyncMock(return_value="operator")
    adapter._resolve_channel_name = AsyncMock(return_value="test-channel")
    adapter.stop_typing = AsyncMock()
    explicit_team = "STALE-T1" if provenance == "unknown" else TEAM
    metadata = {"team_id": explicit_team, "thread_id": "1787365409.908400"}

    if kind == "send":
        result = await adapter.send(OTHER_CHANNEL, "should not be delivered", metadata=metadata)
    elif kind == "private_notice":
        result = await adapter.send_private_notice(
            OTHER_CHANNEL, USER, "should not be delivered", metadata=metadata)
    else:
        result = await adapter.send_draft(
            OTHER_CHANNEL, 1, "should not be delivered", metadata=metadata)

    assert not result.success
    for client in (_primary, team_one, team_two):
        client.chat_postMessage.assert_not_awaited()
        client.chat_postEphemeral.assert_not_awaited()
        client.api_call.assert_not_awaited()
        client.chat_startStream.assert_not_awaited()
        client.chat_appendStream.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_explicit_inferred_and_default_send_routing_is_preserved(monkeypatch):
    adapter, primary, team_one, team_two = _routing_adapter()
    # This test isolates workspace selection from the separately-tested per-turn
    # participation policy. The network-facing clients remain AsyncMock boundaries.
    monkeypatch.setattr(adapter, "_check_deletion_scope", Mock())
    primary.chat_postMessage.return_value = {"ok": True, "ts": "1.0"}
    team_one.chat_postMessage.return_value = {"ok": True, "ts": "2.0"}
    team_two.chat_postMessage.return_value = {"ok": True, "ts": "3.0"}

    explicit = await adapter.send(CHANNEL, "explicit", metadata={"team_id": TEAM})
    inferred = await adapter.send(OTHER_CHANNEL, "inferred")
    default = await adapter.send("C-UNMAPPED", "primary default")

    assert explicit.success and inferred.success and default.success
    team_one.chat_postMessage.assert_awaited_once()
    team_two.chat_postMessage.assert_awaited_once()
    primary.chat_postMessage.assert_awaited_once()
    assert team_one.chat_postMessage.await_args.kwargs["channel"] == CHANNEL
    assert team_two.chat_postMessage.await_args.kwargs["channel"] == OTHER_CHANNEL
    assert primary.chat_postMessage.await_args.kwargs["channel"] == "C-UNMAPPED"
