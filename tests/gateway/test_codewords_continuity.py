"""Follow-up evidence crosses the real isolated provider loop, never private sessions."""

import asyncio
import json
import threading
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import client_context_codewords as route
from tests.gateway.test_client_context import corpus, runner, wire  # noqa: F401
from tests.gateway.test_codewords_stripe import page, stripe, subscription  # noqa: F401
from tests.gateway.test_slack_channel_isolation import sealed, native_text, drain_native  # noqa: F401


@pytest.fixture
def slack(runner):
    client = SimpleNamespace(
        token="synthetic-slack-credential",
        conversations_replies=AsyncMock(return_value={"ok": True, "messages": [
            {"type": "message", "ts": "1789084800.000001", "user": "Uhuman",
             "text": "Use 2026-09-07 through 2026-09-13 versus 2026-08-31 through 2026-09-06."},
            {"type": "message", "ts": "1789084801.000001", "thread_ts": "1789084800.000001",
             "user": "Ujarvis", "bot_id": "Bjarvis", "text": "These are provisional week-over-week windows; rollout is unknown."},
        ], "has_more": False}),
        conversations_history=AsyncMock(return_value={"ok": True, "messages": [
            {"type": "message", "ts": "1789171200.000001", "user": "Uhuman",
             "text": "The payment fix went live on September 12. This is a rollout report."},
        ], "has_more": False}),
    )
    adapter = runner._adapter_for_source.return_value
    adapter._team_clients = {route.WORKSPACE: client}
    adapter._team_bot_user_ids = {route.WORKSPACE: "Ujarvis"}
    adapter.authorize_turn = Mock(return_value=True)
    adapter._codewords_history_read_scope = lambda *args: nullcontext()
    return client


def question(runner, text):
    trigger = sealed(runner)
    trigger.source.thread_id = "1789084800.000001"
    trigger.message_id = "1789430400.000001"
    trigger.text = text
    trigger.source._codewords_native_identity = route.native_identity(trigger.source)
    trigger._codewords_request = route.seal_event(trigger)
    return trigger


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "what is churn versus fixes last week",
    "show total voluntary involuntary and deltas pre/post",
    "find it from this channel",
])
async def test_followups_recover_windows_from_fresh_native_thread(runner, wire, slack, stripe, text):
    _, stripe_calls, install = stripe
    rows = []
    for day, reasons in ((1, ["cancellation_requested", "payment_failed"]),
                         (8, ["cancellation_requested", "cancellation_requested", "payment_failed", None])):
        ended = int(datetime(2026, 9, day, tzinfo=timezone.utc).timestamp())
        for reason in reasons:
            rows.append(subscription(f"sub_Test{len(rows)}", status="canceled", ended_at=ended,
                                     cancellation_details={"reason": reason}))
    install(lambda request: page(rows))
    runner._client_context_history = {"private": "PRIVATE_STORED_SESSION"}
    trigger = question(runner, text)
    final = "I will use the established windows."

    def response(body):
        if len(wire.captured) == 1 and text != "find it from this channel":
            wire.state.text = None
            wire.state.finish = "tool_calls"
            wire.state.tool_calls = [{"id": "stripe1", "type": "function", "function": {
                "name": "codewords_stripe_read", "arguments": json.dumps({
                    "start_date": "2026-09-07", "end_date": "2026-09-13",
                    "comparison_start_date": "2026-08-31", "comparison_end_date": "2026-09-06",
                }),
            }}]
        else:
            if text != "find it from this channel":
                output = next(row["content"] for row in body["messages"] if row["role"] == "tool")
                data = json.loads(output)["result"]
                assert data["metrics"]["cancellation_endings"] == {
                    "total": 4, "voluntary": 2, "involuntary": 1, "other_or_unknown": 1}
                assert data["comparison"]["delta"] == {
                    "total": 2, "voluntary": 1, "involuntary": 0, "other_or_unknown": 1}
                assert data["comparison"]["percent_change"]["total"] == 100
                assert "Canary" not in output
            wire.state.text = final
            wire.state.finish = "stop"
            wire.state.tool_calls = []

    wire.state.callback = response
    reply = await runner._handle_message(trigger)
    assert reply == final
    assert len(stripe_calls) == int(text != "find it from this channel")
    request = wire.captured[0]
    assert [row["role"] for row in request["messages"]] == ["system", "user"]
    content = request["messages"][1]["content"]
    assert text in content
    assert "2026-09-07 through 2026-09-13" in content
    assert "provisional week-over-week windows" in content
    assert "PRIVATE_STORED_SESSION" not in json.dumps(request)
    assert "synthetic-slack-credential" not in json.dumps(request)
    assert {tool["function"]["name"] for tool in request["tools"]} >= {"codewords_slack_history_read"}
    slack.conversations_replies.assert_awaited()
    for call in slack.conversations_replies.await_args_list:
        assert call.kwargs["channel"] == route.CHANNEL
        assert call.kwargs["ts"] == trigger.source.thread_id
    runner.session_store.assert_not_called()
    runner._run_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", [False, True])
async def test_find_in_channel_uses_fixed_history_tool_and_fences_release(runner, wire, slack, revoke):
    trigger = question(runner, "find it from this channel")

    def response(body):
        if len(wire.captured) == 1:
            wire.state.text = None
            wire.state.finish = "tool_calls"
            wire.state.tool_calls = [{"id": "history1", "type": "function", "function": {
                "name": "codewords_slack_history_read", "arguments": json.dumps({
                    "operation": "channel", "start_date": "2026-09-07",
                    "end_date": "2026-09-13", "query": "fix",
                }),
            }}]
        else:
            result = next(row["content"] for row in body["messages"] if row["role"] == "tool")
            assert "payment fix went live" in result
            assert "1789171200.000001" in result
            wire.state.text = "The channel reports a September 12 rollout. Counts alone do not prove causation."
            wire.state.finish = "stop"
            wire.state.tool_calls = []
            if revoke:
                runner.config.client_context["codewords_read_channels"][route.CHANNEL]["generation"] = "revoked"

    wire.state.callback = response
    reply = await runner._handle_message(trigger)
    assert len(wire.captured) == 2
    assert (reply is None) is revoke
    if not revoke:
        reply._codewords_validate()
    slack.conversations_history.assert_awaited_once()
    assert slack.conversations_history.await_args.kwargs["channel"] == route.CHANNEL
    runner.session_store.assert_not_called()
    runner._run_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_stopped_turn_cannot_continue_a_pending_native_history_read(runner, wire, slack, monkeypatch, stop):
    entered, release = asyncio.Event(), asyncio.Event()
    finished = threading.Event()
    original = route._completion_loop

    def completion(*args):
        try:
            return original(*args)
        finally:
            finished.set()

    async def response(**kwargs):
        entered.set()
        await release.wait()
        return {"ok": True, "messages": [], "has_more": True,
                "response_metadata": {"next_cursor": "should-never-be-read"}}

    slack.conversations_replies.side_effect = response
    monkeypatch.setattr(route, "_completion_loop", completion)
    if stop == "timeout":
        monkeypatch.setattr(route, "TURN_TIMEOUT", 2)
    task = asyncio.create_task(runner._handle_message(question(runner, "find it from this channel")))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is None
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 10)
    slack.conversations_replies.assert_awaited_once()
    assert not wire.captured
    runner._pop_post_delivery_callback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [None, "channel", "thread", "method", "child"])
async def test_history_crosses_real_fenced_sdk_with_exact_read_authority(native_text, wire, monkeypatch, tamper):
    pytest.importorskip("slack_sdk.web.async_base_client")
    adapter, _, payload = native_text
    payload.update(ts="1789430400.000001", thread_ts="1789084800.000001")
    methods = []

    async def transport(*, api_url, **kwargs):
        method = api_url.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "users.info":
            data = {"ok": True, "user": {"id": payload["user"], "is_bot": False}}
        elif method == "conversations.replies":
            data = {"ok": True, "has_more": False, "messages": [{
                "type": "message", "ts": payload["thread_ts"], "user": payload["user"],
                "text": "Established windows from real fenced SDK transport.",
            }]}
        elif method == "conversations.history":
            data = {"ok": True, "has_more": False, "messages": [{
                "type": "message", "ts": "1789171200.000001", "user": payload["user"],
                "text": "Actual fix rollout reported September 12.",
            }]}
        elif method in {"reactions.add", "reactions.remove"}:
            data = {"ok": True}
        else:
            assert method == "chat.postMessage"
            data = {"ok": True, "ts": "1789430401.000001"}
        return {"status_code": 200, "headers": {}, "data": data}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    client = adapter._new_web_client("synthetic", None)
    adapter._team_clients = {route.WORKSPACE: client}
    adapter._app.client = client
    original = client.conversations_replies

    async def read(**kwargs):
        if tamper == "channel":
            kwargs["channel"] = "CFOREIGN"
        elif tamper == "thread":
            kwargs["ts"] = "1789084700.000001"
        elif tamper == "method":
            return await client.conversations_info(channel=route.CHANNEL)
        elif tamper == "child":
            return await asyncio.create_task(original(**kwargs))
        return await original(**kwargs)

    monkeypatch.setattr(client, "conversations_replies", read)

    def response(body):
        if tamper:
            assert json.loads(body["messages"][1]["content"])["untrusted_same_thread_context"]["status"] == "unavailable"
            wire.state.text = "History unavailable."
            return
        if len(wire.captured) == 1:
            assert "Established windows from real fenced SDK transport." in body["messages"][1]["content"]
            wire.state.text = None
            wire.state.finish = "tool_calls"
            wire.state.tool_calls = [{"id": "native-history", "type": "function", "function": {
                "name": "codewords_slack_history_read", "arguments": json.dumps({
                    "operation": "channel", "start_date": "2026-09-07", "end_date": "2026-09-13", "query": "fix"}),
            }}]
        else:
            assert "Actual fix rollout reported" in body["messages"][-1]["content"]
            wire.state.text = "The reported rollout was September 12; counts alone do not establish causality."
            wire.state.finish = "stop"
            wire.state.tool_calls = []

    wire.state.callback = response
    await adapter._handle_slack_message(payload, {"team_id": route.WORKSPACE})
    await drain_native(adapter)
    assert methods == ["users.info", "reactions.add"] + (
        [] if tamper else ["conversations.replies", "conversations.history"]
    ) + ["chat.postMessage", "reactions.remove", "reactions.add"]
    assert len(wire.captured) == (1 if tamper else 2)
