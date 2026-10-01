"""Offline native regressions for thread authorization, root edits and workflows."""
import asyncio
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.profile_routing import ProfileRoute
from gateway.run import GatewayRunner
from tests.gateway.test_slack_participation_delivery import META, OUTPUTS, native as native, output, start_turn
from tests.gateway.test_multiplex_interactive_auth import mux_home as mux_home, _runner as pairing_runner


def _thread_pairing_runner(adapter, home):
    runner = pairing_runner(home)
    runner.config.profile_routes = [ProfileRoute(
        name="thread", platform="slack", chat_id="C1", thread_id=META["thread_id"], profile="secondary")]
    runner.config.platforms = {Platform.SLACK: adapter.config}
    runner.adapters = {Platform.SLACK: adapter}
    for store in runner.pairing_stores.values():
        store._save_json(store._approved_path("slack"), {"UHUMAN": {}})
    runner._is_user_authorized_for_source = MagicMock(wraps=runner._is_user_authorized_for_source)
    adapter.set_authorization_check(runner._make_adapter_auth_check(Platform.SLACK))
    return runner


def _revoke_thread_pairing(runner, adapter):
    assert runner.pairing_stores["secondary"].revoke("slack", "UHUMAN")
    assert not runner.pairing_stores["secondary"].is_approved("slack", "UHUMAN")
    assert runner.pairing_store.is_approved("slack", "UHUMAN")
    # The default profile would still authorize a source stripped of its thread.
    assert adapter._is_interactive_user_authorized(
        "UHUMAN", channel_id="C1", team_id="T1")
    runner._is_user_authorized_for_source.reset_mock()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", OUTPUTS)
async def test_thread_routed_output_never_falls_back_to_default(native, kind, mux_home):
    adapter, client, attachment = native
    runner = _thread_pairing_runner(adapter, mux_home)
    context, release, owner = await start_turn(adapter)
    try:
        _revoke_thread_pairing(runner, adapter)
        client.reset_mock()
        await context.run(asyncio.create_task, output(adapter, kind, attachment))
        checked = [call.args[0] for call in runner._is_user_authorized_for_source.call_args_list]
        assert checked
        assert all(source.thread_id == META["thread_id"] and source.scope_id == "T1"
                   and source.profile == "secondary" for source in checked)
        assert not client.mock_calls
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_root_status_edits_require_exact_task_receipt(native, monkeypatch):
    adapter, client, _ = native
    adapter.config.extra['reply_in_thread'] = False
    contexts, releases, owners = [], [], []
    entered = asyncio.Event()
    metadata = {'team_id': 'T1', 'user_id': 'UHUMAN'}
    client.chat_postMessage.side_effect = [
        {'ok': True, 'ts': '200.000001'}, {'ok': True, 'ts': '201.000001'}]

    async def handler(event):
        release = asyncio.Event()
        releases.append(release)
        contexts.append(copy_context())
        entered.set()
        await release.wait()

    adapter.set_message_handler(handler)
    try:
        for ts in ('120.000001', '130.000001'):
            entered.clear()
            await adapter._handle_slack_message({
                'type': 'message', 'channel': 'C1', 'team': 'T1', 'user': 'UHUMAN',
                'ts': ts, 'text': '<@UBOT> start root work'})
            await asyncio.wait_for(entered.wait(), timeout=2)
            owner = next(iter(adapter._session_tasks.values()))
            owners.append(owner)
            context = contexts[-1]
            created = await context.run(asyncio.create_task, adapter.send_or_update_status(
                'C1', ts, 'working', metadata=metadata))
            assert created.success
            client.chat_update.reset_mock()
            updated = await context.run(asyncio.create_task, adapter.send_or_update_status(
                'C1', ts, 'finished', metadata=metadata))
            assert updated.success
            client.chat_update.assert_awaited_once()
            assert adapter._deletion_messages()[('T1', 'C1', created.message_id)] == (
                'T1', 'C1', created.message_id)
            for target in ('999.000001', '200.000001') if len(owners) == 2 else ('999.000001',):
                client.reset_mock()
                denied = await context.run(asyncio.create_task, adapter.edit_message(
                    'C1', target, 'foreign task', metadata=metadata))
                assert not denied.success
                assert not client.mock_calls
            client.reset_mock()
            denied = await context.run(asyncio.create_task, adapter.send(
                'C1', 'new threaded work', metadata={**metadata, 'thread_id': created.message_id}))
            assert not denied.success
            assert not client.mock_calls
            from plugins.platforms.slack.participation import ParticipationDenied
            requests = []

            async def transport(**kwargs):
                requests.append(kwargs)
                return {"status_code": 200, "headers": {}, "data": {"ok": True}}

            monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
            sdk = adapter._new_web_client("fake", None)
            adapter._team_clients["T1"] = sdk
            with pytest.raises(ParticipationDenied):
                await context.run(asyncio.create_task, sdk.chat_postMessage(
                    channel="C1", thread_ts=created.message_id, ts=created.message_id,
                    text="forged edit fields cannot create a thread"))
            assert not requests
            releases[-1].set()
            await owner
    finally:
        for release in releases:
            release.set()
        await asyncio.gather(*owners, return_exceptions=True)
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize('policy,accepted', [(None, False), ('none', False), ('mentions', True), ('all', True)])
async def test_workflow_no_user_reaches_existing_bot_authorization(native, monkeypatch, policy, accepted):
    adapter, client, _ = native
    if policy is not None:
        monkeypatch.setenv('SLACK_ALLOW_BOTS', policy)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.pairing_store = SimpleNamespace(is_approved=lambda *_a, **_kw: False)
    runner.adapters = {Platform.SLACK: adapter}
    adapter.set_authorization_check(runner._make_adapter_auth_check(Platform.SLACK))
    seen = []

    async def handler(event):
        assert runner._is_user_authorized(event.source)
        seen.append(event.source)
        result = await adapter.send('C1', 'workflow result', metadata=META)
        assert result.success

    adapter.set_message_handler(handler)
    await adapter._handle_slack_message({
        'type': 'message', 'subtype': 'bot_message', 'bot_id': 'BWORKFLOW', 'user': None,
        'channel': 'C1', 'team': 'T1', 'ts': '120.000001', 'thread_ts': META['thread_id'],
        'text': '<@UBOT> process workflow'})
    await asyncio.gather(*adapter._background_tasks)
    assert bool(seen) is accepted
    if accepted:
        assert seen[0].user_id is None and seen[0].is_bot is True
        client.chat_postMessage.assert_awaited_once()
    else:
        assert not client.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_at", ["before_click", "ack", "client_context"])
async def test_thread_routed_confirmation_rechecks_original_source(native, mux_home, revoke_at):
    from tools import slash_confirm

    adapter, client, _ = native
    runner = _thread_pairing_runner(adapter, mux_home)
    context, release, owner = await start_turn(adapter)
    session = next(iter(adapter._session_tasks))
    confirmed = AsyncMock(return_value="confirmed")
    slash_confirm.register(session, "thread-confirm", "/reload-mcp", confirmed)
    await context.run(asyncio.create_task, adapter.send_slash_confirm(
        "C1", "Reload", "Proceed?", session, "thread-confirm", metadata=META))
    release.set()
    await owner
    runner._is_user_authorized_for_source.reset_mock()
    if revoke_at == "before_click":
        _revoke_thread_pairing(runner, adapter)
    admission_sources = []

    async def admission(source):
        admission_sources.append(source)
        return "deny" if source.thread_id == META["thread_id"] else "legacy"

    if revoke_at == "client_context":
        adapter.client_context_enabled = True
        adapter._client_context_admission = admission

    async def ack():
        if revoke_at == "ack":
            _revoke_thread_pairing(runner, adapter)

    try:
        client.reset_mock()
        await adapter._handle_slash_confirm_action(ack,
            {"team": {"id": "T1"}, "channel": {"id": "C1"}, "user": {"id": "UHUMAN"},
             # The stored source is authoritative even when Slack omits thread_ts.
             "message": {"ts": "200.000001"}},
            {"action_id": "hermes_confirm_once", "value": f"{session}|thread-confirm"})
        checked = [call.args[0] for call in runner._is_user_authorized_for_source.call_args_list]
        assert checked
        assert all(source.thread_id == META["thread_id"] and source.scope_id == "T1"
                   and source.profile == "secondary" for source in checked)
        if revoke_at == "client_context":
            assert runner.pairing_stores["secondary"].is_approved("slack", "UHUMAN")
            assert runner.pairing_store.is_approved("slack", "UHUMAN")
            assert len(admission_sources) == 1
            source = admission_sources[0]
            assert (source.scope_id, source.chat_id, source.thread_id, source.user_id) == (
                "T1", "C1", META["thread_id"], "UHUMAN")
        confirmed.assert_not_awaited()
        assert not client.mock_calls
    finally:
        slash_confirm.clear(session)
        await adapter.cancel_background_tasks()
