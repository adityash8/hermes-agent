"""Authenticated-adapter fixtures and real bounded dispatch; never live Slack proof."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import hermes_yaml as yaml

from gateway import client_context as cc
from gateway import client_context_restricted as route
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent
from gateway.session import SessionSource
from tests.gateway.test_client_context import corpus, forbidden, runner, wire
from tests.gateway.test_slack_channel_isolation import native_text, drain_native, SETTING
from tests.gateway.test_rhiz_spi_reads import dates, transport


@pytest.fixture
def lane(native_text, runner, tmp_path, monkeypatch):
    adapter, client, payload = native_text
    runner.config.client_context = {"enabled": False}
    adapter.set_authorization_check(lambda *args, **kwargs: True)
    payload.update(channel="Cpublic", user=route.USER)
    client.users_info.return_value["user"].update(id=route.USER, team_id=route.WORKSPACE)
    raw = {"model": {"provider": "openai", "default": "synthetic-main"}, "gateway": {
        "restricted_users": {"slack": {route.USER: {"enabled_toolsets": ["clarify", "todo", "web", "spi"],
                                                    "system_prompt": "PRIVATE_LEGACY_PROMPT"}}}},
        "platform_toolsets": {"slack": ["memory", "terminal", "file", "delegation", "cronjob"]},
        "mcp_servers": {"composio": {"enabled": True, "command": "must-not-start"}}}
    path = tmp_path / "raw" / "config.yaml"
    path.parent.mkdir()
    def save():
        path.write_text(yaml.safe_dump(raw))
    save()
    monkeypatch.setattr("gateway.run._gateway_config_home", lambda: path.parent)
    runner._agent_cache = {"legacy": "PRIVATE_CACHED_AGENT"}
    runner._client_context_history = {"other-user": "PRIVATE_STORED_HISTORY"}
    return SimpleNamespace(adapter=adapter, client=client, payload=payload, raw=raw, save=save)


async def invoke(lane, body=None):
    await lane.adapter._handle_slack_message(lane.payload, {"team_id": route.WORKSPACE} if body is None else body)
    await drain_native(lane.adapter)


def call(name, args, ident="call-1"):
    return {"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def respond(wire, calls):
    wire.state.tool_calls = calls
    wire.state.finish = "tool_calls" if calls else "stop"
    wire.state.text = None if calls else "SPI sessions 1103; clicks 149."


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["message", "app_mention"])
async def test_verified_native_identity_real_resolver_sdk_spi_dispatch_and_delivery(lane, runner, wire, transport, dates, event_type):
    lane.payload["type"] = event_type
    calls = [call("spi_ga4_report", dates), call("spi_gsc_report", dates, "call-2")]
    wire.state.callback = lambda body: respond(wire, calls if len(wire.captured) == 1 else [])
    await invoke(lane)
    assert lane.client.users_info.await_count == 1
    assert len(wire.captured) == 2 and len(transport[0]) == 6
    assert lane.client.chat_postMessage.await_count == 1
    payload = lane.client.chat_postMessage.call_args.kwargs
    assert payload["channel"] == "Cpublic" and payload["thread_ts"] == "100.0"
    assert payload["parse"] == "none" and payload["unfurl_links"] is False and payload["mrkdwn"] is False
    names = {t["function"]["name"] for t in wire.captured[0]["tools"]}
    assert names == {"spi_ga4_report", "spi_gsc_report", "public_web_search", "clarify", "todo_list"}
    assert "PRIVATE_" not in json.dumps(wire.captured)
    assert runner._client_context_history == {"other-user": "PRIVATE_STORED_HISTORY"}
    assert runner.session_store is None
    source = SessionSource(Platform.SLACK, "Cpublic", user_id=route.USER, scope_id=route.WORKSPACE)
    assert runner._resolve_enabled_toolsets_for_source(lane.raw, source, "slack") == []
    source.user_id = "UOTHER"
    assert {"memory", "terminal", "composio"} <= set(runner._resolve_enabled_toolsets_for_source(lane.raw, source, "slack"))
    source.platform = Platform.TELEGRAM
    source.user_id = route.USER
    assert "composio" in runner._resolve_enabled_toolsets_for_source(lane.raw, source, "telegram")


@pytest.mark.asyncio
async def test_missing_admission_callback_denies_without_legacy_enrichment(lane, wire):
    lane.adapter._client_context_admission = None
    lane.adapter.client_context_enabled = False
    lane.adapter._prefilter_inbound = AsyncMock(side_effect=forbidden)
    await invoke(lane)
    assert not wire.captured
    lane.adapter._prefilter_inbound.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["message_changed", "message_deleted"])
async def test_nested_edit_author_cannot_enter_legacy_enrichment(lane, wire, kind):
    lane.payload.pop("user")
    lane.payload["subtype"] = kind
    lane.payload["message" if kind == "message_changed" else "previous_message"] = {"user": route.USER, "text": "private"}
    lane.adapter._prefilter_inbound = AsyncMock(side_effect=forbidden)
    await invoke(lane)
    assert not wire.captured
    lane.adapter._prefilter_inbound.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_later_spi_sibling_denies_whole_batch_without_network(lane, wire, transport, dates):
    respond(wire, [call("spi_ga4_report", dates), call("spi_gsc_report", {**dates, "account": "foreign"}, "call-2")])
    await invoke(lane)
    assert len(wire.captured) == 1 and not transport[0]
    lane.client.chat_postMessage.assert_not_called()


@pytest.mark.asyncio
async def test_no_persisted_delivery_replay_and_failed_send_has_no_plaintext_fallback(lane, wire, monkeypatch):
    monkeypatch.setattr("gateway.delivery_ledger.record_obligation", forbidden)
    lane.client.chat_postMessage.side_effect = RuntimeError("PRIVATE_TRANSPORT_ERROR")
    await invoke(lane)
    assert len(wire.captured) == 1
    assert lane.client.chat_postMessage.await_count == 1


@pytest.mark.asyncio
async def test_outer_lifecycle_error_does_not_send_raw_unsealed_exception(lane, wire):
    lane.adapter._send_final_text = AsyncMock(side_effect=RuntimeError("PRIVATE_OUTER_LIFECYCLE_ERROR"))
    await invoke(lane)
    assert len(wire.captured) == 1
    lane.client.chat_postMessage.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["workspace", "missing_workspace", "missing_client", "bot", "deleted", "wrong_member",
                                     "member_workspace", "auth_error", "file", "attachment", "slash", "bang", "no_summon", "revoked"])
async def test_native_denials_never_enter_operator_agent_or_spi(lane, runner, wire, transport, variant):
    body = {"team_id": route.WORKSPACE}
    if variant == "workspace":
        body["team_id"] = "TOTHER"
    elif variant == "missing_workspace":
        body = {}
    elif variant == "missing_client":
        lane.adapter._team_clients = {}
    elif variant in {"bot", "deleted"}:
        lane.client.users_info.return_value["user"]["is_bot" if variant == "bot" else "deleted"] = True
    elif variant == "wrong_member":
        lane.client.users_info.return_value["user"]["id"] = "UOTHER"
    elif variant == "member_workspace":
        lane.client.users_info.return_value["user"]["team_id"] = "TOTHER"
    elif variant == "auth_error":
        lane.client.users_info.side_effect = RuntimeError("PRIVATE_CREDENTIAL_ERROR")
    elif variant in {"file", "attachment"}:
        lane.payload["files" if variant == "file" else "attachments"] = [{}]
    elif variant in {"slash", "bang", "no_summon"}:
        lane.payload["text"] = {"slash": "<@U0B2X1D8R17> /approve", "bang": "<@U0B2X1D8R17> !new", "no_summon": "hello"}[variant]
    else:
        lane.raw["gateway"]["restricted_users"] = {}
        lane.save()
    await invoke(lane, body)
    assert not wire.captured and not transport[0]
    lane.client.chat_postMessage.assert_not_called()
    assert runner._client_context_history == {"other-user": "PRIVATE_STORED_HISTORY"}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "busy", "limit"])
async def test_early_runtime_denials_preserve_other_history_and_send_nothing(lane, runner, wire, monkeypatch, state):
    if state == "paused":
        monkeypatch.setattr("agent.estop.paused_reply", lambda: "paused")
    elif state == "busy":
        runner._is_session_running = lambda key: True
    else:
        runner._claim_active_session_slot.return_value = (None, "full")
    await invoke(lane)
    assert not wire.captured
    lane.client.chat_postMessage.assert_not_called()
    assert runner._client_context_history == {"other-user": "PRIVATE_STORED_HISTORY"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["terminal", "read_file", "delegate_task", "memory", "session_search", "cronjob", "skill_view",
                                  "tool_search", "tool_call", "execute_code", "COMPOSIO_MULTI_EXECUTE_TOOL", "mcp_composio_new_dynamic_tool"])
async def test_stale_dynamic_generic_and_delegated_calls_denied_at_actual_dispatch(lane, wire, transport, name):
    respond(wire, [call(name, {"toolsets": ["terminal"], "user_id": route.USER})])
    await invoke(lane)
    assert len(wire.captured) == 1 and not transport[0]
    lane.client.chat_postMessage.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before_tool", "after_tool", "delivery"])
async def test_config_revocation_aborts_inflight_tool_or_delivery(lane, wire, transport, dates, when):
    def revoke():
        lane.raw["gateway"]["restricted_users"] = {}
        lane.save()
    if when == "before_tool":
        def response(body):
            respond(wire, [call("spi_ga4_report", dates)])
            revoke()
        wire.state.callback = response
    elif when == "after_tool":
        respond(wire, [call("spi_ga4_report", dates)])
        transport[1]["after_report"] = revoke
    else:
        original = lane.adapter._extract_response_content
        async def extract(*args, **kwargs):
            result = await original(*args, **kwargs)
            revoke()
            return result
        lane.adapter._extract_response_content = extract
    await invoke(lane)
    lane.client.chat_postMessage.assert_not_called()
    assert len(transport[0]) == (3 if when == "after_tool" else 0)


@pytest.mark.asyncio
async def test_missing_receipt_and_mutated_sender_cannot_use_spi(lane, runner, wire, transport):
    source = SessionSource(Platform.SLACK, "Cpublic", user_id=route.USER, scope_id=route.WORKSPACE)
    trigger = MessageEvent(text="SPI report", source=source)
    assert await runner._handle_message(trigger) is None
    assert await runner._handle_message_with_agent(trigger, source, "fake", 1) is None
    from plugins.platforms.slack.client_context import admit_event
    mode, source = await admit_event(lane.adapter, lane.payload, {"team_id": route.WORKSPACE})
    assert mode == "scoped"
    trigger = MessageEvent(text="SPI report", source=source)
    trigger._restricted_request = route.seal_event(trigger)
    source.user_id = "UOTHER"
    assert await runner._handle_message(trigger) is None
    assert not wire.captured and not transport[0]


@pytest.mark.asyncio
async def test_codewords_precedence_cannot_dispatch_spi(lane, runner, wire, transport, dates):
    runner.config.client_context = copy.deepcopy(SETTING)
    lane.payload["channel"] = route.CODEWORDS
    respond(wire, [call("spi_ga4_report", dates)])
    await invoke(lane)
    assert len(wire.captured) == 1
    assert "spi_ga4_report" not in {t["function"]["name"] for t in wire.captured[0]["tools"]}
    assert not transport[0]
    lane.client.chat_postMessage.assert_not_called()


@pytest.mark.asyncio
async def test_shared_thread_has_separate_restricted_key_and_drops_busy_requests(lane, runner, wire):
    from plugins.platforms.slack.client_context import admit_event, dispatch_scoped
    _, source = await admit_event(lane.adapter, lane.payload, {"team_id": route.WORKSPACE})
    source.thread_id = "100.0"
    restricted_event = MessageEvent(text="SPI request", source=source)
    other = copy.copy(source)
    del other._restricted_native
    other.user_id = "UOTHER"
    assert lane.adapter._event_session_key(restricted_event) != lane.adapter._event_session_key(MessageEvent(text="private", source=other))
    key = lane.adapter._event_session_key(restricted_event)
    lane.adapter._active_sessions[key] = object()
    lane.adapter._handle_message_while_active = AsyncMock(side_effect=forbidden)
    lane.adapter._queue_participation = Mock(side_effect=forbidden)
    await dispatch_scoped(lane.adapter, lane.payload, source)
    assert not wire.captured


@pytest.mark.asyncio
async def test_legacy_grant_cannot_smuggle_prompt_or_spi_and_other_sender_stays_legacy(lane, runner, wire):
    lane.raw["gateway"]["restricted_users"]["slack"][route.USER]["enabled_toolsets"] = [
        "clarify", "delegation", "file", "linear", "skills", "terminal", "todo", "vision", "web"]
    lane.save()
    await invoke(lane)
    assert {t["function"]["name"] for t in wire.captured[0]["tools"]} == {"clarify", "todo_list", "public_web_search"}
    assert "PRIVATE_LEGACY_PROMPT" not in json.dumps(wire.captured)
    source = SessionSource(Platform.SLACK, "Cpublic", user_id="UOTHER", scope_id=route.WORKSPACE)
    assert await cc.admission(runner, source) == "legacy"
    assert not cc.applies(runner.config, source)


@pytest.mark.asyncio
async def test_slack_connect_guest_without_team_id_is_admitted(lane, wire):
    """users.info for a Slack Connect guest can omit team_id. That must not deny the DM."""
    user = lane.client.users_info.return_value["user"]
    user.pop("team_id", None)
    lane.payload.update(channel="D0B2NP9NG5R", channel_type="im", ts="1790676057.840929", thread_ts="1790667008.511259")
    await invoke(lane)
    assert len(wire.captured) == 1
    assert lane.client.chat_postMessage.await_count == 1



@pytest.mark.asyncio
async def test_dotted_dm_thread_id_is_admitted_not_denied(lane, wire):
    """Slack DM thread roots are timestamps. A dot must not ContextDenied the turn."""
    lane.payload.update(
        channel="D0B2NP9NG5R", channel_type="im",
        ts="1790668418.361499", thread_ts="1790667008.511259",
        text="same thread, still no reply",
    )
    await invoke(lane)
    assert len(wire.captured) == 1
    assert lane.client.chat_postMessage.await_count == 1
    sent = lane.client.chat_postMessage.call_args.kwargs
    assert sent["channel"] == "D0B2NP9NG5R"
    assert sent["thread_ts"] == "1790667008.511259"
