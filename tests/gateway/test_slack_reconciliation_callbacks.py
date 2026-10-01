"""Completed requests retain revocable ownership of running native confirmations."""
import asyncio
from contextvars import copy_context
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from plugins.platforms.slack.adapter import SlackAdapter
from tools import slash_confirm


@pytest.mark.asyncio
@pytest.mark.parametrize("withdrawal_ts,newer_requester", [
    (None, "UOTHER"),
    ("122.000001", "UOTHER"),
    ("125.000001", "UHUMAN"),
    ("130.000001", "UHUMAN"),
    ("120.000001", "UHUMAN"),
])
async def test_reload_confirmation_after_request_completion_is_revocable(withdrawal_ts, newer_requester):
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._app = MagicMock()
    adapter._bot_user_id = "UBOT"
    adapter._team_bot_user_ids = {"T1": "UBOT"}
    adapter._authorization_check = lambda *args, **kwargs: True
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "200.000001"}
    client.chat_update.return_value = {"ok": True}
    client.reactions_add.return_value = {"ok": True}
    client.reactions_remove.return_value = {"ok": True}
    adapter._get_client = MagicMock(return_value=client)
    adapter._resolve_user_is_bot = AsyncMock(return_value=False)
    adapter._resolve_user_name = AsyncMock(return_value="Human")
    adapter._resolve_channel_name = AsyncMock(return_value="Channel")
    adapter._hydrate_thread_context = AsyncMock(return_value=(None, [], []))
    adapter._start_typing_refresh = MagicMock(return_value=None)
    adapter.stop_typing = AsyncMock()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {Platform.SLACK: adapter}
    runner._read_user_config = lambda: {}
    entered, release = asyncio.Event(), asyncio.Event()
    independent_entered, independent_release = asyncio.Event(), asyncio.Event()
    cancelled = asyncio.Event()
    callback_context = []
    completed = []
    sessions = []
    metadata = {"team_id": "T1", "thread_id": "100.000001"}

    async def reload_mcp(event):
        callback_context.append(copy_context())
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        completed.append(event.source.user_id)
        return "Offline MCP reload completed"

    runner._execute_mcp_reload = reload_mcp

    async def handler(event):
        if event.message_id == "130.000001":
            independent_entered.set()
            await independent_release.wait()
            return "Independent work completed"
        sessions.append(adapter._event_session_key(event))
        return await runner._handle_reload_mcp_command(event)

    async def inbound(text, user="UHUMAN", ts="120.000001"):
        await adapter._handle_slack_message({
            "type": "message", "channel": "C1", "team": "T1", "user": user,
            "client_msg_id": ts, "ts": ts, "thread_ts": "100.000001", "text": text,
        })

    adapter.set_message_handler(handler)
    callback = independent = None
    try:
        await inbound("<@UBOT> /reload-mcp")
        await asyncio.gather(*list(adapter._background_tasks))
        assert len(sessions) == 1
        session = sessions[0]
        original = adapter._slack_prompt_participation[("T1", "C1", "200.000001")][0]
        assert not original.live
        assert not adapter._participations()
        pending = slash_confirm.get_pending(session)
        assert pending and pending["command"] == "reload-mcp"
        callback = asyncio.create_task(adapter._handle_slash_confirm_action(
            AsyncMock(),
            {"team": {"id": "T1"}, "channel": {"id": "C1"},
             "user": {"id": "UHUMAN", "name": "Human"},
             "message": {"ts": "200.000001", "thread_ts": "100.000001"}},
            {"action_id": "hermes_confirm_once", "value": f"{session}|{pending['confirm_id']}"},
        ))
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert slash_confirm.get_pending(session) is None
        await inbound("<@UBOT> independent task", user=newer_requester, ts="130.000001")
        await asyncio.wait_for(independent_entered.wait(), timeout=2)
        independent = adapter._session_tasks[session]
        newer = adapter._requester_task(original.key, newer_requester)
        assert newer is not None and newer.task is independent
        client.reset_mock()
        if withdrawal_ts is not None:
            await inbound("no reply needed", ts=withdrawal_ts)
            if withdrawal_ts in (original.request_ts, newer.request_ts):
                # Equal request timestamps are deduplicated at ingress; exercise
                # their reconciliation eligibility under the same running tasks.
                await adapter._revoke_requester_for_no_reply({
                    "user": "UHUMAN", "ts": withdrawal_ts,
                    "thread_ts": "100.000001", "text": "no reply needed",
                }, "T1", "C1")
            assert original.revoked is (withdrawal_ts > original.request_ts)
            stop_key = (*original.key, "UHUMAN")
            assert adapter._slack_requester_stops[stop_key] == withdrawal_ts
            # Exercise reconciliation replay itself, below Socket Mode dedup.
            for replay_ts in (withdrawal_ts, "119.000001"):
                await adapter._revoke_requester_for_no_reply({
                    "user": "UHUMAN", "ts": replay_ts,
                    "thread_ts": "100.000001", "text": "no reply needed",
                }, "T1", "C1")
                assert adapter._slack_requester_stops[stop_key] == withdrawal_ts
            assert not newer.revoked
            assert adapter._participation_valid(newer)
            assert adapter._session_tasks[session] is independent
            assert not independent.done()
        if withdrawal_ts is not None and withdrawal_ts > original.request_ts:
            assert original.revoked
            await asyncio.wait_for(cancelled.wait(), timeout=2)
            await asyncio.gather(callback, return_exceptions=True)
            assert callback.cancelled()
            assert not completed
            assert not independent.done()
            assert adapter._session_tasks[session] is independent
            late = await callback_context[0].run(asyncio.create_task, adapter.send(
                "C1", "late callback output", metadata=metadata))
            assert not late.success
            assert late.error == "slack_participation_revoked"
            client.chat_postMessage.assert_not_awaited()
        else:
            release.set()
            await asyncio.wait_for(callback, timeout=2)
            assert completed == ["UHUMAN"]
            assert not cancelled.is_set()
            assert not original.revoked
            assert not independent.done()
            assert any(call.kwargs.get("text") == "Offline MCP reload completed"
                       for call in client.chat_postMessage.await_args_list)
        independent_release.set()
        await asyncio.wait_for(independent, timeout=2)
        assert any(call.kwargs.get("text") == "Independent work completed"
                   for call in client.chat_postMessage.await_args_list)
    finally:
        release.set()
        independent_release.set()
        if callback is not None and not callback.done():
            callback.cancel()
        if callback is not None:
            await asyncio.gather(callback, return_exceptions=True)
        await adapter.cancel_background_tasks()
        for session in sessions:
            slash_confirm.clear(session)
