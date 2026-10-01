"""Bounded native bounded read access; synthetic provider transport, no live Slack."""
import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import client_context as cc
from gateway.client_context_policy import ContextDenied
from tests.gateway.test_client_context import corpus, runner, wire, event, forbidden

CHANNEL = "C09PF4QGQMC"
SETTING = {"enabled": False, "codewords_read_channels": {CHANNEL: {
    "scope_id": "T07MMEX5658", "provider": "openai", "model": "synthetic-main", "generation": "v1",
}}}


@pytest.mark.parametrize("provider,ok", [
    ("openai", True),
    ("openai-codex", True),
    ("openrouter", True),
    ("xai-oauth", True),
    ("anthropic", False),
    ("xai", False),
])
def test_codewords_route_accepts_xai_oauth(runner, provider, ok):
    from gateway.client_context_codewords import route

    runner.config.client_context = copy.deepcopy(SETTING)
    runner.config.client_context["codewords_read_channels"][CHANNEL]["provider"] = provider
    runner.config.client_context["codewords_read_channels"][CHANNEL]["model"] = "grok-4.6"
    source = event(chat_id=CHANNEL, scope_id="T07MMEX5658", user_id="U07MJJMMK9T").source
    if ok:
        policy = route(runner.config, source)
        assert policy.completion.provider == provider
        assert policy.completion.model == "grok-4.6"
    else:
        with pytest.raises(ContextDenied):
            route(runner.config, source)


@pytest.mark.asyncio
async def test_native_admission_allows_xai_oauth(native_text, runner, wire):
    runner.config.client_context["codewords_read_channels"][CHANNEL]["provider"] = "xai-oauth"
    adapter, client, payload = native_text
    await adapter._handle_slack_message(payload, {"team_id": "T07MMEX5658"})
    await drain_native(adapter)
    assert len(wire.captured) == 1
    assert client.chat_postMessage.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", ["Cother", "Downer"])
@pytest.mark.parametrize("outer", ["valid", "missing-enabled", "malformed-enabled"])
async def test_bounded_policy_preserves_other_routes(runner, chat, outer):
    runner.config.client_context = copy.deepcopy(SETTING)
    if outer == "missing-enabled":
        runner.config.client_context.pop("enabled")
    elif outer == "malformed-enabled":
        runner.config.client_context["enabled"] = "true"
    runner._hm_admit_event = AsyncMock(return_value=None)
    trigger = event(chat_id=chat, scope_id=None, chat_type="dm" if chat.startswith("D") else "channel")
    assert await cc.admission(runner, trigger.source) == "legacy"
    assert await runner._handle_message(trigger) is None
    runner._hm_admit_event.assert_awaited_once_with(trigger)


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_current_codewords_provider_boundary(runner, wire, monkeypatch, admin):
    runner.config.client_context = copy.deepcopy(SETTING)
    runner._is_user_authorized_for_source.return_value = admin
    runner._adapter_for_source.return_value.authorize_turn = Mock(return_value=True)
    trigger = event(chat_id=CHANNEL, scope_id="T07MMEX5658", thread_id="1789084800.000001",
                    user_id="U07MJJMMK9T" if admin else "U092L0QKYMB")
    trigger.message_id = "1789430400.000001"
    # Native adapter authentication is independently exercised below. This test
    # focuses on the real gateway/provider boundary using the same server seal.
    try:
        from gateway.client_context_codewords import native_identity, route, seal_event
    except ImportError:
        pytest.fail("bounded bounded read route is absent")
    trigger.source._codewords_native_identity = native_identity(trigger.source)
    trigger.source._codewords_policy = route(runner.config, trigger.source)
    trigger._codewords_request = seal_event(trigger)
    runner._client_context_history = {"other-route": "PRIVATE_HISTORY"}
    monkeypatch.setattr(cc, "_prepare", forbidden)
    reply = await runner._handle_message(trigger)
    assert isinstance(reply, cc.ScopedReply) and reply not in {cc.DENIED, cc.FAILED}
    assert len(wire.captured) == 1
    request = wire.captured[0]
    assert {tool["function"]["name"] for tool in request["tools"]} == {
        binding.name for binding in trigger.source._codewords_policy.resources} | {"codewords_slack_history_read"}
    assert request["tool_choice"] == "auto"
    assert [m["role"] for m in request["messages"]] == ["system", "user"]
    assert json.loads(request["messages"][1]["content"])["current_request"] == trigger.text
    assert "PRIVATE_HISTORY" not in json.dumps(request)
    assert runner._client_context_history == {"other-route": "PRIVATE_HISTORY"}
    runner._is_user_authorized_for_source.assert_not_called()
    runner.session_store.assert_not_called()


def sealed(runner):
    from gateway.client_context_codewords import native_identity, route, seal_event
    runner.config.client_context = copy.deepcopy(SETTING)
    trigger = event(chat_id=CHANNEL, scope_id="T07MMEX5658", user_id="U092L0QKYMB",
                    thread_id="1789084800.000001")
    trigger.message_id = "1789430400.000001"
    if not hasattr(runner._adapter_for_source.return_value, "authorize_turn"):
        runner._adapter_for_source.return_value.authorize_turn = Mock(return_value=True)
    trigger.source._codewords_native_identity = native_identity(trigger.source)
    trigger.source._codewords_policy = route(runner.config, trigger.source)
    trigger._codewords_request = seal_event(trigger)
    return trigger


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["policy", "removed", "text", "workspace", "user", "bot", "internal", "media", "prompt", "slash", "unsealed"])
async def test_invalid_or_changed_request_never_reaches_provider(runner, wire, change):
    trigger = sealed(runner)
    if change == "policy":
        runner.config.client_context["codewords_read_channels"][CHANNEL]["read_tools"] = ["terminal"]
    elif change == "removed":
        runner.config.client_context = {"enabled": False}
    elif change == "text":
        trigger.text += " PRIVATE_INJECTED_CONTEXT"
    elif change == "workspace":
        trigger.source.scope_id = "Tforeign"
    elif change == "user":
        trigger.source.user_id = None
    elif change == "bot":
        trigger.source.is_bot = True
    elif change == "internal":
        trigger.internal = True
    elif change == "media":
        trigger.media_urls = ["file:///PRIVATE_FILE"]
    elif change == "prompt":
        trigger.prompt_response = {"option": "allow"}
    elif change == "slash":
        trigger.text = "/approve"
    else:
        del trigger._codewords_request
    await runner._handle_message(trigger)
    assert wire.captured == []


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["resolution", "response", "release"])
@pytest.mark.parametrize("change", ["generation", "text", "media", "identity"])
async def test_revalidate_before_transmit_and_release(runner, wire, monkeypatch, when, change):
    from gateway.client_context_policy import ContextDenied
    import agent.auxiliary_client as aux
    trigger = sealed(runner)
    def revoke():
        if change == "generation":
            runner.config.client_context["codewords_read_channels"][CHANNEL]["generation"] = "v2"
        elif change == "text":
            trigger.text += " PRIVATE_LATE_CONTEXT"
        elif change == "media":
            trigger.media_types = ["image/png"]
        else:
            trigger.source.user_id = "Uother"
    if when == "resolution":
        resolve = aux.resolve_provider_client
        def delayed(*args, **kwargs):
            result = resolve(*args, **kwargs)
            revoke()
            return result
        monkeypatch.setattr(aux, "resolve_provider_client", delayed)
    elif when == "response":
        wire.state.callback = lambda body: revoke()
    reply = await runner._handle_message(trigger)
    if when == "release":
        assert isinstance(reply, cc.ScopedReply)
        revoke()
        with pytest.raises(ContextDenied):
            reply._codewords_validate()
    else:
        assert reply is None
    assert len(wire.captured) == (0 if when == "resolution" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_output", ["tool", "function", "media", "path"])
async def test_model_cannot_create_tool_or_media_path(runner, wire, bad_output):
    trigger = sealed(runner)
    if bad_output == "tool":
        wire.state.tool_calls = [{"id": "call", "type": "function", "function": {
            "name": "terminal", "arguments": '{"command":"cat private"}'}}]
        wire.state.finish = "tool_calls"
    elif bad_output == "function":
        wire.state.extra = {"function_call": {"name": "memory", "arguments": "{}"}}
    elif bad_output == "media":
        wire.state.text = "MEDIA:/private/secret"
    else:
        wire.state.text = "/private/secret"
    assert await runner._handle_message(trigger) is None
    assert len(wire.captured) == 1


@pytest.fixture
def native_text(runner):
    from types import SimpleNamespace
    from gateway.config import PlatformConfig
    from plugins.platforms.slack.adapter import SlackAdapter
    runner.config.client_context = copy.deepcopy(SETTING)
    runner.session_store = None
    runner._busy_text_mode = "queue"
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="synthetic", typing_indicator=False))
    runner._wire_adapter_handlers(adapter, authorization_check=lambda *args, **kwargs: False)
    runner._adapter_for_source = Mock(return_value=adapter)
    adapter._bot_user_id = "U0B2X1D8R17"
    client = AsyncMock()
    client.token = "synthetic-slack-credential"
    client.conversations_replies.return_value = {"ok": True, "messages": [], "has_more": False}
    client.users_info.return_value = {"ok": True, "user": {
        "id": "U092L0QKYMB", "team_id": "T052JJDUHCP", "is_bot": False,
    }}
    client.chat_postMessage.return_value = {"ok": True, "ts": "200.1"}
    client.reactions_add.return_value = {"ok": True}
    client.reactions_remove.return_value = {"ok": True}
    adapter._team_clients = {"T07MMEX5658": client}
    adapter._team_bot_user_ids = {"T07MMEX5658": "U0B2X1D8R17"}
    adapter._app = SimpleNamespace(client=client)
    adapter._start_typing_refresh = Mock(return_value=None)
    adapter.stop_typing = AsyncMock()
    adapter._hydrate_thread_context = AsyncMock(side_effect=forbidden)
    adapter._download_slack_file_bytes = AsyncMock(side_effect=forbidden)
    payload = {"type": "message", "channel": CHANNEL, "channel_type": "channel",
               "user": "U092L0QKYMB", "ts": "100.1", "thread_ts": "100.0",
               "text": "<@U0B2X1D8R17> help with this current request"}
    return adapter, client, payload


async def drain_native(adapter):
    import asyncio
    tasks = list(adapter._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["human", "admin", "wrong-team", "missing-team", "unbound-team", "bot", "bot-user", "auth-failure", "missing-user", "attachment", "files", "slash", "bang", "unmentioned", "malformed"])
async def test_native_admission_to_provider_and_plain_text_delivery(native_text, runner, wire, variant):
    adapter, client, payload = native_text
    body = {"team_id": "T07MMEX5658"}
    if variant == "admin":
        payload["user"] = "U07MJJMMK9T"
        client.users_info.return_value["user"].update(id="U07MJJMMK9T", team_id="T07MMEX5658", is_admin=True)
        adapter.set_authorization_check(lambda *args, **kwargs: True)
    elif variant == "wrong-team":
        body["team_id"] = "Tforeign"
    elif variant == "missing-team":
        body = {}
    elif variant == "unbound-team":
        adapter._team_clients = {}
    elif variant == "bot":
        payload["bot_id"] = "Bforeign"
    elif variant == "bot-user":
        client.users_info.return_value["user"]["is_bot"] = True
    elif variant == "auth-failure":
        client.users_info.side_effect = RuntimeError("PRIVATE_AUTH_SECRET")
    elif variant == "missing-user":
        payload.pop("user")
    elif variant == "attachment":
        payload["attachments"] = [{"text": "PRIVATE_ATTACHMENT"}]
    elif variant == "files":
        payload["files"] = [{"url_private": "https://private.invalid"}]
    elif variant in {"slash", "bang"}:
        payload["text"] = "<@U0B2X1D8R17> " + ("/approve" if variant == "slash" else "!reset")
    elif variant == "unmentioned":
        payload["text"] = "help with this current request"
    elif variant == "malformed":
        runner.config.client_context["codewords_read_channels"][CHANNEL]["sources"] = ["private"]
    await adapter._handle_slack_message(payload, body)
    await drain_native(adapter)
    permitted = variant in {"human", "admin"}
    assert len(wire.captured) == int(permitted)
    assert client.chat_postMessage.await_count == int(permitted)
    assert client.reactions_add.await_count == 2 * int(permitted)
    assert client.reactions_remove.await_count == int(permitted)
    adapter._hydrate_thread_context.assert_not_awaited()
    adapter._download_slack_file_bytes.assert_not_awaited()
    if permitted:
        request = wire.captured[0]
        assert json.loads(request["messages"][1]["content"])["current_request"] == "help with this current request"
        sent = client.chat_postMessage.call_args.kwargs
        assert sent["text"] == wire.state.text
        assert sent["mrkdwn"] is True and sent["parse"] == "none" and sent["link_names"] is False
        assert sent["unfurl_links"] is False and sent["unfurl_media"] is False
        assert "blocks" not in sent and "attachments" not in sent


@pytest.mark.asyncio
async def test_native_admission_failure_is_logged_without_provider_secrets(native_text, caplog):
    adapter, client, payload = native_text
    client.users_info.side_effect = RuntimeError("PRIVATE_AUTH_SECRET")
    with caplog.at_level("WARNING"):
        await adapter._handle_slack_message(payload, {"team_id": "T07MMEX5658"})
        await drain_native(adapter)
    assert client.chat_postMessage.await_count == 0
    assert "PRIVATE_AUTH_SECRET" not in caplog.text
    assert "admission denied" in caplog.text
    assert "RuntimeError" in caplog.text
    assert CHANNEL in caplog.text
    assert payload["ts"] in caplog.text


@pytest.mark.asyncio
async def test_native_admission_retries_transient_users_info_failure(native_text, wire):
    adapter, client, payload = native_text
    member = client.users_info.return_value
    client.users_info.side_effect = [RuntimeError("transient"), member]
    await adapter._handle_slack_message(payload, {"team_id": "T07MMEX5658"})
    await drain_native(adapter)
    assert client.users_info.await_count == 2
    assert len(wire.captured) == 1
    assert client.chat_postMessage.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/stop", "/reset", "a second question"])
async def test_busy_isolated_request_never_cancels_or_merges(runner, text):
    import asyncio
    from gateway.client_context_codewords import seal_event, selected
    from tests.gateway.test_client_context import TextAdapter
    adapter = TextAdapter()
    adapter._client_context_codewords = lambda source: selected(runner.config, source)
    trigger = sealed(runner)
    trigger.text = text
    trigger._codewords_request = seal_event(trigger)
    key = adapter._event_session_key(trigger)
    sentinel = asyncio.Event()
    adapter._active_sessions[key] = sentinel
    adapter._dispatch_active_session_command = AsyncMock(side_effect=forbidden)
    adapter._message_handler = AsyncMock(side_effect=forbidden)
    await adapter.handle_message(trigger)
    assert not sentinel.is_set()
    assert not adapter._pending_messages
    adapter._message_handler.assert_not_awaited()


@pytest.mark.parametrize("chat,kind", [(CHANNEL, "group"), ("Cother", "group"), ("Downer", "dm")])
@pytest.mark.parametrize("user,expected", [("U092L0QKYMB", False), ("U07MJJMMK9T", True)])
def test_channel_grant_never_changes_global_authorization(monkeypatch, chat, kind, user, expected):
    from gateway.config import GatewayConfig
    from tests.gateway.test_slack_bot_auth_bypass import _make_bare_runner
    for name in ("SLACK_ALLOW_ALL_USERS", "SLACK_ALLOW_BOTS", "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U07MJJMMK9T")
    normal = _make_bare_runner()
    normal.config = GatewayConfig()
    source = event(chat_id=chat, chat_type=kind, user_id=user, scope_id="T07MMEX5658").source
    assert normal._is_user_authorized_for_source(source) is expected
    normal.config.client_context = copy.deepcopy(SETTING)
    assert normal._is_user_authorized_for_source(source) is expected


@pytest.mark.asyncio
async def test_revoked_native_source_cannot_be_reused_for_background_or_hooks(runner, wire):
    trigger = sealed(runner)
    runner.config.client_context = {"enabled": False}
    del trigger._codewords_request
    trigger.internal = True
    assert await cc.admission(runner, trigger.source) == "deny"
    await runner._handle_message(trigger)
    await runner._handle_gateway_platform_event({"kind": "late-event"}, trigger.source)
    assert wire.captured == []
    runner._is_user_authorized_for_source.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,team,permitted", [
    ("app_mention", "T07MMEX5658", True),
    ("app_mention", "Tforeign", False),
    ("message", "T07MMEX5658", False),
    (None, "T07MMEX5658", False),
])
async def test_native_app_mention_without_channel_type(native_text, wire, kind, team, permitted):
    adapter, client, payload = native_text
    payload.pop("channel_type")
    payload["type"] = kind
    await adapter._handle_slack_message(payload, {"team_id": team})
    await drain_native(adapter)
    assert len(wire.captured) == int(permitted)
    assert client.chat_postMessage.await_count == int(permitted)
    adapter._hydrate_thread_context.assert_not_awaited()
    adapter._download_slack_file_bytes.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["old", "mixed", "wrong-workspace"])
async def test_retired_or_misbound_config_never_activates(runner, wire, setting):
    trigger = sealed(runner)
    cfg = runner.config.client_context
    if setting == "old":
        cfg["text_only_channels"] = cfg.pop("codewords_read_channels")
    elif setting == "mixed":
        cfg["text_only_channels"] = copy.deepcopy(cfg["codewords_read_channels"])
    else:
        cfg["codewords_read_channels"][CHANNEL]["scope_id"] = "Tforeign"
        trigger.source.scope_id = "Tforeign"
    assert await cc.admission(runner, trigger.source) == "deny"
    await runner._handle_message(trigger)
    assert wire.captured == []
    runner._is_user_authorized_for_source.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["_text_only_native_identity", "_text_only_policy", "_text_only_request"])
async def test_retired_receipts_cannot_reach_agent_or_controls(runner, wire, marker):
    from tests.gateway.test_client_context import TextAdapter

    runner.config.client_context = {"enabled": False}
    trigger = event(chat_id=CHANNEL, scope_id="T07MMEX5658", user_id="U07MJJMMK9T")
    trigger.text = "/stop"
    setattr(trigger if marker.endswith("request") else trigger.source, marker, ("retired",))
    await runner._handle_message(trigger)
    assert wire.captured == []
    runner._is_user_authorized_for_source.assert_not_called()
    adapter = TextAdapter()
    adapter._message_handler = AsyncMock(side_effect=forbidden)
    await adapter.handle_message(trigger)
    assert not adapter._active_sessions
    adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_source_without_request_seal_cannot_cancel_at_base_adapter(runner):
    import asyncio
    from gateway.client_context_codewords import selected
    from tests.gateway.test_client_context import TextAdapter

    trigger = sealed(runner)
    trigger.text = "/stop"
    del trigger._codewords_request
    runner.config.client_context = {"enabled": False}
    adapter = TextAdapter()
    adapter._client_context_codewords = lambda source: selected(runner.config, source)
    adapter._message_handler = AsyncMock(side_effect=forbidden)
    adapter._dispatch_active_session_command = AsyncMock(side_effect=forbidden)
    key = adapter._event_session_key(trigger)
    active = asyncio.Event()
    adapter._active_sessions[key] = active
    await adapter.handle_message(trigger)
    assert not active.is_set()
    assert not adapter._pending_messages
    adapter._dispatch_active_session_command.assert_not_awaited()
    adapter._message_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat,kind", [("Dnew", "im"), ("Cforeign", "channel")])
async def test_native_teammate_has_no_new_dm_or_foreign_channel_grant(native_text, wire, chat, kind):
    adapter, client, payload = native_text
    payload.update(channel=chat, channel_type=kind)
    await adapter._handle_slack_message(payload, {"team_id": "T07MMEX5658"})
    await drain_native(adapter)
    assert wire.captured == []
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["relay", "profile", "multiplex", "rejected-profile"])
async def test_native_route_rejects_other_runtime_authorities(runner, wire, change):
    trigger = sealed(runner)
    if change == "relay":
        trigger.source.delivered_via_upstream_relay = True
    elif change == "profile":
        trigger.source.profile = "other"
    elif change == "multiplex":
        runner.config.multiplex_profiles = True
    else:
        trigger.source.profile_route_rejected = True
    await runner._handle_message(trigger)
    assert wire.captured == []
