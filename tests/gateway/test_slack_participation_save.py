"""Threaded /save carries native Slack authority across transcript export."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from plugins.platforms.slack.adapter import SlackAdapter
from plugins.platforms.slack.participation import _participation_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ("empty_metadata", "forged_metadata", "revoked"))
async def test_native_thread_save_preserves_source_grant_through_export(scenario):
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._app = MagicMock()
    adapter._bot_user_id = "UBOT"
    adapter._team_bot_user_ids = {"T1": "UBOT"}
    adapter._authorization_check = lambda *args, **kwargs: True
    adapter._resolve_user_is_bot = AsyncMock(return_value=False)
    adapter._resolve_user_name = AsyncMock(return_value="Human")
    adapter._resolve_channel_name = AsyncMock(return_value="Channel")
    adapter._hydrate_thread_context = AsyncMock(return_value=(None, [], []))
    adapter._start_typing_refresh = MagicMock(return_value=None)
    adapter.stop_typing = AsyncMock()
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "200.000001"}
    client.files_upload_v2.return_value = {"ok": True, "files": [{"id": "F1"}]}
    adapter._get_client = MagicMock(return_value=client)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.get_adapter = lambda platform: adapter
    runner.adapters = {Platform.SLACK: adapter}
    runner.session_store = SimpleNamespace()
    runner._async_session_store = SimpleNamespace(_store=runner.session_store,
        get_or_create_session=AsyncMock(return_value=SimpleNamespace(session_id="saved")))
    entered, release = asyncio.Event(), asyncio.Event()
    grants, results, destinations = [], [], []

    async def export_session(session_id, include_compacted=False):
        assert session_id == "saved"
        grants.append(_participation_scope.get())
        entered.set()
        if scenario == "revoked":
            try:
                await release.wait()
            except asyncio.CancelledError:
                # A boundary finishing despite cancellation still cannot publish.
                pass
        return {"session_id": session_id, "messages": [{"role": "user", "content": "private"}]}

    runner._session_db = SimpleNamespace(export_session=export_session)

    async def upload(**kwargs):
        grant = _participation_scope.get()
        assert grant is grants[0]
        assert grant.requester == "UHUMAN"
        assert grant.key == ("T1", "C1", "100.000001")
        destinations.append(kwargs)
        return {"ok": True, "files": [{"id": "F1"}]}

    client.files_upload_v2.side_effect = upload

    async def handler(event):
        # Admission has already happened. No event metadata is required to derive
        # the outbound destination, and model-like metadata cannot widen it.
        event.metadata = {} if scenario != "forged_metadata" else {
            "team_id": "TFOREIGN", "thread_id": "999.000001", "user_id": "UOTHER",
            "job_id": "forged-job", "_slack_participation_id": "forged-grant"}
        destinations.append(runner._reply_metadata(event))
        results.append(await runner._handle_save_command(event))

    adapter.set_message_handler(handler)
    await adapter._handle_slack_message({
        "type": "message", "channel": "C1", "team": "T1", "user": "UHUMAN",
        "ts": "120.000001", "thread_ts": "100.000001", "text": "<@UBOT> /save json"})
    await asyncio.wait_for(entered.wait(), timeout=2)
    tasks = list(adapter._background_tasks)
    if scenario == "revoked":
        await adapter._handle_slack_message({
            "type": "message", "channel": "C1", "team": "T1", "user": "UHUMAN",
            "ts": "130.000001", "thread_ts": "100.000001", "text": "no reply needed"})
    release.set()
    await asyncio.gather(*tasks)
    assert destinations[0] == {
        "thread_id": "100.000001", "slack_team_id": "T1", "scope_id": "T1",
        "user_id": "UHUMAN", "message_id": "120.000001"}
    if scenario == "revoked":
        client.files_upload_v2.assert_not_awaited()
        assert results and results[0] != "Export complete."
    else:
        assert results == ["Export complete."]
        client.files_upload_v2.assert_awaited_once()
        assert destinations[1]["channel"] == "C1"
        assert destinations[1]["thread_ts"] == "100.000001"
        assert any(call.kwargs.get("team_id") == "T1" for call in adapter._get_client.call_args_list)
