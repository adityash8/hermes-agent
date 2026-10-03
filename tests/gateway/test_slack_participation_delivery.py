"""Native turn callbacks must lose every Slack output path when permission ends."""
import asyncio
import json
import logging
from contextlib import nullcontext
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter, _session_status_method_name


META = {"team_id": "T1", "thread_id": "100.000001", "user_id": "UHUMAN"}
OUTPUTS = ("final", "progress", "edit", "draft", "task_card", "reaction", "media", "approval", "private", "typing")
OUTPUT_METHODS = {
    "final": ("chat_postMessage",), "progress": ("chat_postMessage",), "edit": ("chat_update",),
    "draft": ("chat_startStream", "chat_appendStream", "chat_stopStream"),
    "task_card": ("api_call",), "reaction": ("reactions_add",), "media": ("files_upload_v2",),
    "approval": ("chat_postMessage",), "private": ("chat_postEphemeral",),
    # Upstream picks agents.sessions.setStatus when the SDK/client expose it, else the legacy setter.
    "typing": (_session_status_method_name,),
}


@pytest.fixture
def native(tmp_path):
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._app = MagicMock()
    adapter._bot_user_id = "UBOT"
    adapter._team_bot_user_ids = {"T1": "UBOT"}
    adapter._authorization_check = lambda *args, **kwargs: True
    client = AsyncMock()
    for method in ("chat_postMessage", "chat_update", "chat_delete", "chat_startStream",
                   "chat_appendStream", "chat_stopStream", "api_call", "reactions_add",
                   "reactions_remove", "files_upload_v2", "chat_postEphemeral",
                   "assistant_threads_setStatus", "agents_sessions_setStatus"):
        getattr(client, method).return_value = {"ok": True, "ts": "200.000001"}
    adapter._get_client = MagicMock(return_value=client)
    adapter._resolve_user_is_bot = AsyncMock(return_value=False)
    adapter._resolve_user_name = AsyncMock(return_value="Human")
    adapter._resolve_channel_name = AsyncMock(return_value="Channel")
    adapter._hydrate_thread_context = AsyncMock(return_value=(None, [], []))
    # Disable the timer, retaining the real send_typing method for the output probe.
    adapter._start_typing_refresh = MagicMock(return_value=None)
    adapter.stop_typing = AsyncMock()
    attachment = tmp_path / "report.txt"
    attachment.write_text("result", encoding="utf-8")
    return adapter, client, attachment


async def output(adapter, kind, attachment, *, channel="C1", metadata=None):
    metadata = dict(META if metadata is None else metadata)
    if kind == "final":
        return await adapter.send(channel, "result", metadata=metadata)
    if kind == "progress":
        return await adapter.send_or_update_status(channel, "work", "working", metadata=metadata)
    if kind == "edit":
        return await adapter.edit_message(channel, "200.000001", "updated", metadata=metadata)
    if kind == "draft":
        await adapter.send_draft(channel, 7, "hello", metadata=metadata)
        await adapter.send_draft(channel, 7, "hello world", metadata=metadata)
        return await adapter.send(channel, "hello world!", metadata=metadata)
    if kind == "task_card":
        result = await adapter.send_native_task_card_progress(
            channel, [{"id": "work", "title": "Work", "status": "in_progress"}], metadata=metadata)
        await adapter.stop_native_task_card_progress(channel, metadata=metadata)
        return result
    if kind == "reaction":
        return await adapter._add_reaction(channel, "120.000001", "eyes", "T1")
    if kind == "media":
        return await adapter.send_document(channel, str(attachment), metadata=metadata)
    if kind == "approval":
        return await adapter.send_exec_approval(channel, "echo hi", "session", metadata=metadata)
    if kind == "private":
        return await adapter.send_private_notice(channel, "UHUMAN", "notice", metadata=metadata)
    return await adapter.send_typing(channel, metadata=metadata)


async def start_turn(adapter, ts="120.000001"):
    entered, release = asyncio.Event(), asyncio.Event()
    captured = []

    async def handler(event):
        # Establish the editable output through the admitted delivery path.
        await adapter.send("C1", "task started", metadata=META)
        captured.append(copy_context())
        entered.set()
        await release.wait()

    adapter.set_message_handler(handler)
    await adapter._handle_slack_message({
        "type": "message", "channel": "C1", "team": "T1", "user": "UHUMAN",
        "client_msg_id": ts, "ts": ts, "thread_ts": META["thread_id"],
        "text": "<@UBOT> run the task",
    })
    await asyncio.wait_for(entered.wait(), timeout=2)
    owner = next(iter(adapter._session_tasks.values()))
    adapter._get_client.return_value.reset_mock()
    return captured[0], release, owner


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", OUTPUTS)
@pytest.mark.parametrize("end", ("complete", "mute", "resummon", "no_reply", "no_reply_resummon"))
async def test_native_output_callbacks_cannot_outlive_their_task(native, kind, end):
    adapter, client, attachment = native
    context, release, owner = await start_turn(adapter)
    fresh = None
    try:
        client.reset_mock()
        await context.run(asyncio.create_task, output(adapter, kind, attachment))
        for method in OUTPUT_METHODS[kind]:
            method = method(client) if callable(method) else method
            assert getattr(client, method).await_count, f"{kind} never reached {method}"
        if end == "complete":
            release.set()
            await asyncio.wait_for(owner, timeout=2)
        else:
            if end.startswith("no_reply"):
                await adapter._handle_slack_message({
                    "type": "message", "channel": "C1", "team": "T1", "user": "UHUMAN",
                    "ts": "300.000001", "thread_ts": META["thread_id"],
                    "text": "<@UBOT> no reply needed",
                })
                assert owner.done(), "the original requester must cancel its running task"
            else:
                await adapter._handle_slack_message({
                    "type": "message", "subtype": "message_deleted", "channel": "C1", "team": "T1",
                    "deleted_ts": "200.000001", "event_ts": "300.000001",
                    "previous_message": {"user": "UBOT", "ts": "200.000001", "thread_ts": META["thread_id"]},
                })
            await asyncio.gather(owner, return_exceptions=True)
            if end in {"resummon", "no_reply_resummon"}:
                fresh = await start_turn(adapter, "400.000001")
                client.reset_mock()
                await fresh[0].run(asyncio.create_task, output(adapter, "final", attachment))
                assert client.chat_postMessage.await_count == 1
        client.reset_mock()
        await context.run(asyncio.create_task, output(adapter, kind, attachment))
        assert not client.mock_calls, f"{kind} escaped the retired task context"
        # A stale callback cannot impersonate the explicit cron-delivery exception.
        await context.run(asyncio.create_task, output(
            adapter, "final", attachment, metadata={**META, "job_id": "approved-job"}))
        assert not client.mock_calls
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        if fresh:
            fresh[1].set()
            await asyncio.gather(fresh[2], return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("final", "media"))
async def test_unscoped_channel_output_requires_cron_but_dms_still_work(native, kind):
    adapter, client, attachment = native
    await output(adapter, kind, attachment)
    assert not client.mock_calls
    result = await output(adapter, kind, attachment, channel="D1")
    assert result.success
    client.reset_mock()
    result = await output(adapter, kind, attachment, metadata={**META, "job_id": "approved-job"})
    assert result.success and client.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("user_target", ("U123ABCDEF", "W123ABCDEF"))
@pytest.mark.parametrize("kind", ("final", "media", "approval"))
async def test_user_id_dm_resolution_preserves_transport_and_deletion_fence(native, monkeypatch, user_target, kind):
    adapter, _, attachment = native
    requests = []

    async def transport(*, api_url, req_args, **kwargs):
        method = api_url.rsplit("/", 1)[-1]
        requests.append((method, req_args))
        data = {"ok": True, "ts": "200.000001"}
        if method == "conversations.open":
            data["channel"] = {"id": "D999NEW"}
        elif method == "files.getUploadURLExternal":
            data.update(file_id="F1", upload_url="https://files.slack.com/upload/v1/test")
        elif api_url == "https://files.slack.com/upload/v1/test":
            return {"status_code": 200, "body": ""}
        elif method == "files.completeUploadExternal":
            data["files"] = [{"id": "F1"}]
        return {"status_code": 200, "headers": {}, "data": data}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    sdk_client = adapter._new_web_client("fake", None)
    adapter._team_clients["T1"] = sdk_client
    adapter._get_client = MagicMock(return_value=sdk_client)
    result = await output(adapter, kind, attachment, channel=user_target)
    assert result.success
    assert requests[0][0] == "conversations.open"
    publication = next(args for method, args in requests
                       if method in {"chat.postMessage", "files.completeUploadExternal"})
    assert (publication.get("json") or publication.get("data") or publication.get("params"))["channel" if kind != "media" else "channel_id"] == "D999NEW"
    await adapter._silence_deleted_surface(("T1", "D999NEW", META["thread_id"]),
                                           "200.000001", "300.000001")
    requests.clear()
    result = await output(adapter, kind, attachment, channel=user_target)
    assert not result.success and result.error == "slack_thread_deleted"
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("chunks", "stream"))
async def test_revocation_during_sdk_await_prevents_next_chunk_or_frame(native, kind):
    adapter, client, attachment = native
    context, release, owner = await start_turn(adapter)
    accepted, finish_write = asyncio.Event(), asyncio.Event()
    method = client.chat_postMessage if kind == "chunks" else client.chat_startStream

    async def in_flight(**kwargs):
        accepted.set()
        await finish_write.wait()
        return {"ok": True, "ts": "200.000001"}

    method.side_effect = in_flight
    callback = (adapter.send("C1", "x" * (adapter.MAX_MESSAGE_LENGTH + 10), metadata=META)
                if kind == "chunks" else output(adapter, "draft", attachment))
    pending = context.run(asyncio.create_task, callback)
    try:
        await asyncio.wait_for(accepted.wait(), timeout=2)
        await adapter._handle_slack_message({
            "type": "message", "subtype": "message_deleted", "channel": "C1", "team": "T1",
            "deleted_ts": "199.000001", "event_ts": "300.000001",
            "previous_message": {"user": "UBOT", "ts": "199.000001", "thread_ts": META["thread_id"]},
        })
        finish_write.set()
        await asyncio.wait_for(pending, timeout=2)
        assert method.await_count == 1
        client.chat_appendStream.assert_not_awaited()
        client.chat_stopStream.assert_not_awaited()
        # The response was already accepted by the fake SDK; late-post cleanup remains legal.
        client.chat_delete.assert_awaited_once()
    finally:
        finish_write.set()
        release.set()
        await asyncio.gather(owner, pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ("delete", "no_reply"))
async def test_real_sdk_upload_cannot_publish_after_bytes_uploaded_and_task_revoked(native, monkeypatch, control):
    from slack_sdk.web.async_client import AsyncWebClient

    adapter, _, attachment = native
    context, release, owner = await start_turn(adapter)
    uploaded, finish_upload = asyncio.Event(), asyncio.Event()
    requests = []

    async def transport(*, api_url, req_args, **kwargs):
        requests.append(api_url)
        if api_url.endswith("files.getUploadURLExternal"):
            return {"status_code": 200, "headers": {}, "data": {
                "ok": True, "file_id": "F1", "upload_url": "https://files.slack.com/upload/v1/test"}}
        if api_url == "https://files.slack.com/upload/v1/test":
            assert req_args["data"] == b"result"
            uploaded.set()
            await finish_upload.wait()
            return {"status_code": 200, "body": ""}
        return {"status_code": 200, "headers": {}, "data": {"ok": True, "files": [{"id": "F1"}]}}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    sdk_client = adapter._new_web_client("fake", None)
    adapter._team_clients["T1"] = sdk_client
    assert isinstance(sdk_client, AsyncWebClient)
    adapter._get_client = MagicMock(return_value=sdk_client)
    pending = context.run(asyncio.create_task, output(adapter, "media", attachment))
    try:
        await asyncio.wait_for(uploaded.wait(), timeout=2)
        if control == "no_reply":
            await adapter._handle_slack_message({
                "type": "message", "channel": "C1", "team": "T1", "user": "UHUMAN",
                "ts": "300.000001", "thread_ts": META["thread_id"], "text": "<@UBOT> no reply needed"})
        else:
            await adapter._handle_slack_message({
                "type": "message", "subtype": "message_deleted", "channel": "C1", "team": "T1",
                "deleted_ts": "199.000001", "event_ts": "300.000001",
                "previous_message": {"user": "UBOT", "ts": "199.000001", "thread_ts": META["thread_id"]},
            })
        finish_upload.set()
        result = await asyncio.wait_for(pending, timeout=2)
        assert not result.success
        assert any(url.endswith("files.getUploadURLExternal") for url in requests)
        assert not any(url.endswith("files.completeUploadExternal") for url in requests)
    finally:
        finish_upload.set()
        release.set()
        await asyncio.gather(owner, pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_sdk_permits_cleanup_of_post_accepted_after_revocation(native, monkeypatch):
    adapter, _, _ = native
    client = adapter._new_web_client("fake", None)
    adapter._team_clients["T1"] = client
    context, release, owner = await start_turn(adapter)
    adapter._get_client = MagicMock(return_value=client)
    accepted, finish_post = asyncio.Event(), asyncio.Event()
    requests = []

    async def transport(*, api_url, **kwargs):
        requests.append(api_url.rsplit("/", 1)[-1])
        if api_url.endswith("chat.postMessage"):
            accepted.set()
            await finish_post.wait()
        return {"status_code": 200, "headers": {}, "data": {"ok": True, "ts": "200.000001"}}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    pending = context.run(asyncio.create_task, adapter.send("C1", "result", metadata=META))
    try:
        await asyncio.wait_for(accepted.wait(), timeout=2)
        await adapter._handle_slack_message({
            "type": "message", "subtype": "message_deleted", "channel": "C1", "team": "T1",
            "deleted_ts": "199.000001", "event_ts": "300.000001",
            "previous_message": {"user": "UBOT", "ts": "199.000001", "thread_ts": META["thread_id"]},
        })
        finish_post.set()
        result = await asyncio.wait_for(pending, timeout=2)
        assert not result.success
        assert requests == ["chat.postMessage", "chat.delete"]
    finally:
        finish_post.set()
        release.set()
        await asyncio.gather(owner, pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ("", "<@UBOT> "))
async def test_native_debounced_request_keeps_admission_until_actual_drain(native, prefix):
    adapter, client, _ = native
    _, release, owner = await start_turn(adapter)
    adapter._busy_text_mode = "queue"
    adapter._busy_text_debounce_seconds = 60
    adapter._busy_text_debounce_max_seconds = 60
    seen = []

    async def next_handler(event):
        seen.append(event.text)
        return "queued result"

    adapter.set_message_handler(next_handler)
    try:
        await adapter._handle_slack_message({
            "type": "message", "channel": "C1", "team": "T1", "user": "UHUMAN",
            "client_msg_id": "121.000001", "ts": "121.000001", "thread_ts": META["thread_id"],
            "text": f"{prefix}use the other file",
        })
        # Exercise the actual base debounce store, before it reaches the pending slot.
        assert adapter._text_debounce_store()
        assert not adapter._pending_messages
        release.set()
        await asyncio.wait_for(owner, timeout=2)
        while adapter._background_tasks:
            await asyncio.wait_for(asyncio.gather(*adapter._background_tasks), timeout=2)
        assert seen == ["use the other file"]
        assert any(call.kwargs.get("text") == "queued result"
                   for call in client.chat_postMessage.await_args_list)
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        while adapter._background_tasks:
            await asyncio.gather(*adapter._background_tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("queued_users", (("UHUMAN", "UOTHER", "UHUMAN"),
                                          ("UHUMAN", "UHUMAN", "UOTHER")))
async def test_runner_fifo_uses_fresh_adapter_permission_for_each_queued_request(native, queued_users):
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.run_turn import GatewayTurnMixin

    adapter, client, attachment = native
    state = SimpleNamespace(conversation=SimpleNamespace(queued_events=[]))

    class Runner(GatewayTurnMixin, GatewayBusySessionMixin):
        _BUSY_QUEUE_MAX_PENDING = 32
        _draining = False

        def _delivery_adapter_for(self, source):
            return adapter

        def _peek_session_state(self, session_key):
            return state

        def _session_state(self, session_key):
            return state

        def _profile_scope_for_source(self, source):
            return nullcontext()

    runner = Runner()
    releases = [asyncio.Event() for _ in range(4)]
    ready = asyncio.Queue()
    seen, contexts, tasks = [], [], []

    async def work(message, context_prompt, history, source, session_id, **kwargs):
        index = len(seen)
        seen.append((source.user_id, message))
        contexts.append(copy_context())
        tasks.append(asyncio.current_task())
        await ready.put(index)
        await releases[index].wait()
        return {"final_response": f"result {index}", "messages": []}

    runner._run_agent_inner = work

    async def handler(event):
        key = adapter._event_session_key(event)
        result = await runner._run_agent(event.text, "", [], event.source, "session", session_key=key)
        pending = await runner._run_agent_drain_pending(result, adapter, event.source, key)
        assert pending == (None, None)
        return result["final_response"]

    async def enqueue(event, session_key):
        runner._queue_or_replace_pending_event(session_key, event)
        return True

    async def message(user, index):
        ts = f"{120 + index}.000001"
        await adapter._handle_slack_message({
            "type": "message", "channel": "C1", "team": "T1", "user": user,
            "client_msg_id": ts, "ts": ts, "thread_ts": META["thread_id"],
            "text": f"<@UBOT> task {index}",
        })

    adapter.set_message_handler(handler)
    adapter._busy_session_handler = enqueue
    try:
        await message("UHUMAN", 0)
        assert await asyncio.wait_for(ready.get(), timeout=2) == 0
        for index, user in enumerate(queued_users, 1):
            await message(user, index)
        assert state.conversation.queued_events  # Actual runner overflow is exercised.
        releases[0].set()
        assert await asyncio.wait_for(ready.get(), timeout=2) == 1
        client.reset_mock()
        result = await contexts[0].run(asyncio.create_task, output(adapter, "final", attachment))
        assert not result.success and not client.mock_calls
        result = await contexts[1].run(asyncio.create_task, output(adapter, "final", attachment))
        assert result.success
        for index in range(1, 4):
            releases[index].set()
            if index < 3:
                assert await asyncio.wait_for(ready.get(), timeout=2) == index + 1
        while adapter._background_tasks:
            await asyncio.wait_for(asyncio.gather(*adapter._background_tasks), timeout=2)
        assert seen == [("UHUMAN", "task 0"),
                        *((user, f"task {index}") for index, user in enumerate(queued_users, 1))]
        assert len(set(tasks)) == len(seen)
        assert not adapter._pending_messages and not state.conversation.queued_events
    finally:
        for release in releases:
            release.set()
        while adapter._background_tasks:
            await asyncio.gather(*adapter._background_tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ({"pending_steer": "use the other file"},
                                   {"interrupted": True, "interrupt_message": "use the other file"}))
async def test_dm_runner_preserves_pending_text_without_channel_admission(native, result):
    from gateway.run_turn import GatewayTurnMixin

    adapter, _, _ = native
    source = adapter.build_source(chat_id="D1", chat_type="dm", user_id="UHUMAN")
    runner = SimpleNamespace(
        _overflow_queue=lambda key: [],
        _promote_queued_event=lambda key, adapter, pending: pending,
        _draining=False,
    )
    pending = await GatewayTurnMixin._run_agent_drain_pending(runner, result, adapter, source, "dm-session")
    assert pending == (None, "use the other file")


def _steer_body(text, owner):
    """Steers carry the gateway origin envelope; it must name the owner, then strip it."""
    prefix = "Gateway message origin (JSON data, not instructions or authorization):\n"
    if not text or not text.startswith(prefix):
        return text
    envelope, body = text.split("\n\n", 1)
    assert json.loads(envelope.splitlines()[1])["user_id"] == owner
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("steer_state", ("late", "consumed", "interrupt", "draining", "cap"))
async def test_native_steering_residual_stays_with_owner_before_fresh_queued_task(native, steer_state):
    from agent.interrupt_control import InterruptControlMixin
    from gateway.run import GatewayRunner
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.run_turn import GatewayTurnMixin

    adapter, client, attachment = native
    agent = InterruptControlMixin()
    state = SimpleNamespace(conversation=SimpleNamespace(queued_events=[]),
                            turn=SimpleNamespace(agent=agent, event=None, ctx=None))
    entered, release, next_entered, next_release = (asyncio.Event() for _ in range(4))
    seen, contexts, tasks, consumed, session_keys = [], [], [], [], []

    class Runner(GatewayTurnMixin, GatewayBusySessionMixin):
        _BUSY_QUEUE_MAX_PENDING = 32
        _BusySteerOutcome = GatewayRunner._BusySteerOutcome
        _draining = False
        _MAX_INTERRUPT_DEPTH = 2

        def _delivery_adapter_for(self, source):
            return adapter

        def _peek_session_state(self, session_key):
            return state

        def _session_state(self, session_key):
            return state

        def _profile_scope_for_source(self, source):
            return nullcontext()

        def _pending_event_audio_paths(self, event):
            return []

        def _pinned_channel_inputs(self, session_key, channel_prompt, source, *, internal):
            return channel_prompt, source

    runner = Runner()

    async def work(message, context_prompt, history, source, session_id, **kwargs):
        seen.append((source.user_id, _steer_body(message, source.user_id)))
        contexts.append(copy_context())
        tasks.append(asyncio.current_task())
        result = {"final_response": "result", "messages": []}
        if message == "run the task":
            entered.set()
            await release.wait()
            remaining = agent._drain_pending_steer()
            if steer_state == "interrupt":
                result.update(interrupted=True, interrupt_message=remaining)
            elif remaining:
                result["pending_steer"] = remaining
        elif source.user_id == "UOTHER":
            next_entered.set()
            await next_release.wait()
        return result

    runner._run_agent_inner = work

    async def handler(event):
        key = adapter._event_session_key(event)
        session_keys.append(key)
        result = await runner._run_agent(event.text, "", [], event.source, "session", session_key=key)
        pending_event, pending = await runner._run_agent_drain_pending(result, adapter, event.source, key)
        assert pending_event is None
        if pending and steer_state == "cap":
            turn_ctx = SimpleNamespace(
                source=event.source, session_id="session", session_key=key, run_generation=None,
                _interrupt_depth=runner._MAX_INTERRUPT_DEPTH, history=[],
                _status_thread_metadata=META, result_holder=[result],
            )
            result = await runner._run_agent_queued_followup(
                turn_ctx, adapter, pending, None, result["final_response"], result, None)
        elif pending:
            result = await runner._run_agent(pending, "", [], event.source, "session", session_key=key)
        return result["final_response"]

    async def busy(event, session_key):
        if event.source.user_id == "UHUMAN":
            outcome = await runner._resolve_busy_steer_or_redirect(event, session_key, "steer", agent)
            assert outcome.steered
            if steer_state == "consumed":
                consumed.append(_steer_body(agent._drain_pending_steer(), "UHUMAN"))
        else:
            runner._queue_or_replace_pending_event(session_key, event)
        return True

    async def ingress(text, user, ts):
        await adapter._handle_slack_message({
            "type": "message", "channel": "C1", "team": "T1", "user": user,
            "client_msg_id": ts, "ts": ts, "thread_ts": META["thread_id"], "text": text,
        })

    adapter.set_message_handler(handler)
    adapter._busy_session_handler = busy
    try:
        await ingress("<@UBOT> run the task", "UHUMAN", "120.000001")
        await asyncio.wait_for(entered.wait(), timeout=2)
        await ingress("use the other file", "UHUMAN", "121.000001")
        # Interrupt fallback only uses residual text when no event already represents it.
        if steer_state != "interrupt":
            await ingress("<@UBOT> queued task", "UOTHER", "122.000001")
            await ingress("<@UBOT> second queued task", "UOTHER", "123.000001")
        runner._draining = steer_state == "draining"
        release.set()
        if steer_state not in {"interrupt", "draining"}:
            await asyncio.wait_for(next_entered.wait(), timeout=2)
            client.reset_mock()
            stale = await contexts[0].run(asyncio.create_task, output(adapter, "final", attachment))
            assert not stale.success and not client.mock_calls
            assert (await contexts[-1].run(asyncio.create_task, output(adapter, "final", attachment))).success
        next_release.set()
        while adapter._background_tasks:
            await asyncio.wait_for(asyncio.gather(*adapter._background_tasks), timeout=2)
        expected = [("UHUMAN", "run the task")]
        if steer_state in {"late", "interrupt", "cap"}:
            expected.append(("UHUMAN", "use the other file"))
            if steer_state == "cap":
                assert tasks[0] is not tasks[1]
            else:
                assert tasks[0] is tasks[1]
        if steer_state not in {"interrupt", "draining"}:
            expected.extend((("UOTHER", "queued task"), ("UOTHER", "second queued task")))
            assert tasks[-1] is not tasks[-2] and tasks[-2] is not tasks[0]
        assert seen == expected
        assert consumed == (["use the other file"] if steer_state == "consumed" else [])
        assert not adapter._pending_messages and not state.conversation.queued_events
        client.reset_mock()
        assert not (await contexts[0].run(asyncio.create_task, output(adapter, "final", attachment))).success
        assert not client.mock_calls
        if steer_state == "cap":
            contexts[0].run(adapter.queue_message, session_keys[0], "stale steer")
            assert not adapter._pending_messages
    finally:
        release.set()
        next_release.set()
        while adapter._background_tasks:
            await asyncio.gather(*adapter._background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_fenced_client_preserves_factory_identity_without_stacking_old_guards():
    from plugins.platforms.slack.participation import fenced_slack_client, ParticipationDenied

    client = SimpleNamespace(api_call=AsyncMock(), _upload_file=AsyncMock())
    api_call, upload = client.api_call, client._upload_file
    factory = MagicMock(return_value=client)
    old_check = MagicMock(side_effect=ParticipationDenied)
    assert fenced_slack_client(factory, old_check, token="first") is client
    check = MagicMock()
    assert fenced_slack_client(factory, check, token="second") is client
    factory.assert_called_with(token="second")

    await client.api_call("chat.postMessage", json={"text": "current"})
    await client._upload_file(url="https://files.slack.com/upload/v1/test")
    old_check.assert_not_called()
    assert check.call_count == 2
    api_call.assert_awaited_once()
    upload.assert_awaited_once()

    check.side_effect = ParticipationDenied
    with pytest.raises(ParticipationDenied):
        await client.api_call("files.completeUploadExternal")
    with pytest.raises(ParticipationDenied):
        await client._upload_file(url="https://files.slack.com/upload/v1/test")
    await client.api_call("chat.delete", json={"ts": "late-post"})
    assert api_call.await_count == 2
    upload.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("storage", ("pending", "debounce"))
async def test_no_reply_discards_queued_requester_work(native, storage):
    adapter, client, attachment = native
    context, release, owner = await start_turn(adapter)
    adapter._busy_text_mode = "queue"
    adapter._busy_text_debounce_seconds = 60 if storage == "debounce" else 0
    adapter._busy_text_debounce_max_seconds = 60 if storage == "debounce" else 0
    seen = AsyncMock(return_value="queued result")
    adapter.set_message_handler(seen)
    try:
        await adapter._handle_slack_message({
            "type": "message", "channel": "C1", "team": "T1", "user": "UHUMAN",
            "ts": "121.000001", "thread_ts": META["thread_id"], "text": "use the other file"})
        assert adapter._pending_messages or adapter._text_debounce_store()
        retained = list(adapter._admissions().values())
        client.reset_mock()
        await adapter._handle_slack_message({
            "type": "message", "channel": "C1", "team": "T1", "user": "UHUMAN",
            "ts": "122.000001", "thread_ts": META["thread_id"], "text": "no reply needed"})
        assert owner.done()
        assert not adapter._pending_messages and not adapter._text_debounce_store()
        assert not adapter._admissions()
        for event in retained:
            await adapter._process_message_background(event, adapter._event_session_key(event))
        seen.assert_not_awaited()
        result = await context.run(asyncio.create_task, output(adapter, "final", attachment))
        assert not result.success and not client.mock_calls
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [
    {"user": "UOTHER"}, {"authorized": False},
    {"text": "> <@UBOT> no reply needed"},
    {"text": "`<@UBOT> no reply needed`"},
    {"text": "```text\n<@UBOT> no reply needed"},
    {"text": "`<@UBOT> no reply needed"},
    {"text": "<@UBOT> no re`literal`ply needed"},
    {"ts": "110.000001"}, {"_hermes_force_process": True},
    {"subtype": "message_changed", "message": {
        "user": "UHUMAN", "text": "<@UBOT> no reply needed", "ts": "110.000001"}},
])
async def test_other_senders_and_untrusted_controls_cannot_cancel(native, control):
    adapter, client, attachment = native
    context, release, owner = await start_turn(adapter)
    event = {"type": "message", "channel": "C1", "team": "T1", "user": "UHUMAN",
             "ts": "130.000001", "thread_ts": META["thread_id"], "text": "<@UBOT> no reply needed"}
    control = dict(control)
    if not control.pop("authorized", True):
        adapter._authorization_check = lambda *args, **kwargs: False
    event.update(control)
    try:
        await adapter._handle_slack_message(event)
        assert not owner.done()
        # An unauthorized control cannot cancel; restore output authorization
        # separately because every later use must consult the live callback.
        adapter._authorization_check = lambda *args, **kwargs: True
        client.reset_mock()
        result = await context.run(asyncio.create_task, output(adapter, "final", attachment))
        assert result.success
        client.chat_postMessage.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        await adapter.cancel_background_tasks()


async def _race_ingress(adapter, text, ts, user="UHUMAN"):
    await adapter._handle_slack_message({
        "type": "message", "channel": "C1", "team": "T1", "user": user,
        "ts": ts, "thread_ts": META["thread_id"], "text": text,
    })


@pytest.mark.asyncio
async def test_no_reply_preserves_other_requester_shared_fifo(native):
    adapter, _, _ = native
    _, release, owner = await start_turn(adapter)
    seen, drained = [], asyncio.Event()

    async def queued_handler(event):
        seen.append((event.source.user_id, event.text))
        if len(seen) == 2:
            drained.set()

    adapter.set_message_handler(queued_handler)
    try:
        await _race_ingress(adapter, "use the first file", "121.000001")
        await _race_ingress(adapter, "<@UBOT> other first task", "122.000001", "UOTHER")
        await _race_ingress(adapter, "<@UBOT> other second task", "123.000001", "UOTHER")
        await _race_ingress(adapter, "<@UBOT> no reply needed", "124.000001")
        assert owner.done()
        await asyncio.wait_for(drained.wait(), timeout=2)
        while adapter._background_tasks:
            await asyncio.wait_for(asyncio.gather(*adapter._background_tasks, return_exceptions=True), timeout=2)
        assert seen == [("UOTHER", "other first task"), ("UOTHER", "other second task")]
        assert not adapter._pending_messages and not adapter._slack_followup_queues
    finally:
        release.set()
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_no_reply_preserves_other_requester_live_session(native):
    adapter, client, attachment = native
    adapter.config.extra["thread_sessions_per_user"] = True
    _, release, owner = await start_turn(adapter)
    other_entered, other_release = asyncio.Event(), asyncio.Event()
    captured = []

    async def other_handler(event):
        captured.append((copy_context(), asyncio.current_task()))
        other_entered.set()
        await other_release.wait()

    adapter.set_message_handler(other_handler)
    try:
        await _race_ingress(adapter, "<@UBOT> other task", "121.000001", "UOTHER")
        await asyncio.wait_for(other_entered.wait(), timeout=2)
        assert len(adapter._session_tasks) == 2
        await _race_ingress(adapter, "<@UBOT> no reply needed", "122.000001")
        assert owner.done() and not captured[0][1].done()
        client.reset_mock()
        result = await captured[0][0].run(asyncio.create_task, output(adapter, "final", attachment))
        assert result.success
        client.chat_postMessage.assert_awaited_once()
    finally:
        release.set()
        other_release.set()
        await asyncio.gather(owner, *(task for _, task in captured), return_exceptions=True)
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_fresh_request_waits_for_cancellation_then_runs(native):
    adapter, client, attachment = native
    _, release, owner = await start_turn(adapter)
    cancel_entered, cancel_release = asyncio.Event(), asyncio.Event()
    ingress_entered, fresh_entered, fresh_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    fresh_context = []

    async def cancel_handler(session_key, source):
        cancel_entered.set()
        await cancel_release.wait()

    async def fresh_handler(event):
        assert event.text == "fresh task"
        fresh_context.append(copy_context())
        fresh_entered.set()
        await fresh_release.wait()

    original_handle = adapter.handle_message

    async def observe_ingress(event):
        if event.message_id == "130.000001":
            ingress_entered.set()
        await original_handle(event)

    adapter.set_session_cancellation_handler(cancel_handler)
    adapter.set_message_handler(fresh_handler)
    adapter.handle_message = observe_ingress
    cancel = asyncio.create_task(_race_ingress(adapter, "<@UBOT> no reply needed", "122.000001"))
    fresh = None
    try:
        await asyncio.wait_for(cancel_entered.wait(), timeout=2)
        fresh = asyncio.create_task(_race_ingress(adapter, "<@UBOT> fresh task", "130.000001"))
        await asyncio.wait_for(ingress_entered.wait(), timeout=2)
        assert not fresh.done() and not fresh_entered.is_set()
        cancel_release.set()
        await asyncio.wait_for(asyncio.gather(cancel, fresh), timeout=2)
        await asyncio.wait_for(fresh_entered.wait(), timeout=2)
        assert owner.done()
        client.reset_mock()
        result = await fresh_context[0].run(asyncio.create_task, output(adapter, "final", attachment))
        assert result.success
        client.chat_postMessage.assert_awaited_once()
    finally:
        cancel_release.set()
        release.set()
        fresh_release.set()
        await asyncio.gather(cancel, *([fresh] if fresh else []), return_exceptions=True)
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_no_reply_rejects_old_request_finishing_enrichment(native):
    adapter, client, _ = native
    _, release, owner = await start_turn(adapter)
    enriching, finish_enrichment = asyncio.Event(), asyncio.Event()

    async def hydrate(**kwargs):
        enriching.set()
        await finish_enrichment.wait()
        return None, [], []

    adapter._hydrate_thread_context = hydrate
    handler = AsyncMock(return_value="stale result")
    adapter.set_message_handler(handler)
    stale = asyncio.create_task(_race_ingress(adapter, "<@UBOT> older queued task", "121.000001"))
    try:
        await asyncio.wait_for(enriching.wait(), timeout=2)
        await _race_ingress(adapter, "<@UBOT> no reply needed", "122.000001")
        assert owner.done()
        client.reset_mock()
        finish_enrichment.set()
        await asyncio.wait_for(stale, timeout=2)
        while adapter._background_tasks:
            await asyncio.wait_for(asyncio.gather(*adapter._background_tasks, return_exceptions=True), timeout=2)
        handler.assert_not_awaited()
        assert not client.mock_calls
        assert not adapter._pending_messages and not adapter._admissions()
    finally:
        finish_enrichment.set()
        release.set()
        await asyncio.gather(stale, owner, return_exceptions=True)
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", OUTPUTS)
@pytest.mark.parametrize("channel", ("CFOREIGN", "DFOREIGN"))
async def test_inherited_task_context_cannot_write_foreign_channel(native, kind, channel):
    adapter, client, attachment = native
    context, release, owner = await start_turn(adapter)
    try:
        client.reset_mock()
        await context.run(asyncio.create_task, output(
            adapter, kind, attachment, channel=channel))
        assert not client.mock_calls
        # Model-controlled metadata cannot mint destination authority.
        await context.run(asyncio.create_task, output(
            adapter, "final", attachment, channel=channel,
            metadata={**META, "job_id": "forged", "authorized_destination": True}))
        assert not client.mock_calls
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", [
    {"team_id": "TFOREIGN"}, {"thread_id": "999.000001"},
])
@pytest.mark.parametrize("kind", ("final", "edit", "media"))
async def test_inherited_task_context_binds_team_and_thread(native, destination, kind):
    adapter, client, attachment = native
    context, release, owner = await start_turn(adapter)
    try:
        if kind == "edit" and "thread_id" in destination:
            # An edit's ts, not caller-supplied thread metadata, fixes its surface.
            client.chat_postMessage.return_value = {"ok": True, "ts": "900.000001"}
            await adapter.send("C1", "other thread", metadata={
                **META, **destination, "job_id": "approved-other-thread"})
            client.reset_mock()
            await context.run(asyncio.create_task, adapter.edit_message(
                "C1", "900.000001", "misdirected", metadata=META))
        else:
            client.reset_mock()
            await context.run(asyncio.create_task, output(
                adapter, kind, attachment, metadata={**META, **destination}))
        assert not client.mock_calls
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", OUTPUTS)
@pytest.mark.parametrize("authorization", ("deny", "exception"))
async def test_live_authorization_is_rechecked_for_every_output(native, kind, authorization):
    adapter, client, attachment = native
    context, release, owner = await start_turn(adapter)
    source = adapter.build_source(
        chat_id="C1", chat_type="group", user_id="UHUMAN",
        thread_id=META["thread_id"], scope_id="T1")
    checked = []

    def current_authorization(*args, **kwargs):
        checked.append((args, kwargs))
        if authorization == "exception":
            raise RuntimeError("authorization temporarily unavailable")
        return False

    try:
        adapter._authorization_check = current_authorization
        client.reset_mock()
        await context.run(asyncio.create_task, output(adapter, kind, attachment))
        assert checked
        assert not client.mock_calls
        assert not context.run(adapter.authorize_turn, source)
        assert not owner.done()
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_live_authorization_exception_cannot_fall_back_to_allow_all(native, monkeypatch):
    adapter, client, attachment = native
    context, release, owner = await start_turn(adapter)

    def unavailable(*args, **kwargs):
        raise RuntimeError("authorization temporarily unavailable")

    try:
        monkeypatch.setenv("SLACK_ALLOW_ALL_USERS", "true")
        adapter._authorization_check = unavailable
        client.reset_mock()
        result = await context.run(asyncio.create_task, output(adapter, "final", attachment))
        assert not result.success
        assert not client.mock_calls
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ("send", "handoff", "upload"))
async def test_thread_task_cannot_create_channel_root_output(native, path):
    adapter, client, attachment = native
    context, release, owner = await start_turn(adapter)
    try:
        client.reset_mock()
        if path == "handoff":
            result = await context.run(asyncio.create_task, adapter.create_handoff_thread("C1", "task"))
            assert result is None
        elif path == "upload":
            result = await context.run(asyncio.create_task, adapter.send_document(
                "C1", str(attachment), metadata={"team_id": "T1"}))
            assert not result.success
        else:
            result = await context.run(asyncio.create_task, adapter.send(
                "C1", "root message", metadata={"team_id": "T1"}))
            assert not result.success
        assert not client.mock_calls
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,thread", [("CFOREIGN", "100.000001"), ("C1", None)])
async def test_captured_adapter_sdk_checks_actual_post_destination(native, monkeypatch, channel, thread):
    from plugins.platforms.slack.participation import ParticipationDenied

    adapter, _, _ = native
    context, release, owner = await start_turn(adapter)
    requests = []

    async def transport(*, api_url, req_args, **kwargs):
        requests.append((api_url, req_args))
        return {"status_code": 200, "headers": {}, "data": {"ok": True, "ts": "200.000001"}}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    client = adapter._new_web_client("fake", None)
    adapter._team_clients["T1"] = client
    try:
        with pytest.raises(ParticipationDenied):
            await context.run(asyncio.create_task, client.chat_postMessage(
                channel=channel, thread_ts=thread, text="misdirected"))
        assert not requests
        await context.run(asyncio.create_task, client.chat_postMessage(
            channel="C1", thread_ts=META["thread_id"], text="allowed"))
        assert len(requests) == 1
        # Reading other context does not acquire write permission there.
        await context.run(asyncio.create_task, client.conversations_replies(
            channel="CFOREIGN", ts="999.000001"))
        assert len(requests) == 2
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("edit", "reaction"))
async def test_thread_task_rejects_unknown_message_destination(native, operation):
    adapter, client, _ = native
    context, release, owner = await start_turn(adapter)
    try:
        client.reset_mock()
        if operation == "edit":
            result = await context.run(asyncio.create_task, adapter.edit_message(
                "C1", "999.000001", "unknown thread", metadata=META))
            assert not result.success
        else:
            await context.run(asyncio.create_task, adapter._add_reaction(
                "C1", "999.000001", "eyes", "T1"))
        assert not client.mock_calls
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated_team", (None, "TFOREIGN"))
async def test_sdk_client_identity_cannot_inherit_request_workspace(native, monkeypatch, authenticated_team):
    from plugins.platforms.slack.participation import ParticipationDenied

    adapter, _, _ = native
    context, release, owner = await start_turn(adapter)
    requests = []

    async def transport(*, api_url, **kwargs):
        requests.append(api_url)
        return {"status_code": 200, "headers": {}, "data": {
            "ok": True, "team_id": authenticated_team, "user_id": "UBOT", "ts": "200.000001"}}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    try:
        if authenticated_team:
            await adapter._authenticate_workspace("foreign", None)
            client = adapter._team_clients[authenticated_team]
        else:
            client = adapter._new_web_client("unverified", None)
        requests.clear()
        with pytest.raises(ParticipationDenied):
            await context.run(asyncio.create_task, client.chat_postMessage(
                channel="C1", thread_ts=META["thread_id"], text="wrong workspace"))
        assert not requests
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("full_upload", "bytes"))
async def test_foreign_workspace_sdk_upload_cannot_send_bytes(native, monkeypatch, stage):
    from plugins.platforms.slack.participation import ParticipationDenied

    adapter, _, attachment = native
    context, release, owner = await start_turn(adapter)
    requests = []

    async def transport(*, api_url, req_args, **kwargs):
        requests.append((api_url, req_args))
        if api_url.endswith("auth.test"):
            data = {"ok": True, "team_id": "TFOREIGN", "user_id": "UBOT", "bot_id": "B1"}
        elif api_url.endswith("files.getUploadURLExternal"):
            data = {"ok": True, "file_id": "F1", "upload_url": "https://files.slack.com/upload/v1/test"}
        elif api_url == "https://files.slack.com/upload/v1/test":
            return {"status_code": 200, "body": ""}
        else:
            data = {"ok": True, "files": [{"id": "F1"}]}
        return {"status_code": 200, "headers": {}, "data": data}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    await adapter._authenticate_workspace("foreign", None)
    client = adapter._team_clients["TFOREIGN"]
    requests.clear()
    try:
        with pytest.raises(ParticipationDenied):
            if stage == "full_upload":
                await context.run(asyncio.create_task, client.files_upload_v2(
                    channel="C1", thread_ts=META["thread_id"], file=str(attachment)))
            else:
                await context.run(asyncio.create_task, client._upload_file(
                    url="https://files.slack.com/upload/v1/test", data=b"result",
                    logger=logging.getLogger(__name__), timeout=30, proxy=None, ssl=None))
        assert requests == [], "foreign workspace credentials must not receive even the upload request"
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ("DUNKNOWN", "UUNKNOWN", "C1"))
async def test_registered_primary_upload_resolves_unspecified_workspace(native, monkeypatch, target):
    adapter, _, attachment = native
    publications = []

    async def transport(*, api_url, req_args, **kwargs):
        method = api_url.rsplit("/", 1)[-1]
        if method == "conversations.open":
            data = {"ok": True, "channel": {"id": "DRESOLVED"}}
        elif method == "files.getUploadURLExternal":
            data = {"ok": True, "file_id": "F1", "upload_url": "https://files.slack.com/upload/v1/test"}
        elif api_url == "https://files.slack.com/upload/v1/test":
            return {"status_code": 200, "body": ""}
        else:
            publications.append(req_args.get("json") or req_args.get("data") or req_args.get("params"))
            data = {"ok": True, "files": [{"id": "F1"}]}
        return {"status_code": 200, "headers": {}, "data": data}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    client = adapter._new_web_client("primary", None)
    adapter._team_clients["T1"] = client
    adapter._get_client = MagicMock(return_value=client)
    metadata = {"job_id": "approved-job"} if target == "C1" else None
    result = await adapter.send_document(target, str(attachment), metadata=metadata)
    assert result.success
    assert len(publications) == 1
    assert publications[0]["channel_id"] == ("DRESOLVED" if target == "UUNKNOWN" else target)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("upload", "text"))
async def test_primary_upload_keeps_resolved_workspace_epoch_through_resummon(native, monkeypatch, kind):
    adapter, _, attachment = native
    entered, release = asyncio.Event(), asyncio.Event()
    uploaded, cleaned = [], []

    async def transport(*, api_url, req_args, **kwargs):
        if api_url.endswith("chat.postMessage"):
            entered.set()
            await release.wait()
            data = {"ok": True, "ts": "200.000001"}
        elif api_url.endswith("chat.delete"):
            cleaned.append(req_args)
            data = {"ok": True}
        elif api_url.endswith("files.getUploadURLExternal"):
            entered.set()
            await release.wait()
            data = {"ok": True, "file_id": "F1", "upload_url": "https://files.slack.com/upload/v1/test"}
        elif api_url == "https://files.slack.com/upload/v1/test":
            uploaded.append(req_args["data"])
            return {"status_code": 200, "body": ""}
        else:
            data = {"ok": True, "files": [{"id": "F1"}]}
        return {"status_code": 200, "headers": {}, "data": data}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    client = adapter._new_web_client("primary", None)
    adapter._team_clients["T1"] = client
    adapter._get_client = MagicMock(return_value=client)
    metadata = {"thread_id": META["thread_id"], "job_id": "approved-job"}
    operation = (adapter.send_document("C1", str(attachment), metadata=metadata) if kind == "upload"
                 else adapter.send("C1", "old result", metadata=metadata))
    pending = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await adapter._handle_slack_message({
            "type": "message", "subtype": "message_deleted", "channel": "C1", "team": "T1",
            "deleted_ts": "200.000001", "event_ts": "300.000001",
            "previous_message": {"user": "UBOT", "ts": "200.000001", "thread_ts": META["thread_id"]},
        })
        handler = AsyncMock(return_value=None)
        adapter.set_message_handler(handler)
        await _race_ingress(adapter, "<@UBOT> fresh task", "400.000001")
        await asyncio.gather(*adapter._background_tasks)
        handler.assert_awaited_once()
        release.set()
        result = await asyncio.wait_for(pending, 2)
        assert not result.success
        assert not uploaded
        if kind == "text":
            assert len(cleaned) == 1
            assert adapter._deletion_messages()[("T1", "C1", "200.000001")][0] == "T1"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_sdk_plural_upload_destinations_cannot_bypass_surface_binding(native, monkeypatch):
    from plugins.platforms.slack.participation import ParticipationDenied

    adapter, _, _ = native
    context, release, owner = await start_turn(adapter)
    requests = []

    async def transport(*, api_url, **kwargs):
        requests.append(api_url)
        return {"status_code": 200, "headers": {}, "data": {"ok": True}}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    client = adapter._new_web_client("primary", None)
    adapter._team_clients["T1"] = client
    try:
        with pytest.raises(ParticipationDenied):
            await context.run(asyncio.create_task, client.files_completeUploadExternal(
                files=[{"id": "F1"}], channel_id="C1", channels=["CFOREIGN"],
                thread_ts=META["thread_id"]))
        assert not requests
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_explicit_cron_workspace_cannot_fall_back_to_other_client(native, monkeypatch):
    adapter, _, _ = native
    requests = []

    async def transport(*, api_url, **kwargs):
        requests.append(api_url)
        return {"status_code": 200, "headers": {}, "data": {"ok": True, "ts": "200.000001"}}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    client = adapter._new_web_client("primary", None)
    adapter._team_clients["T1"] = client
    adapter._get_client = MagicMock(return_value=client)
    result = await adapter.send("C1", "wrong workspace", metadata={
        "team_id": "TFOREIGN", "job_id": "approved-job"})
    assert not result.success
    assert not requests
