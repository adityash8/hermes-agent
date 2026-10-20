"""Authoritative settlement blocks autonomous replies even from a non-silent model."""

import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry
from tests.gateway.test_background_process_notifications import _build_runner
from tests.gateway.test_slack_response_necessity import native  # noqa: F401
from tools.process_registry import ProcessRegistry, ProcessSession
from tools.process_registry_notifications import format_process_notification


@pytest.fixture
def delivery(monkeypatch, tmp_path, native):
    import tools.process_registry as processes

    monkeypatch.setattr(processes, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = ProcessRegistry()
    monkeypatch.setattr(processes, "process_registry", registry)
    runner = _build_runner(monkeypatch, tmp_path, "all")
    monkeypatch.setattr(runner.config, "get_home_channel", lambda *args: "test-home")
    adapter, client = native
    runner.adapters[Platform.SLACK] = adapter
    received = []

    async def model(message, **kwargs):
        received.append(message)
        if "OLD-RESULT" not in message:
            assert "OLD-RESULT" not in str(kwargs.get("persist_user_message"))
        # Deliberately ignores NO_REPLY instructions; uses the real outward path.
        return {"final_response": "NON-SILENT MODEL: " + message, "messages": []}

    monkeypatch.setattr(runner, "_run_agent", model)
    monkeypatch.setattr(runner, "_should_send_voice_reply", lambda *args, **kwargs: False)
    adapter.set_message_handler(runner._handle_message)
    return runner, registry, adapter, client, received


def process_event(registry, sid, output, exit_code=1):
    session = ProcessSession(id=sid, command="test worker", started_at=time.time(),
                             session_key="agent:main:slack:dm:D1",
                             notify_on_complete=True, output_buffer=output)
    registry._running[sid] = session
    registry._finish_exited(session, exit_code)  # Real producer, including its queued event.
    return registry.completion_queue.get_nowait()


def delegation_event(processes, delegation_id, report):
    from tools import async_delegation as delegations
    record = {"delegation_id": delegation_id, "goal": "requested report",
              "session_key": "agent:main:slack:dm:D1",
              "dispatched_at": time.time()}
    delegations._persist_dispatch(record)
    delegations._push_completion_event(record, {"summary": report}, "completed")
    return processes.completion_queue.get_nowait()


async def drain(adapter):
    while adapter._background_tasks:
        await asyncio.gather(*list(adapter._background_tasks))


async def deliver(runner, evt, refused):
    """Deliver a completion; a busy session refuses the wake (False = retryable)."""
    if await runner._deliver_completion_notification(format_process_notification(evt), evt) is False:
        refused.append(evt)


async def redeliver_refused(runner, adapter, refused):
    """Retry refused wakes after each drain, as the completion drain loop requeues them."""
    for _attempt in range(5):
        await drain(adapter)
        if not refused:
            return
        retry, refused[:] = list(refused), []
        for evt in retry:
            await deliver(runner, evt, refused)
    raise AssertionError(f"wakes still refused: {[evt.get('session_id') for evt in refused]}")


@pytest.mark.asyncio
@pytest.mark.parametrize("human_first", [False, True])
@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("rewrite", [False, True])
async def test_mixed_pending_preserves_human_and_filters_structured_results(delivery, monkeypatch, human_first, fresh, rewrite):
    runner, processes, adapter, client, received = delivery
    old = process_event(processes, "proc_old", "OLD-RESULT")
    source = runner._build_process_event_source(old)
    human = MessageEvent(text="Please deliver the comparison I requested.",
                         source=replace(source, user_id="UHUMAN"), message_id="301.1",
                         metadata={"gateway_session_id": "human-parent", "human_marker": True},
                         reply_to_message_id="300.1", allow_gateway_control=False)
    human._client_context_required = False
    original = {field: value for field, value in vars(human).items() if field != "_pending_completion_entries"}
    key = adapter._event_session_key(human)
    runner.session_store._entries[key] = SessionEntry(
        session_key=key, session_id="human-parent", created_at=datetime.now(),
        updated_at=datetime.now(), platform=Platform.SLACK, chat_type="dm", origin=human.source)
    await runner._session_db.create_session("human-parent", source="slack")
    monkeypatch.setattr(runner, "_is_user_authorized_for_source", lambda *args: True)
    adapter._active_sessions[key] = asyncio.Event()
    adapter._session_tasks[key] = asyncio.current_task()
    refused = []
    try:
        if human_first:
            await adapter.handle_message(human)
        await deliver(runner, old, refused)
        if fresh:
            new = process_event(processes, "proc_new", "FRESH-BLOCKER")
            report = delegation_event(processes, "deleg_report", "FRESH-REQUESTED-REPORT")
            for evt in (new, report):
                await deliver(runner, evt, refused)
        if not human_first:
            await adapter.handle_message(human)
        processes.wait("proc_old", timeout=1)
        pending = adapter._pending_messages.pop(key)
        assert pending is human
        assert all(getattr(pending, field) == value for field, value in original.items())
        if rewrite:
            import hermes_cli.lifecycle

            async def rewrite_human(name, **kwargs):
                if name == "pre_gateway_dispatch":
                    return [{"action": "rewrite", "text": "REWRITTEN-REQUEST"}]
                return []

            monkeypatch.setattr(hermes_cli.lifecycle, "ainvoke_hook", rewrite_human)
        adapter._session_tasks.pop(key)
        adapter._release_session_guard(key)
        adapter._start_session_processing(pending, key)
        await redeliver_refused(runner, adapter, refused)
        assert "OLD-RESULT" not in "\n".join(received)
        assert ("REWRITTEN-REQUEST" if rewrite else human.text) in received[0]
        # A refused wake is never folded into the human turn; it takes its own later turn.
        for text in ("FRESH-BLOCKER", "FRESH-REQUESTED-REPORT"):
            assert text not in received[0]
            assert (text in "\n".join(received[1:])) is fresh
        assert len(received) > 1 if fresh else len(received) == 1
        assert all(getattr(pending, field) == value for field, value in original.items())
        assert client.chat_postMessage.await_count == len(received)
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["ingress", "intercepts", "agent_start"])
@pytest.mark.parametrize("kind", ["completion", "async_delegation", "human"])
async def test_final_admission_rechecks_after_await(delivery, monkeypatch, stage, kind):
    from gateway import client_context
    from tools import async_delegation

    runner, processes, adapter, client, received = delivery
    evt = (delegation_event(processes, "deleg_late", "FIRST-REQUESTED-REPORT")
           if kind == "async_delegation" else process_event(processes, "proc_late", "OLD-RESULT"))
    entered = asyncio.Event()

    async def settle():
        await asyncio.sleep(0)
        if kind == "async_delegation":
            async_delegation.mark_completion_delivered(evt["delegation_id"])
        else:
            processes.wait("proc_late", timeout=1)
        entered.set()

    if stage == "ingress":
        original = client_context.handle_ingress

        async def ingress(*args):
            await settle()
            return await original(*args)

        monkeypatch.setattr(client_context, "handle_ingress", ingress)
    elif stage == "intercepts":
        async def intercept(*args):
            await settle()
            return None

        monkeypatch.setattr(runner, "_hm_pending_reply_intercepts", intercept)
    else:
        original = runner.hooks.emit

        async def emit(name, *args, **kwargs):
            if name == "agent:start":
                await settle()
            return await original(name, *args, **kwargs)

        monkeypatch.setattr(runner.hooks, "emit", emit)
    try:
        if kind == "human":
            monkeypatch.setattr(runner, "_is_user_authorized_for_source", lambda *args: True)
            await adapter.handle_message(MessageEvent(text="HUMAN-REQUEST", source=runner._build_process_event_source(evt)))
        else:
            await runner._deliver_completion_notification(format_process_notification(evt), evt)
        await drain(adapter)
        assert entered.is_set()
        assert len(received) == (0 if kind == "completion" else 1)
        if kind != "completion":
            assert ("HUMAN-REQUEST" if kind == "human" else "FIRST-REQUESTED-REPORT") in received[0]
        else:
            client.chat_postMessage.assert_not_awaited()
        assert not runner._is_session_running(runner._session_key_for_source(runner._build_process_event_source(evt)))
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.parametrize("epoch", [None, "missing", "different", "matching"])
def test_consumption_requires_matching_process_incarnation(delivery, epoch):
    runner, processes, _adapter, _client, _received = delivery
    evt = process_event(processes, "proc_epoch", "RESULT")
    processes.wait("proc_epoch", timeout=1)
    if epoch == "missing":
        evt.pop("started_at")
    elif epoch == "different":
        evt["started_at"] += 1
    elif epoch is None:
        evt["started_at"] = None
    assert runner._completion_is_settled(evt, accepted=True) is (epoch == "matching")


@pytest.mark.asyncio
async def test_settled_completion_stays_silent_on_event_reentry(delivery):
    from gateway.platforms.base import merge_pending_message_event

    runner, processes, _adapter, client, received = delivery
    evt = process_event(processes, "proc_reentry", "OLD-RESULT")
    event = MessageEvent(text=format_process_notification(evt), internal=True,
                         source=runner._build_process_event_source(evt),
                         metadata={"_completion_entries": [(format_process_notification(evt), evt)]})
    processes.wait("proc_reentry", timeout=1)
    assert await runner._handle_message(event) is None
    assert await runner._handle_message(event) is None
    assert received == []
    client.chat_postMessage.assert_not_awaited()
    human = MessageEvent(text="HUMAN-REQUEST", source=event.source)
    for first, second in ((event, human), (human, event)):
        pending = {"session": first}
        merge_pending_message_event(pending, "session", second, merge_text=True)
        assert pending["session"] is human and human.text == "HUMAN-REQUEST"
        assert await runner._admit_completion_message(human, human.text) == human.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["completion", "batch", "human", "delegation"])
async def test_queued_followup_final_admission(delivery, monkeypatch, kind):
    from tools import async_delegation

    runner, processes, adapter, _client, received = delivery
    old = process_event(processes, "proc_followup", "OLD-RESULT")
    entries = [(format_process_notification(old), old)]
    if kind == "batch":
        fresh = process_event(processes, "proc_sibling", "FRESH-BLOCKER")
        entries.append((format_process_notification(fresh), fresh))
    elif kind == "delegation":
        fresh = delegation_event(processes, "deleg_followup", "FIRST-REQUESTED-REPORT")
        entries = [(format_process_notification(fresh), fresh)]
    source = runner._build_process_event_source(old)
    event = MessageEvent(text="HUMAN-REQUEST" if kind == "human" else runner._completion_entries_text(entries),
                         source=source, internal=kind != "human")
    if kind == "human":
        event._pending_completion_entries = entries
    else:
        event.metadata["_completion_entries"] = entries
    key = runner._session_key_for_source(source)
    result = {"final_response": "prior answer", "messages": [], "interrupted": True}
    context = SimpleNamespace(source=source, session_id="parent", session_key=key, run_generation=1,
                              _interrupt_depth=0, history=[], _status_thread_metadata={},
                              result_holder=[result], context_prompt="existing context")

    async def consume_during_last_await(*args):
        await asyncio.sleep(0)
        processes.wait("proc_followup", timeout=1)
        if kind == "delegation":
            async_delegation.mark_completion_delivered(fresh["delegation_id"])

    monkeypatch.setattr(runner, "_refresh_agent_cache_message_count", consume_during_last_await)
    outcome = await runner._run_agent_queued_followup(context, adapter, event.text, event,
                                                    result["final_response"], result, None)
    if kind == "completion":
        assert received == []
        assert outcome is result
    else:
        assert len(received) == 1 and "OLD-RESULT" not in received[0]
        expected = {"batch": "FRESH-BLOCKER", "human": "HUMAN-REQUEST", "delegation": "FIRST-REQUESTED-REPORT"}
        assert expected[kind] in received[0]
    assert event._completion_entries == ([] if kind in {"completion", "human"} else entries[-1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["wait", "log", "kill", "poll", "partial_log", "none"])
@pytest.mark.parametrize("stage", ["singleton", "batch", "routing", "queued", "pending_pair", "pending_user", "pending_mixed", "pending_scoped"])
async def test_process_settlement_silences_only_consumed_results(delivery, monkeypatch, consumer, stage):
    from tools.registry import registry as tools

    runner, processes, adapter, client, received = delivery
    evt = process_event(processes, "proc_aabbccdd", "OLD-RESULT\nterminal failure")
    settled = consumer in {"wait", "log", "kill"}

    def consume():
        if consumer == "none":
            return
        args = {"action": "log" if consumer == "partial_log" else consumer,
                "session_id": "aabb", "timeout": 1}
        if consumer == "partial_log":
            args.update(offset=0, limit=1)
        result = json.loads(tools.dispatch("process_manage", args))
        assert "error" not in result

    if stage == "routing":
        original = runner._build_process_event_source

        def resolve(event):
            consume()
            return original(event)

        monkeypatch.setattr(runner, "_build_process_event_source", resolve)
    elif stage == "queued":
        original_hook = adapter._run_processing_hook

        async def hook(name, event, *args):
            if name == "on_processing_start":
                consume()
            return await original_hook(name, event, *args)

        monkeypatch.setattr(adapter, "_run_processing_hook", hook)
    elif not stage.startswith("pending_"):
        consume()

    refused = []
    try:
        if stage.startswith("pending_"):
            from dataclasses import replace
            from gateway.platforms.base import MessageEvent

            source = runner._build_process_event_source(evt)
            key = adapter._event_session_key(MessageEvent(text="", source=source))
            # Occupied adapter slot: exercise actual handle_message/merge/drain,
            # including the fallback used when the busy handler declines merging.
            adapter._active_sessions[key] = asyncio.Event()
            adapter._session_tasks[key] = asyncio.current_task()
            await deliver(runner, evt, refused)
            fresh = process_event(processes, "proc_eeff1122", "FRESH-BLOCKER", exit_code=0)
            await deliver(runner, fresh, refused)
            if stage == "pending_mixed":
                delegated = delegation_event(processes, "deleg_mixed", "FRESH-DELEGATION-REPORT")
                await deliver(runner, delegated, refused)
            if stage in {"pending_user", "pending_scoped"}:
                human = MessageEvent(text="Please deliver the comparison I requested.",
                                     source=replace(source, user_id="UHUMAN"), message_id="301.1")
                if stage == "pending_scoped":
                    human._client_context_required = True
                monkeypatch.setattr(runner, "_is_user_authorized_for_source", lambda *args: True)
                await adapter.handle_message(human)
            consume()
            pending = adapter._pending_messages.pop(key)
            if stage in {"pending_user", "pending_scoped"}:
                assert pending is human
                assert not pending.internal and pending.source.user_id == "UHUMAN"
            adapter._session_tasks.pop(key)
            adapter._release_session_guard(key)
            adapter._start_session_processing(pending, key)
        elif stage == "batch":
            fresh = process_event(processes, "proc_eeff1122", "FRESH-BLOCKER")
            await asyncio.gather(*(
                runner._enqueue_process_completion_notification(format_process_notification(e), e)
                for e in (evt, fresh)))
        else:
            await runner._deliver_completion_notification(format_process_notification(evt), evt)
        await drain(adapter)
        if stage != "pending_scoped":
            # The scoped case checks only the user's own queued turn; refused wakes are not part of it.
            await redeliver_refused(runner, adapter, refused)
        if stage == "pending_scoped":
            # No scoped host is available: the original user's trusted marker
            # must survive merging and forbid a legacy fallback/content leak.
            assert not received
            for call in client.chat_postMessage.await_args_list:
                for private in ("OLD-RESULT", "FRESH-BLOCKER", "comparison I requested"):
                    assert private not in call.kwargs.get("text", "")
            return
        text = "\n".join(received)
        sent = "\n".join(c.kwargs.get("text", "") for c in client.chat_postMessage.await_args_list)
        # A real request merged into the queue owns its response; its completion
        # context must not cause the request to be suppressed or rewritten.
        expected_old = not settled
        assert ("OLD-RESULT" in text) is expected_old
        assert ("OLD-RESULT" in sent) is expected_old
        if stage == "pending_user":
            assert "Please deliver the comparison I requested." in sent
        if stage == "pending_mixed":
            assert "FRESH-DELEGATION-REPORT" in sent
        if stage in {"batch", "pending_pair", "pending_user", "pending_mixed"}:
            assert "FRESH-BLOCKER" in text and "FRESH-BLOCKER" in sent
        elif settled:
            client.chat_postMessage.assert_not_awaited()
        else:
            client.chat_postMessage.assert_awaited_once()
        # Poll is observed, never accepted output consumption.
        assert processes.is_completion_consumed(evt["session_id"]) is settled
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement", ["pending", "delivered", "dropped"])
@pytest.mark.parametrize("entry", ["direct", "group", "routing"])
async def test_delegation_settlement_preserves_first_accepted_report(delivery, monkeypatch, settlement, entry):
    from tools import async_delegation as delegations

    runner, processes, adapter, client, received = delivery

    evt = delegation_event(processes, "deleg_settled", "OLD-REPORT")

    def settle():
        if settlement == "delivered":
            delegations.mark_completion_delivered(evt["delegation_id"])
        elif settlement == "dropped":
            claim = delegations.claim_event_delivery(evt, "superseded-parent")
            if claim:
                delegations.drop_completion_delivery(evt["delegation_id"], claim)

    if entry == "routing":
        original = runner._build_process_event_source

        def resolve(event):
            # Settle through the consuming API while routing is in progress.
            if settlement == "dropped":
                # The dispatch currently owns the claim; use the authoritative
                # terminal target path separately in test_completion_session_boundary.
                delegations.mark_completion_delivered(evt["delegation_id"])
            else:
                settle()
            return original(event)

        monkeypatch.setattr(runner, "_build_process_event_source", resolve)
    else:
        settle()
    try:
        if entry == "group":
            fresh = delegation_event(processes, "deleg_fresh", "FRESH-REQUESTED-REPORT")
            await runner._deliver_async_delegation_group([evt, fresh])
        elif entry == "direct":
            # Public injection seam must not bypass the same authoritative gate.
            await runner._inject_watch_notification(format_process_notification(evt), evt)
        else:
            await runner._deliver_completion_notification(format_process_notification(evt), evt)
        await drain(adapter)
        text = "\n".join(received)
        sent = "\n".join(c.kwargs.get("text", "") for c in client.chat_postMessage.await_args_list)
        assert ("OLD-REPORT" in text) is (settlement == "pending")
        assert ("OLD-REPORT" in sent) is (settlement == "pending")
        if entry == "group":
            assert "FRESH-REQUESTED-REPORT" in sent
            assert delegations.get_durable_delegation("deleg_fresh")["delivery_state"] == "delivered"
        elif settlement != "pending":
            client.chat_postMessage.assert_not_awaited()
        else:
            client.chat_postMessage.assert_awaited_once()
    finally:
        await adapter.cancel_background_tasks()
