"""Per-route account capabilities through the real gateway/provider and native lane."""
import json
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import client_context as cc
from gateway import client_context_codewords as route
from gateway import client_context_codewords_chart as chart
from gateway import client_context_codewords_history as history
from gateway import client_context_codewords_reads as reads
from gateway.client_context_policy import ContextDenied
from tests.gateway.test_client_context import corpus, event, forbidden, runner, wire  # noqa: F401
from tests.gateway.test_codewords_tool_loop import call, respond, unavailable  # noqa: F401
from tests.gateway.test_slack_channel_isolation import (  # noqa: F401
    CHANNEL, drain_native, native_text, sealed,
)


def setting(runner):
    return runner.config.client_context["codewords_read_channels"][CHANNEL]


def gated(runner, names):
    trigger = sealed(runner)
    setting(runner)["read_tools"] = names
    trigger.source._codewords_policy = route.route(runner.config, trigger.source)
    trigger._codewords_request = route.seal_event(trigger)
    return trigger


def arguments(name):
    return {"operation": reads.OPERATIONS[name][0]} if name in reads.UNDATED else None


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_empty_gate_has_only_exact_history_and_no_private_context(runner, wire, monkeypatch, admin):
    trigger = gated(runner, [])
    trigger.source.user_id = "U07MJJMMK9T" if admin else "U092L0QKYMB"
    trigger.source._codewords_native_identity = route.native_identity(trigger.source)
    trigger._codewords_request = route.seal_event(trigger)
    runner._is_user_authorized_for_source.return_value = admin
    private = "PRIVATE_ACCOUNT_GATE_CANARY"
    runner._client_context_history = {"other-route": private}
    for attr in ("channel_prompt", "channel_context", "reply_to_text", "auto_skill", "raw_message", "metadata"):
        setattr(trigger, attr, private)
    monkeypatch.setattr(cc, "_prepare", forbidden)
    monkeypatch.setattr(reads, "execute", forbidden)
    assert await runner._handle_message(trigger) == wire.state.text
    assert trigger.source._codewords_policy.resources == ()
    assert wire.captured[0]["tools"] == [history.schema()]
    encoded = json.dumps(wire.captured)
    assert all(canary not in encoded for canary in (
        private, "CODEWORDS_A_SYNTHETIC", "CLIENT_B_SYNTHETIC"))
    assert runner._client_context_history == {"other-route": private}
    runner.session_store.assert_not_called()
    runner._is_user_authorized_for_source.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", reads.NAMES)
@pytest.mark.parametrize("mixed", [False, True])
async def test_each_disabled_reader_denies_whole_batch_before_execution(
        runner, wire, monkeypatch, name, mixed):
    trigger = gated(runner, [])
    execute = Mock(side_effect=forbidden)
    history_execute = Mock(side_effect=forbidden)
    monkeypatch.setattr(reads, "execute", execute)
    monkeypatch.setattr(history.HistoryReader, "execute", history_execute)
    calls = [call(name, arguments(name), "disabled")]
    if mixed:
        calls.insert(0, call(history.NAME, {"operation": "thread"}, "history"))
    respond(wire, calls)
    assert await runner._handle_message(trigger) is None
    execute.assert_not_called()
    history_execute.assert_not_called()
    assert len(wire.captured) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("listed", [False, True])
async def test_subset_exposes_and_admits_only_listed_reader(runner, wire, unavailable, monkeypatch, listed):
    trigger = gated(runner, ["codewords_gsc_read"])
    execute = Mock(wraps=reads.execute)
    monkeypatch.setattr(reads, "execute", execute)
    name = "codewords_gsc_read" if listed else "codewords_ga4_read"
    wire.state.callback = lambda body: respond(
        wire, [call(name)] if len(wire.captured) == 1 else [])
    reply = await runner._handle_message(trigger)
    assert wire.captured[0]["tools"] == [
        schema for schema in reads.schemas() if schema["function"]["name"] == "codewords_gsc_read"
    ] + [history.schema()]
    assert [binding.name for binding in trigger.source._codewords_policy.resources] == ["codewords_gsc_read"]
    assert execute.call_count == int(listed)
    assert (reply == "The requested integrations are unavailable.") if listed else reply is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    None, "codewords_gsc_read", {}, (), True, [None], [1], [[]],
    ["terminal"], [history.NAME], ["codewords_gsc_read", "codewords_gsc_read"],
    list(reads.NAMES) + ["codewords_gsc_read"],
])
async def test_invalid_gate_fails_closed_without_legacy_fallback(runner, wire, invalid):
    trigger = sealed(runner)
    setting(runner)["read_tools"] = invalid
    with pytest.raises(ContextDenied):
        route.route(runner.config, trigger.source)
    assert await runner._handle_message(trigger) == cc.DENIED
    assert wire.captured == []
    runner.session_store.assert_not_called()
    runner._is_user_authorized_for_source.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [[], list(reads.NAMES)])
@pytest.mark.parametrize("change", ["edit", "remove"])
@pytest.mark.parametrize("moment", ["admitted", "dispatch", "release"])
async def test_gate_mutations_revoke_admission_turn_and_release(runner, wire, names, change, moment):
    trigger = gated(runner, names)

    def revoke():
        if change == "remove":
            setting(runner).pop("read_tools")
        else:
            setting(runner)["read_tools"] = ["codewords_gsc_read"]

    if moment == "admitted":
        revoke()
    elif moment == "dispatch":
        wire.state.callback = lambda body: revoke()
    reply = await runner._handle_message(trigger)
    if moment == "release":
        assert isinstance(reply, cc.ScopedReply)
        revoke()
        with pytest.raises(ContextDenied):
            reply._codewords_validate()
    else:
        assert reply == cc.DENIED if moment == "admitted" else reply is None
        runner._pop_post_delivery_callback.assert_not_called()
    assert len(wire.captured) == (0 if moment == "admitted" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_native_empty_gate_keeps_safe_same_thread_text(native_text, runner, wire, admin):
    adapter, client, payload = native_text
    setting(runner)["read_tools"] = []
    if admin:
        payload["user"] = "U07MJJMMK9T"
        client.users_info.return_value["user"].update(id=payload["user"], is_admin=True)
        adapter.set_authorization_check(lambda *args, **kwargs: True)
    client.conversations_replies.return_value["messages"] = [
        {"type": "message", "user": payload["user"], "ts": "100.0", "text": "SAFE_THREAD_CONTEXT"},
        {"type": "message", "user": payload["user"], "ts": "100.0", "text": "FOREIGN_CONTEXT",
         "channel": "Cforeign"},
    ]
    await adapter._handle_slack_message(payload, {"team_id": route.WORKSPACE})
    await drain_native(adapter)
    assert len(wire.captured) == 1
    assert wire.captured[0]["tools"] == [history.schema()]
    encoded = json.dumps(wire.captured)
    assert "SAFE_THREAD_CONTEXT" in encoded and "FOREIGN_CONTEXT" not in encoded
    client.conversations_replies.assert_awaited_once_with(
        channel=CHANNEL, limit=history.PAGE_SIZE, latest="100.1", inclusive=True, ts="100.0")
    assert client.chat_postMessage.await_count == 1
    adapter._hydrate_thread_context.assert_not_awaited()
    adapter._download_slack_file_bytes.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["edit", "remove"])
async def test_native_gate_change_after_admission_denies_provider(native_text, runner, wire, change):
    adapter, client, payload = native_text
    setting(runner)["read_tools"] = []
    handler = adapter._message_handler

    async def revoked(event):
        assert event.source._codewords_policy.resources == ()
        if change == "remove":
            setting(runner).pop("read_tools")
        else:
            setting(runner)["read_tools"] = ["codewords_gsc_read"]
        return await handler(event)

    adapter._message_handler = revoked
    await adapter._handle_slack_message(payload, {"team_id": route.WORKSPACE})
    await drain_native(adapter)
    assert wire.captured == []
    client.conversations_replies.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["edit", "remove"])
async def test_native_sdk_gate_change_immediately_before_delivery(
        native_text, runner, wire, monkeypatch, change):
    pytest.importorskip("slack_sdk.web.async_base_client")
    adapter, _, payload = native_text
    setting(runner)["read_tools"] = []
    calls = []

    async def transport(*, api_url, **kwargs):
        calls.append(api_url.rsplit("/", 1)[-1])
        if api_url.endswith("users.info"):
            data = {"ok": True, "user": {"id": payload["user"], "is_bot": False}}
        elif api_url.endswith("conversations.replies"):
            data = {"ok": True, "messages": [], "has_more": False}
        else:
            data = {"ok": True, "ts": "200.1"}
        return {"status_code": 200, "headers": {}, "data": data}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    client = adapter._new_web_client("synthetic", None)
    adapter._team_clients = {route.WORKSPACE: client}
    adapter._app.client = client
    post = client.chat_postMessage

    async def revoked(**kwargs):
        if change == "remove":
            setting(runner).pop("read_tools")
        else:
            setting(runner)["read_tools"] = ["codewords_gsc_read"]
        return await post(**kwargs)

    monkeypatch.setattr(client, "chat_postMessage", revoked)
    await adapter._handle_slack_message(payload, {"team_id": route.WORKSPACE})
    await drain_native(adapter)
    assert len(wire.captured) == 1
    assert wire.captured[0]["tools"] == [history.schema()]
    assert calls == ["users.info", "reactions.add", "conversations.replies", "reactions.remove"]


@pytest.mark.asyncio
async def test_stripe_off_chart_is_unavailable_without_render_or_upload(runner, wire, monkeypatch):
    trigger = gated(runner, [])
    trigger.text = "show a chart of the Stripe ending comparison"
    assert chart.requested(trigger.text)
    trigger._codewords_request = route.seal_event(trigger)
    monkeypatch.setattr(chart, "render", forbidden)
    monkeypatch.setattr(reads, "execute", forbidden)
    reply = await runner._handle_message(trigger)
    assert reply == chart.unavailable_summary()
    assert reply._codewords_chart_unavailable is True
    assert not hasattr(reply, "_codewords_chart")
    assert wire.captured[0]["tools"] == [history.schema()]


def test_codewords_history_channels_require_matching_approved_client_route(runner):
    from gateway import client_context_codewords as cw

    route_spec = {"scope_id": "T07MMEX5658", "provider": "openai", "model": "synthetic-main",
                  "generation": "v1"}
    runner.config.client_context = {"enabled": False, "codewords_read_channels": {
        CHANNEL: dict(route_spec), "C0BUYTE6D0T": dict(route_spec),
    }}
    source = event(chat_id="C0BUYTE6D0T", scope_id="T07MMEX5658", user_id="U07MJJMMK9T").source
    policy = cw.route(runner.config, source)
    assert cw.selected(runner.config, source)
    assert cw.approved_history_channels(runner.config, source, policy) == (CHANNEL, "C0BUYTE6D0T")
    source.chat_id = "CUNAPPROVED"
    with pytest.raises(ContextDenied):
        cw.route(runner.config, source)


@pytest.mark.asyncio
async def test_aditya_owner_dm_keeps_legacy_behavior_without_client_history(runner):
    runner.config.client_context = {"enabled": False, "codewords_read_channels": {CHANNEL: {
        "scope_id": "T07MMEX5658", "provider": "openai", "model": "synthetic-main", "generation": "v1",
    }}}
    runner._hm_admit_event = AsyncMock(return_value=None)
    trigger = event(chat_id="D0B2DLPUSB1", scope_id=None, user_id="U07MJJMMK9T", chat_type="dm")
    assert await cc.admission(runner, trigger.source) == "legacy"
    assert await runner._handle_message(trigger) is None
    runner._hm_admit_event.assert_awaited_once_with(trigger)
