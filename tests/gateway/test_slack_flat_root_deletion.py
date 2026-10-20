"""Deleting a flat-channel bot post stops the request that wrote it (EZ-841)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter
from tools.interrupt import tool_execution_allowed


def flat_adapter():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake", extra={"reply_in_thread": False}))
    adapter._app = MagicMock()
    adapter._bot_user_id = "UBOT"
    adapter._team_bot_user_ids = {"T1": "UBOT"}
    adapter._authorization_check = lambda *args, **kwargs: True
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "200.000001"}
    client.chat_update.return_value = {"ok": True}
    adapter._get_client = MagicMock(return_value=client)
    adapter._resolve_user_is_bot = AsyncMock(return_value=False)
    adapter._resolve_user_name = AsyncMock(return_value="Human")
    adapter._resolve_channel_name = AsyncMock(return_value="Channel")
    adapter._hydrate_thread_context = AsyncMock(return_value=(None, [], []))
    adapter._start_typing_refresh = MagicMock(return_value=None)
    adapter._run_processing_hook = AsyncMock()
    adapter.stop_typing = AsyncMock()
    adapter.set_session_cancellation_handler(AsyncMock())
    return adapter, client


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted, stops", [("200.000001", True), ("300.000001", False)])
async def test_flat_root_status_deletion_revokes_originating_request(deleted, stops):
    adapter, client = flat_adapter()
    posted, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    after = {}

    async def generate(event):
        try:
            assert (await adapter.send("C1", "working", metadata={"team_id": "T1"})).success
            posted.set()
            await release.wait()
            after["tools"] = tool_execution_allowed()
            after["send"] = (await adapter.send("C1", "late", metadata={"team_id": "T1"})).success
        finally:
            finished.set()

    adapter.set_message_handler(generate)
    await adapter._handle_slack_message({
        "type": "message", "channel": "C1", "team": "T1", "user": "UHUMAN",
        "client_msg_id": "150.000001", "ts": "150.000001", "text": "<@UBOT> begin"})
    await asyncio.wait_for(posted.wait(), timeout=2)
    session_key, owner = next(iter(adapter._session_tasks.items()))
    await adapter._handle_slack_message({
        "type": "message", "subtype": "message_deleted", "channel": "C1", "team": "T1",
        "deleted_ts": deleted, "event_ts": "210.000001",
        "previous_message": {"user": "UBOT", "ts": deleted}})
    callback = adapter._session_cancellation_handler
    if stops:
        callback.assert_awaited_once()
        assert callback.await_args.args[0] == session_key
    else:
        callback.assert_not_awaited()
    if not owner.done():
        release.set()
    await asyncio.wait_for(finished.wait(), timeout=2)
    if after:
        assert after == {"tools": not stops, "send": not stops}
