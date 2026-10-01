"""Startup must not manufacture Slack request authority from durable routing state."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionStore
from plugins.platforms.slack.adapter import SlackAdapter
from tests.gateway.restart_test_helpers import make_restart_runner


def native():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake"))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("state", ["authorized", "withdrawn", "deleted", "unauthorized"])
async def test_startup_recovery_requires_durable_request_authority(tmp_path, scoped, state):
    """An actual admitted request survives routing reload, but its permission does not.

    This pins the fail-closed blocker, not successful recovery: the routing index
    cannot distinguish an interrupted invitation from a withdrawn one. Adding
    internal/native-looking flags must not substitute for the consumed receipt.
    """
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    adapter, client = native()
    adapter.client_context_enabled = scoped
    adapter._client_context_admission = AsyncMock(return_value="scoped")
    entered = asyncio.Event()
    admitted = []

    async def interrupted_request(event):
        assert adapter.authorize_turn(event.source)
        entry = store.get_or_create_session(event.source)
        assert store.mark_resume_pending(entry.session_key, "restart_timeout")
        result = await adapter.send("C1", "task started", metadata={
            "team_id": "T1", "thread_id": "100.000001", "user_id": "UHUMAN"})
        assert result.success
        admitted.append((event, entry.session_key, entry.session_id))
        entered.set()
        await asyncio.Event().wait()

    adapter.set_message_handler(interrupted_request)
    event = {"type": "message", "channel": "C1", "team": "T1", "user": "UHUMAN",
             "ts": "120.000001", "thread_ts": "100.000001", "text": "<@UBOT> deliver the report"}
    try:
        await adapter._handle_slack_message(event)
        await asyncio.wait_for(entered.wait(), timeout=2)
        original, session_key, session_id = admitted[0]
        owner = adapter._session_tasks[session_key]
        assert getattr(original, "_client_context_required", False) is scoped
        assert client.chat_postMessage.await_count == 1
        if state == "withdrawn":
            await adapter._handle_slack_message({
                **event, "ts": "300.000001", "text": "no reply needed"})
            assert owner.done()
            assert not adapter.authorize_turn(original.source)
        elif state == "deleted":
            await adapter._handle_message_deleted({
                "channel": "C1", "team": "T1", "deleted_ts": "200.000001",
                "event_ts": "300.000001", "previous_message": {
                    "user": "UBOT", "ts": "200.000001", "thread_ts": "100.000001"}}, {})
            assert owner.done()
    finally:
        await adapter.cancel_background_tasks()

    restored, transport = native()
    restored.client_context_enabled = scoped
    restored._client_context_admission = AsyncMock(return_value="scoped")
    resumed = AsyncMock(return_value="resumed report")
    runner, _ = make_restart_runner(restored)
    restored.set_message_handler(resumed)
    runner.adapters = {Platform.SLACK: restored}
    runner.session_store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    runner._persist_active_agents = MagicMock()
    runner._is_user_authorized = lambda source: state != "unauthorized"
    restored._authorization_check = lambda *args, **kwargs: state != "unauthorized"
    recovered = runner.session_store.lookup_by_session_key(session_key)
    assert recovered.session_id == session_id and recovered.resume_pending
    assert recovered.origin.to_dict() == original.source.to_dict()
    key = ("T1", "C1", "100.000001")
    if state == "deleted":
        assert restored._deletion_record(key)["muted"]
        assert restored._deletion_generation(key) == adapter._deletion_generation(key)

    try:
        # Exercise the actual scheduler and synthetic-event constructor twice,
        # including its claimed-slot cleanup, rather than mocking handle_message.
        for _ in range(2):
            assert runner._schedule_resume_pending_sessions() == (state != "unauthorized")
            tasks = list(runner._background_tasks)
            if tasks:
                await asyncio.gather(*tasks)
            assert not runner._is_session_running(session_key)
            resumed.assert_not_awaited()
            assert not transport.mock_calls

        forged = MessageEvent(text="", source=recovered.origin, internal=True,
                              metadata=dict(original.metadata))
        await restored._process_message_background(forged, session_key)
        resumed.assert_not_awaited()
        assert not transport.mock_calls
        # A denied attempt must not claim the outstanding deliverable succeeded.
        assert runner.session_store.lookup_by_session_key(session_key).resume_pending
    finally:
        await restored.cancel_background_tasks()
