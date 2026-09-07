"""Deletion is an exact-surface stop, not an invitation to recreate a reply."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


META = {"team_id": "T1", "thread_id": "100.000001"}


def make_adapter():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake", extra={"require_mention": False}))
    adapter._app = MagicMock()
    adapter._bot_user_id = "UBOT"
    adapter._team_bot_user_ids = {"T1": "UBOT", "T2": "UOTHERBOT"}
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "101.000001"}
    client.chat_update.return_value = {"ok": True}
    client.chat_delete.return_value = {"ok": True}
    client.chat_startStream.return_value = {"ok": True, "ts": "102.000001"}
    client.api_call.return_value = {"ok": True, "ts": "103.000001"}
    setattr(adapter, "_get_client", MagicMock(return_value=client))
    setattr(adapter, "stop_typing", AsyncMock())
    setattr(adapter, "_resolve_user_is_bot", AsyncMock(return_value=False))
    setattr(adapter, "_resolve_user_name", AsyncMock(return_value="Human"))
    setattr(adapter, "_resolve_channel_name", AsyncMock(return_value="Channel"))
    setattr(adapter, "_hydrate_thread_context", AsyncMock(return_value=(None, [], [])))
    adapter._authorization_check = lambda *args, **kwargs: True
    adapter.handle_message = AsyncMock()
    return adapter, client


def deletion(*, user="UBOT", channel="C1", team="T1", ts="101.000001", thread="100.000001"):
    return {"type": "message", "subtype": "message_deleted", "channel": channel,
            "team": team, "deleted_ts": ts, "event_ts": "110.000001",
            "previous_message": {"user": user, "ts": ts, "thread_ts": thread}}


def mention(text, ts="120.000001", **extra):
    return {"type": "message", "channel": "C1", "team": "T1", "user": "UHUMAN",
            "client_msg_id": ts, "ts": ts, "thread_ts": META["thread_id"],
            "text": text, **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, muted", [
    ("external", True), ("restart", True), ("restart_before_delete", True), ("active_turn", True), ("tracked_without_author", True),
    ("human_root", False), ("other_bot", False), ("other_workspace", False),
    ("cleanup_race", False), ("failed_cleanup", True),
])
async def test_deleted_reply_blocks_all_recreation_until_fresh_authorized_mention(kind, muted):
    adapter, client = make_adapter()
    assert (await adapter.send_or_update_status("C1", "progress", "working", metadata=META)).success
    event = deletion()
    if kind == "restart_before_delete":
        adapter, client = make_adapter()
        event.pop("previous_message")
    if kind == "active_turn":
        del adapter.handle_message  # use real dispatch and background processing
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def generate(message):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        adapter.set_message_handler(generate)
        adapter._run_processing_hook = AsyncMock()
        adapter._start_typing_refresh = MagicMock(return_value=None)
        adapter.set_session_cancellation_handler(AsyncMock())
        await adapter._handle_slack_message(mention("<@UBOT> begin", ts="105.000001"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert len(adapter._session_tasks) == 1
        session_key, owner = next(iter(adapter._session_tasks.items()))
        # Queue clearing must precede the owner's finalizer and its pending-message drain.
        adapter._pending_messages[session_key] = MagicMock()
    if kind == "human_root":
        event = deletion(user="UHUMAN", ts=META["thread_id"])
    elif kind == "other_bot":
        event = deletion(user="UOTHERBOT", ts="104.000001")
    elif kind == "other_workspace":
        event = deletion(team="T2")
    elif kind == "tracked_without_author":
        event.pop("previous_message")
    elif kind == "cleanup_race":
        async def cleanup(**kwargs):
            await adapter._handle_slack_message(event)
            return {"ok": True}
        client.chat_delete.side_effect = cleanup
        assert await adapter.delete_message("C1", "101.000001")
    elif kind == "failed_cleanup":
        client.chat_delete.return_value = {"ok": False, "error": "cant_delete_message"}
        assert not await adapter.delete_message("C1", "101.000001")
    await adapter._handle_slack_message(event)
    if kind == "active_turn":
        assert cancelled.is_set() and owner.cancelled()
        assert not adapter._pending_messages and not adapter._active_sessions
        callback = adapter._session_cancellation_handler
        callback.assert_awaited_once()
        called_key, source = callback.await_args.args
        assert called_key == session_key
        assert (source.scope_id, source.chat_id, source.thread_id) == ("T1", "C1", META["thread_id"])
        adapter.handle_message = AsyncMock()
    if kind == "restart":
        adapter, client = make_adapter()
    client.reset_mock()
    if not muted:
        assert (await adapter.send("C1", "unaffected", metadata=META)).success
        return
    results = [
        await adapter.send_or_update_status("C1", "progress", "still working", metadata=META),
        await adapter.edit_message("C1", "101.000001", "final", finalize=True, metadata=META),
        await adapter.send("C1", "fallback", metadata=META),
        await adapter.send_draft("C1", 1, "stream", metadata=META),
        await adapter.send_native_task_card_progress("C1", [{"id": "a"}], metadata=META),
        await adapter.send_exec_approval("C1", "echo hi", "session", metadata=META),
    ]
    assert all(not result.success for result in results)
    assert not client.mock_calls
    # Matching timestamps in another channel/workspace/thread must remain writable.
    for channel, metadata in [("C2", META), ("C1", {**META, "team_id": "T2"}),
                              ("C1", {**META, "thread_id": "200.000001"})]:
        assert (await adapter.send(channel, "unaffected", metadata=metadata)).success
    for event in [mention("ambient"), mention("<@UBOT> stale", ts="105.000001"),
                  mention("ambient", ts="121.000001", _hermes_force_process=True),
                  mention("> <@UBOT> quoted", ts="121.000002"),
                  mention("`<@UBOT>` code", ts="121.000003")]:
        await adapter._handle_slack_message(event)
    adapter._authorization_check = lambda *args, **kwargs: False
    await adapter._handle_slack_message(mention("<@UBOT> unauthorized", ts="122.000001"))
    adapter.handle_message.assert_not_awaited()
    assert not (await adapter.send("C1", "still muted", metadata=META)).success
    adapter._authorization_check = None
    await adapter._handle_slack_message(mention("<@UBOT> unknown auth", ts="122.000002"))
    adapter.handle_message.assert_not_awaited()
    adapter._authorization_check = lambda *args, **kwargs: True
    await adapter._handle_slack_message(mention("<@UBOT> resume", ts="123.000001"))
    adapter.handle_message.assert_awaited_once()
    assert (await adapter.send("C1", "fresh reply", metadata=META)).success
    await adapter._handle_slack_message(deletion())  # reconnect replay cannot revoke the summon
    assert (await adapter.send("C1", "still summoned", metadata=META)).success


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["status_return", "status_raise", "native_append", "native_seal",
                                       "late_post", "late_post_after_summon", "late_post_cancelled", "late_first_reply_deleted", "late_chunk"])
async def test_missing_or_inflight_writes_cannot_resurrect_a_deleted_surface(transport):
    adapter, client = make_adapter()
    await adapter.send_or_update_status("C1", "progress", "working", metadata=META)
    if transport.startswith("status"):
        if transport == "status_return":
            client.chat_update.return_value = {"ok": False, "error": "message_not_found"}
        else:
            error = RuntimeError("Slack update failed")
            setattr(error, "response", {"ok": False, "error": "message_not_found"})
            client.chat_update.side_effect = error
        result = await adapter.send_or_update_status("C1", "progress", "resurrect", metadata=META)
    elif transport.startswith("native"):
        await adapter.send_draft("C1", 1, "hello", metadata=META)
        if transport == "native_append":
            client.chat_appendStream.return_value = {"ok": False, "error": "message_not_found"}
            result = await adapter.send_draft("C1", 1, "hello again", metadata=META)
        else:
            client.chat_stopStream.return_value = {"ok": False, "error": "message_not_found"}
            result = await adapter.send("C1", "hello final", metadata=META)
    else:
        entered, release = asyncio.Event(), asyncio.Event()
        async def late_post(**kwargs):
            entered.set()
            await release.wait()
            return {"ok": True, "ts": "201.000001"}
        client.chat_postMessage.side_effect = late_post
        content = "hello" if transport != "late_chunk" else "x" * (adapter.MAX_MESSAGE_LENGTH + 20)
        pending = asyncio.create_task(adapter.send("C1", content, metadata=META))
        await entered.wait()
        if transport == "late_post_cancelled":
            source = adapter.build_source(
                chat_id="C1", chat_type="group", user_id="UHUMAN",
                thread_id=META["thread_id"], scope_id="T1")
            adapter._slack_surface_sessions = {"cancelled-send": source}
            adapter._track_session_task("cancelled-send", pending)
            cleaned = asyncio.Event()
            async def cleanup(**kwargs):
                cleaned.set()
                return {"ok": True}
            client.chat_delete.side_effect = cleanup
        # Ambient channel cache must not steal the in-flight call's workspace.
        adapter._channel_team["C1"] = "T2"
        event = deletion()
        if transport == "late_first_reply_deleted":
            event = deletion(ts="201.000001")
            event.pop("previous_message")
        await adapter._handle_slack_message(event)
        if transport == "late_post_after_summon":
            await adapter._handle_slack_message(mention("<@UBOT> new work"))
        release.set()
        if transport == "late_post_cancelled":
            with pytest.raises(asyncio.CancelledError):
                await pending
            await asyncio.wait_for(cleaned.wait(), timeout=2)
            result = None
        else:
            result = await pending
        client.chat_delete.assert_awaited_once_with(channel="C1", ts="201.000001")
    assert result is None or not result.success
    assert client.chat_postMessage.await_count == (2 if transport.startswith("late") else 1)
    if transport != "late_post_after_summon":
        assert not (await adapter.send("C1", "final fallback", metadata=META)).success
