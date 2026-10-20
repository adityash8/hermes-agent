"""Native Slack addressing and requester ownership, through real adapter dispatch."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter
from tools import clarify_gateway


def native():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake"))
    # Explicit fixture defaults keep live operator routing out of native tests.
    adapter.config.extra.update({"reaction_triggers": False, "reaction_trigger_target": ""})
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
    return adapter, client


def message(text, user="UHUMAN", ts="120.000001", **fields):
    return {"type": "message", "channel": "C1", "team": "T1", "user": user,
            "client_msg_id": ts, "ts": ts, "thread_ts": "100.000001", "text": text, **fields}


async def drain(adapter):
    tasks = list(adapter._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["thanks", "FYI the deploy finished", "no reply needed",
    "<@UOTHER> can you review this?", "?", "> <@UBOT> quoted", "`<@UBOT>`", "```<@UBOT>```"])
async def test_historical_membership_never_starts_work(text):
    adapter, client = native()
    adapter._register_mentioned_thread("100.000001", team_id="T1")
    adapter._bot_message_ts.add("100.000001")
    adapter._has_active_session_for_thread = MagicMock(return_value=True)
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)
    await adapter._handle_slack_message(message(text))
    await drain(adapter)
    handler.assert_not_awaited()
    adapter._hydrate_thread_context.assert_not_awaited()
    assert not client.mock_calls


@pytest.mark.asyncio
async def test_request_completion_revokes_ingress_and_unknown_auth_fails_closed():
    adapter, _ = native()
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)
    await adapter._handle_slack_message(message("<@UBOT> start"))
    await drain(adapter)
    handler.assert_awaited_once()
    await adapter._handle_slack_message(message("continue", ts="121.000001"))
    await drain(adapter)
    handler.assert_awaited_once()
    adapter._authorization_check = lambda *args, **kwargs: False
    await adapter._handle_slack_message(message("<@UBOT> start", ts="122.000001"))
    await drain(adapter)
    handler.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra,thread,accepted", [
    ({"free_response_channels": ["C1"]}, "100.000001", True),
    ({"require_mention": False}, "100.000001", True),
    ({"free_response_channels": ["C2"]}, "100.000001", False),
    ({"free_response_channels": ["C1"], "require_mention_channels": ["C1"]}, "100.000001", False),
    ({"require_mention": False, "thread_require_mention": True}, "100.000001", False),
    ({"require_mention": False, "thread_require_mention": True}, None, True),
])
async def test_configured_free_response_admits_real_event_and_fences_completion(extra, thread, accepted):
    from contextvars import copy_context

    adapter, client = native()
    adapter.config.extra.update(extra)
    contexts = []

    async def handler(event):
        contexts.append(copy_context())
        return "result"

    adapter.set_message_handler(handler)
    await adapter._handle_slack_message(message("vpn is broken", thread_ts=thread))
    await drain(adapter)
    assert bool(contexts) is accepted
    if accepted:
        assert any(call.kwargs.get("text") == "result" for call in client.chat_postMessage.await_args_list)
        client.reset_mock()
        result = await contexts[0].run(asyncio.create_task, adapter.send(
            "C1", "stale", metadata={"team_id": "T1", "thread_id": thread or "120.000001"}))
        assert not result.success
    else:
        adapter._hydrate_thread_context.assert_not_awaited()
    assert not client.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,extra", [
    ("A", {}), ("okay", {}), ("FYI deploy finished", {}),
    ("A", {"strict_mention": True}), ("okay", {"strict_mention": True}),
    ("A", {"thread_require_mention": True}), ("okay", {"thread_require_mention": True}),
])
async def test_actual_pending_reply_is_owned_by_requester_and_resolved_state_is_not_pending(reply, extra):
    adapter, client = native()
    adapter.config.extra.update(extra)
    ready, done = asyncio.Event(), asyncio.Event()
    seen = []
    session_key = None

    async def handler(event):
        nonlocal session_key
        seen.append(event.source.user_id)
        if len(seen) == 1:
            session_key = adapter._event_session_key(event)
            clarify_gateway.register("prompt", session_key, "Which?", None)
            await adapter.send_clarify("C1", "Which?", ["A", "B"], "prompt", session_key,
                                       metadata={"team_id": "T1", "thread_id": "100.000001"})
            ready.set()
            await done.wait()
        else:
            assert clarify_gateway.resolve_gateway_clarify("prompt", event.text)

    adapter.set_message_handler(handler)
    try:
        await adapter._handle_slack_message(message("<@UBOT> start"))
        await asyncio.wait_for(ready.wait(), timeout=2)
        await adapter._handle_slack_message(message(reply, "UOTHER", "121.000001"))
        await adapter._handle_slack_message(message(f"<@UBOT> {reply}", "UOTHER", "122.000001"))
        await adapter._handle_slack_message(message("<@UBOT> no reply needed", user="UOTHER", ts="122.500001"))
        assert seen == ["UHUMAN"]
        assert not clarify_gateway._entries["prompt"].event.is_set()
        await adapter._handle_slack_message(message(reply, ts="123.000001"))
        assert seen == ["UHUMAN", "UHUMAN"]
        assert clarify_gateway._entries["prompt"].response == reply
        await adapter._handle_slack_message(message("thanks", ts="124.000001"))
        assert seen == ["UHUMAN", "UHUMAN"]
        # A resolved prompt is not authority for a new button response either.
        client.reset_mock()
        await adapter._handle_clarify_action(AsyncMock(), {
            "team": {"id": "T1"}, "channel": {"id": "C1"}, "user": {"id": "UHUMAN"},
            "message": {"ts": "200.000001"}},
            {"action_id": "hermes_clarify_choice_0", "value": "prompt|0"})
        assert not client.mock_calls
    finally:
        done.set()
        await drain(adapter)
        if session_key:
            clarify_gateway.clear_session(session_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "<@UBOT> no reply needed, just FYI", "<@UBOT> please don't respond",
    "<@UBOT> no response required", "<@UBOT> thanks!", "<@UBOT> FYI the deploy finished",
])
@pytest.mark.parametrize("extra", [{}, {"free_response_channels": ["C1"]}])
async def test_explicit_silence_veto_precedes_enrichment_and_task_admission(text, extra):
    adapter, client = native()
    adapter.config.extra.update(extra)
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)
    await adapter._handle_slack_message(message(text, client_msg_id=None))
    await drain(adapter)
    handler.assert_not_awaited()
    adapter._resolve_user_is_bot.assert_not_awaited()
    adapter._hydrate_thread_context.assert_not_awaited()
    adapter._resolve_user_name.assert_not_awaited()
    assert not adapter._admissions()
    assert not client.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "<@UBOT> use the other file\n> no reply needed",
    "<@UBOT> remove `no reply needed` from the file",
    "<@UBOT> thanks, use the other file",
])
async def test_quoted_silence_and_thanks_with_instructions_do_not_erase_requests(text):
    adapter, _ = native()
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)
    await adapter._handle_slack_message(message(text))
    await drain(adapter)
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_silence_and_free_response_do_not_reopen_deleted_surface():
    adapter, client = native()
    adapter.config.extra["free_response_channels"] = ["C1"]
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)
    await adapter._handle_slack_message({
        "type": "message", "subtype": "message_deleted", "channel": "C1", "team": "T1",
        "deleted_ts": "200.000001", "event_ts": "300.000001",
        "previous_message": {"user": "UBOT", "ts": "200.000001", "thread_ts": "100.000001"},
    })
    key = ("T1", "C1", "100.000001")
    generation = adapter._deletion_generation(key)
    await adapter._handle_slack_message(message("<@UBOT> no reply needed", ts="400.000001"))
    await adapter._handle_slack_message(message("use the other file", ts="401.000001"))
    await drain(adapter)
    assert adapter._deletion_record(key)["muted"]
    assert adapter._deletion_generation(key) == generation
    handler.assert_not_awaited()
    assert not client.mock_calls
    await adapter._handle_slack_message(message("<@UBOT> use the other file", ts="402.000001"))
    await drain(adapter)
    handler.assert_awaited_once()
    assert not adapter._deletion_record(key)["muted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("complete_first", [False, True])
async def test_clarification_buttons_need_actual_pending_state_and_original_requester(complete_first):
    adapter, client = native()
    ready, done = asyncio.Event(), asyncio.Event()
    session_key = None

    async def handler(event):
        nonlocal session_key
        session_key = adapter._event_session_key(event)
        clarify_gateway.register("button", session_key, "Which?", ["A", "B"])
        await adapter.send_clarify("C1", "Which?", ["A", "B"], "button", session_key,
                                  metadata={"team_id": "T1", "thread_id": "100.000001"})
        ready.set()
        await done.wait()

    adapter.set_message_handler(handler)
    try:
        await adapter._handle_slack_message(message("<@UBOT> choose"))
        await asyncio.wait_for(ready.wait(), timeout=2)
        if complete_first:
            done.set()
            await drain(adapter)
        body = {"team": {"id": "T1"}, "channel": {"id": "C1"},
                "user": {"id": "UOTHER"}, "message": {"ts": "200.000001"}}
        action = {"action_id": "hermes_clarify_choice_0", "value": "button|0"}
        client.reset_mock()
        await adapter._handle_clarify_action(AsyncMock(), body, action)
        assert not clarify_gateway._entries["button"].event.is_set()
        assert not client.mock_calls
        body["user"]["id"] = "UHUMAN"
        await adapter._handle_clarify_action(AsyncMock(), body, action)
        assert clarify_gateway._entries["button"].response == "A"
        client.chat_update.assert_awaited_once()
        client.reset_mock()
        await adapter._handle_clarify_action(AsyncMock(), body, action)
        assert not client.mock_calls
    finally:
        done.set()
        await drain(adapter)
        if session_key:
            clarify_gateway.clear_session(session_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "<@UBOT> "])
async def test_same_requester_can_steer_live_task_without_admitting_chatter(prefix):
    adapter, _ = native()
    ready, done = asyncio.Event(), asyncio.Event()
    seen = []

    async def handler(event):
        seen.append(event.source.user_id)
        ready.set()
        await done.wait()

    steering = AsyncMock(return_value=True)
    adapter.set_message_handler(handler)
    adapter._busy_session_handler = steering
    try:
        await adapter._handle_slack_message(message("<@UBOT> start"))
        await asyncio.wait_for(ready.wait(), timeout=2)
        for index, text in enumerate(("thanks", "FYI the deploy finished", "`no reply needed`",
                                      "<@UOTHER> use the other file", "?", "> use the other file")):
            await adapter._handle_slack_message(message(text, ts=f"121.{index:06d}"))
        steering.assert_not_awaited()
        await adapter._handle_slack_message(message(prefix + "use the other file", ts="122.000001"))
        steering.assert_awaited_once()
        assert steering.await_args.args[0].text == "use the other file"
        await adapter._handle_slack_message(message("use the other file", "UOTHER", "122.000002"))
        steering.assert_awaited_once()
        # Another requester gets a separate queued request, never control of this turn.
        await adapter._handle_slack_message(message("<@UBOT> my request", "UOTHER", "123.000001"))
        steering.assert_awaited_once()
        assert seen == ["UHUMAN"]
        done.set()
        while adapter._background_tasks:
            await drain(adapter)
        assert seen == ["UHUMAN", "UOTHER"]
        assert not adapter._participations()
    finally:
        done.set()
        await drain(adapter)


@pytest.mark.asyncio
async def test_old_approval_card_cannot_resolve_a_new_pending_request():
    from tools import approval
    from tools.approval_gateway_wait import _ApprovalEntry

    adapter, client = native()
    ready, done = asyncio.Event(), asyncio.Event()
    session_key = None

    async def handler(event):
        nonlocal session_key
        session_key = adapter._event_session_key(event)
        # Use the real approval entry/resolver; only the command executor is absent.
        with approval._lock:
            approval._gateway_queues[session_key] = [_ApprovalEntry({"request_id": "first"})]
        await adapter.send_exec_approval("C1", "echo first", session_key, metadata={
            "team_id": "T1", "thread_id": "100.000001", "approval_request_id": "first"})
        ready.set()
        await done.wait()

    adapter.set_message_handler(handler)
    try:
        await adapter._handle_slack_message(message("<@UBOT> execute"))
        await asyncio.wait_for(ready.wait(), timeout=2)
        # Approval buttons/commands are explicit actions, not open text prompts.
        adapter._hydrate_thread_context.reset_mock()
        await adapter._handle_slack_message(message("thanks", ts="121.000001"))
        adapter._hydrate_thread_context.assert_not_awaited()
        assert approval.has_blocking_approval(session_key)
        body = {"team": {"id": "T1"}, "channel": {"id": "C1"},
                "user": {"id": "UOTHER"}, "message": {"ts": "200.000001"}}
        action = {"action_id": "hermes_approve_once", "value": session_key}
        await adapter._handle_approval_action(AsyncMock(), body, action)
        assert approval.has_blocking_approval(session_key)
        body["user"]["id"] = "UHUMAN"
        await adapter._handle_approval_action(AsyncMock(), body, action)
        assert not approval.has_blocking_approval(session_key)
        with approval._lock:
            second = _ApprovalEntry({"request_id": "second"})
            approval._gateway_queues[session_key] = [second]
        # Even an unclicked duplicate of the old card must not act on the new request.
        adapter._approval_resolved[adapter._workspace_message_marker("T1", "200.000001")] = False
        client.reset_mock()
        await adapter._handle_approval_action(AsyncMock(), body, action)
        assert not second.event.is_set()
        assert not client.mock_calls
    finally:
        done.set()
        await drain(adapter)
        if session_key:
            approval.unregister_gateway_notify(session_key)


@pytest.mark.asyncio
async def test_model_picker_clicks_belong_to_the_original_requester():
    adapter, client = native()
    ready, done = asyncio.Event(), asyncio.Event()
    selected = AsyncMock(return_value="Switched")
    providers = [{"slug": "openrouter", "name": "OpenRouter", "models": ["m1"], "total_models": 1}]

    async def handler(event):
        await adapter.send_model_picker(
            "C1", providers, "old", "openrouter", adapter._event_session_key(event), selected,
            metadata={"team_id": "T1", "thread_id": "100.000001"})
        ready.set()
        await done.wait()

    adapter.set_message_handler(handler)
    try:
        await adapter._handle_slack_message(message("<@UBOT> /model"))
        await asyncio.wait_for(ready.wait(), timeout=2)
        body = {"team": {"id": "T1"}, "channel": {"id": "C1"},
                "user": {"id": "UOTHER"}, "message": {"ts": "200.000001"}}
        action = {"action_id": "hermes_model_provider", "selected_option": {"value": "0"}}
        client.reset_mock()
        await adapter._handle_model_picker_action(AsyncMock(), body, action)
        assert not client.mock_calls
        body["user"]["id"] = "UHUMAN"
        await adapter._handle_model_picker_action(AsyncMock(), body, action)
        client.chat_update.assert_awaited_once()
        await adapter._handle_model_picker_action(AsyncMock(), body, {
            "action_id": "hermes_model_model", "selected_option": {"value": "0"}})
        selected.assert_awaited_once_with("C1", "m1", "openrouter")
    finally:
        done.set()
        await drain(adapter)


def test_scheduled_text_and_media_carry_same_job_authority():
    from types import SimpleNamespace
    from gateway.config import Platform
    from cron.scheduler_delivery import _live_route_metadata

    target = SimpleNamespace(job={"id": "approved-job"}, platform=Platform.SLACK,
                             thread_id="100.000001", notify_delivery=True,
                             origin_target=False, origin={})
    _, text, media = _live_route_metadata(target)
    assert text["job_id"] == media["job_id"] == target.job["id"]


@pytest.mark.asyncio
async def test_queued_request_is_rechecked_before_any_work_after_mute():
    adapter, client = native()
    held = []
    # Hold only the scheduler boundary: ingress and pre-work dispatch stay real.
    adapter._start_session_processing = lambda event, key, **kwargs: held.append((event, key))
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)
    await adapter._handle_slack_message(message("<@UBOT> start"))
    assert len(held) == 1
    await adapter._handle_slack_message({
        "type": "message", "subtype": "message_deleted", "channel": "C1", "team": "T1",
        "deleted_ts": "200.000001", "event_ts": "300.000001",
        "previous_message": {"user": "UBOT", "ts": "200.000001", "thread_ts": "100.000001"}})
    client.reset_mock()
    await adapter._process_message_background(*held[0])
    handler.assert_not_awaited()
    assert not client.mock_calls


@pytest.mark.asyncio
async def test_gateway_turn_entry_rejects_unscoped_and_completed_native_tasks():
    from contextlib import nullcontext
    from contextvars import copy_context
    from gateway.run_turn import GatewayTurnMixin

    adapter, _ = native()
    runner = GatewayTurnMixin()
    runner._profile_scope_for_source = lambda source: nullcontext()
    runner._delivery_adapter_for = lambda source: adapter
    runner._run_agent_inner = AsyncMock(return_value={"final_response": "", "messages": []})
    captured = []

    async def handler(event):
        captured.append((copy_context(), event.source))
        await runner._run_agent("task", "", [], event.source, "test-session")

    adapter.set_message_handler(handler)
    await adapter._handle_slack_message(message("<@UBOT> start"))
    await drain(adapter)
    runner._run_agent_inner.assert_awaited_once()
    context, source = captured[0]
    await runner._run_agent("synthetic watcher", "", [], source, "test-session")
    await context.run(asyncio.create_task, runner._run_agent("stale task", "", [], source, "test-session"))
    runner._run_agent_inner.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("text,accepted", [
    ("```text\n<@UBOT> copied literal", False),
    ("`<@UBOT> copied literal", False),
    ("``<@UBOT> copied literal", False),
    ("`example\n<@UBOT> copied literal", False),
    ("<@U`copied`BOT> use the file", False),
    ("<@U```copied```BOT> use the file", False),
    ("<@UBOT> use the file\n```text\nno reply needed", True),
    ("<@UBOT> use the file `no reply needed", True),
    ("<@UBOT> use the file ``no reply needed``", True),
    ("<@UBOT> use the file `example\nno reply needed`", True),
    ("<@UBOT> remove no`copied` reply needed", True),
    ("<@UBOT> remove no `copied` reply needed", True),
    ("<@UBOT> remove no re`copied`ply needed", True),
    ("<@UBOT> remove no\n> copied\nreply needed", True),
    ("`<@UOTHER> no reply needed` <@UBOT> use the file", True),
    ("```text\n<@UOTHER> no reply needed\n```\n<@UBOT> use the file", True),
    ("> `no reply needed\n<@UBOT> use the file", True),
    ("> <@UBOT> no reply needed", False),
    ("<@UBOT> use the file\n> no reply needed", True),
    ("`example` <@UBOT> no reply needed", False),
    ("```example``` <@UBOT> no reply needed", False),
])
async def test_native_code_literal_boundaries_cannot_create_mentions_or_silence(text, accepted):
    adapter, client = native()
    handler = AsyncMock(return_value=None)
    adapter.set_message_handler(handler)
    await adapter._handle_slack_message(message(text))
    await drain(adapter)
    assert handler.await_count == int(accepted)
    if not accepted:
        adapter._hydrate_thread_context.assert_not_awaited()
        assert not client.mock_calls


@pytest.mark.asyncio
async def test_no_reply_preserves_normal_dm_steering_and_delivery():
    adapter, client = native()
    ready, done = asyncio.Event(), asyncio.Event()
    from contextvars import copy_context
    contexts = []

    async def handler(event):
        contexts.append(copy_context())
        ready.set()
        await done.wait()

    steering = AsyncMock(return_value=True)
    adapter.set_message_handler(handler)
    adapter._busy_session_handler = steering
    try:
        await adapter._handle_slack_message(message("start", channel="D1", channel_type="im"))
        await asyncio.wait_for(ready.wait(), timeout=2)
        owner = next(iter(adapter._session_tasks.values()))
        await adapter._handle_slack_message(message(
            "no reply needed", ts="121.000001", channel="D1", channel_type="im"))
        assert not owner.done()
        steering.assert_not_awaited()
        await adapter._handle_slack_message(message(
            "use the other file", ts="122.000001", channel="D1", channel_type="im"))
        steering.assert_awaited_once()
        assert steering.await_args.args[0].text == "use the other file"
        result = await contexts[0].run(asyncio.create_task, adapter.send("D1", "result"))
        assert result.success
    finally:
        done.set()
        await drain(adapter)


@pytest.mark.asyncio
async def test_no_reply_revokes_even_still_pending_prompt_callbacks():
    adapter, client = native()
    ready, done = asyncio.Event(), asyncio.Event()
    session_key = None

    async def handler(event):
        nonlocal session_key
        session_key = adapter._event_session_key(event)
        clarify_gateway.register("stopped-prompt", session_key, "Which?", ["A", "B"])
        await adapter.send_clarify("C1", "Which?", ["A", "B"], "stopped-prompt", session_key,
                                  metadata={"team_id": "T1", "thread_id": "100.000001"})
        ready.set()
        await done.wait()

    adapter.set_message_handler(handler)
    try:
        await adapter._handle_slack_message(message("<@UBOT> choose"))
        await asyncio.wait_for(ready.wait(), timeout=2)
        owner = next(iter(adapter._session_tasks.values()))
        entry = clarify_gateway._entries["stopped-prompt"]
        await adapter._handle_slack_message(message("<@UBOT> no reply needed", ts="121.000001"))
        assert owner.done()
        assert entry.event.is_set()
        assert clarify_gateway.get_pending_for_session(session_key, include_choice_prompts=True) is None
        client.reset_mock()
        await adapter._handle_clarify_action(AsyncMock(), {
            "team": {"id": "T1"}, "channel": {"id": "C1"}, "user": {"id": "UHUMAN"},
            "message": {"ts": "200.000001"}},
            {"action_id": "hermes_clarify_choice_0", "value": "stopped-prompt|0"})
        # Revocation resolves the prompt as cancelled; the stale click records no choice.
        assert entry.response == clarify_gateway.CANCELLED
        assert not client.mock_calls
        fresh = AsyncMock(return_value="fresh result")
        adapter.set_message_handler(fresh)
        await adapter._handle_slack_message(message("<@UBOT> start fresh", ts="122.000001"))
        await drain(adapter)
        fresh.assert_awaited_once()

    finally:
        done.set()
        await drain(adapter)
        if session_key:
            clarify_gateway.clear_session(session_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["enrichment", "scheduled_worker"])
@pytest.mark.parametrize("overflow", [False, True])
async def test_no_reply_before_owner_registration_rejects_stale_work(stage, overflow):
    adapter, client = native()
    adapter._REQUESTER_STOPS_MAX = 3
    entered, release = asyncio.Event(), asyncio.Event()
    handler = AsyncMock(return_value="authorized result")
    adapter.set_message_handler(handler)
    if stage == "enrichment":
        async def hydrate(**kwargs):
            if kwargs["ts"] == "120.000001":
                entered.set()
                await release.wait()
            return None, [], []
        adapter._hydrate_thread_context = hydrate
    else:
        original_worker = adapter._process_message_background

        async def worker(event, session_key):
            if event.message_id == "120.000001":
                entered.set()
                await release.wait()
            await original_worker(event, session_key)
        adapter._process_message_background = worker
    stale = asyncio.create_task(adapter._handle_slack_message(message("<@UBOT> original")))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not adapter._participations()
        await adapter._handle_slack_message(message("<@UBOT> no reply needed", ts="121.000001"))
        if overflow:
            for index in range(4):
                await adapter._handle_slack_message(message(
                    "<@UBOT> no reply needed", ts=f"12{index + 2}.000001",
                    channel=f"C{index + 2}"))
            assert len(adapter._slack_requester_stops) <= adapter._REQUESTER_STOPS_MAX
        release.set()
        await asyncio.wait_for(stale, timeout=2)
        await drain(adapter)
        handler.assert_not_awaited()
        assert not client.mock_calls
        # A retained/replayed old native event cannot outlive an evicted stop.
        await adapter._handle_slack_message(message("<@UBOT> stale replay", ts="119.000001"))
        await drain(adapter)
        handler.assert_not_awaited()
        # Stops target this requester; later explicit requests remain usable.
        await adapter._handle_slack_message(message("<@UBOT> other", user="UOTHER", ts="130.000001"))
        await drain(adapter)
        await adapter._handle_slack_message(message("<@UBOT> fresh", ts="131.000001"))
        await drain(adapter)
        assert [call.args[0].text for call in handler.await_args_list] == ["other", "fresh"]
        assert client.chat_postMessage.await_count == 2
    finally:
        release.set()
        await asyncio.gather(stale, return_exceptions=True)
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_requester_stop_history_is_bounded_without_reviving_evicted_surface():
    adapter, client = native()
    adapter._REQUESTER_STOPS_MAX = 3
    entered = asyncio.Event()

    async def handler(event):
        entered.set()
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    try:
        for index in range(5):
            entered.clear()
            await adapter._handle_slack_message(message(
                "<@UBOT> start", channel=f"C{index + 1}", ts=f"{120 + index * 2}.000001"))
            await asyncio.wait_for(entered.wait(), timeout=2)
            await adapter._handle_slack_message(message(
                "<@UBOT> no reply needed", channel=f"C{index + 1}", ts=f"{121 + index * 2}.000001"))
        assert len(adapter._slack_requester_stops) <= adapter._REQUESTER_STOPS_MAX
        handler = AsyncMock(return_value=None)
        adapter.set_message_handler(handler)
        client.reset_mock()
        await adapter._handle_slack_message(message("<@UBOT> stale", ts="119.000001"))
        await drain(adapter)
        handler.assert_not_awaited()
        assert not client.mock_calls
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_no_owner_stop_promotes_another_requesters_queued_work():
    adapter, client = native()
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def handler(event):
        seen.append(event.source.user_id)
        if event.source.user_id == "UOWNER":
            entered.set()
            await release.wait()
        return "authorized result"

    adapter.set_message_handler(handler)
    try:
        await adapter._handle_slack_message(message("<@UBOT> owner", user="UOWNER"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await adapter._handle_slack_message(message("<@UBOT> queued", ts="121.000001"))
        await adapter._handle_slack_message(message(
            "<@UBOT> independent queued", user="UOTHER", ts="122.000001"))
        await adapter._handle_slack_message(message("<@UBOT> no reply needed", ts="123.000001"))
        release.set()
        while adapter._background_tasks:
            await drain(adapter)
        assert seen == ["UOWNER", "UOTHER"]
        assert client.chat_postMessage.await_count == 2
    finally:
        release.set()
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("choices", [None, ["A", "B"]])
async def test_no_owner_stop_revokes_completed_tasks_actual_pending_prompt(choices):
    adapter, client = native()
    session_key = None

    async def handler(event):
        nonlocal session_key
        session_key = adapter._event_session_key(event)
        clarify_gateway.register("completed-stopped-prompt", session_key, "Which?", choices)
        await adapter.send_clarify("C1", "Which?", choices, "completed-stopped-prompt", session_key,
                                  metadata={"team_id": "T1", "thread_id": "100.000001"})

    adapter.set_message_handler(handler)
    try:
        await adapter._handle_slack_message(message("<@UBOT> choose"))
        await drain(adapter)
        assert not adapter._participations()
        entry = clarify_gateway._entries["completed-stopped-prompt"]
        assert not entry.event.is_set()
        await adapter._handle_slack_message(message("<@UBOT> no reply needed", ts="121.000001"))
        client.reset_mock()
        await adapter._handle_clarify_action(AsyncMock(), {
            "team": {"id": "T1"}, "channel": {"id": "C1"}, "user": {"id": "UHUMAN"},
            "message": {"ts": "200.000001"}},
            {"action_id": "hermes_clarify_choice_0", "value": "completed-stopped-prompt|0"})
        assert not entry.response
        assert not client.mock_calls
        assert clarify_gateway.get_pending_for_session(session_key, include_choice_prompts=True) is None
        fresh = AsyncMock(return_value="fresh result")
        adapter.set_message_handler(fresh)
        await adapter._handle_slack_message(message("<@UBOT> fresh", ts="122.000001"))
        await drain(adapter)
        fresh.assert_awaited_once()
    finally:
        await adapter.cancel_background_tasks()
        if session_key:
            clarify_gateway.clear_session(session_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["clarify", "approval", "slash"])
@pytest.mark.parametrize("foreign", [False, True])
async def test_no_owner_stop_targets_exact_completed_prompt(kind, foreign):
    from tools import approval, slash_confirm
    from tools.approval_gateway_wait import _ApprovalEntry

    adapter, client = native()
    client.chat_postMessage.side_effect = [
        {"ok": True, "ts": "200.000001"}, {"ok": True, "ts": "201.000001"}]
    session_key = None
    records = []
    confirmed = AsyncMock(return_value="confirmed")

    async def handler(event):
        nonlocal session_key
        session_key = adapter._event_session_key(event)
        identity = f"completed-{event.source.user_id}"
        metadata = {"team_id": "T1", "thread_id": "100.000001"}
        if kind == "clarify":
            entry = clarify_gateway.register(identity, session_key, "Which?", ["A", "B"])
            await adapter.send_clarify("C1", "Which?", ["A", "B"], identity, session_key,
                                      metadata=metadata)
        elif kind == "approval":
            entry = _ApprovalEntry({"request_id": identity})
            with approval._lock:
                approval._gateway_queues.setdefault(session_key, []).append(entry)
            await adapter.send_exec_approval("C1", "echo hello", session_key,
                                            metadata={**metadata, "approval_request_id": identity})
        else:
            entry = None
            slash_confirm.register(session_key, identity, "/confirm", confirmed)
            await adapter.send_slash_confirm("C1", "Confirm", "Proceed?", session_key, identity,
                                            metadata=metadata)
        records.append((identity, entry))

    adapter.set_message_handler(handler)
    try:
        await adapter._handle_slack_message(message("<@UBOT> original"))
        await drain(adapter)
        if foreign:
            clarify_gateway.clear_session(session_key)
            approval.resolve_gateway_approval(session_key, "deny", resolve_all=True)
            slash_confirm.clear(session_key)
            await adapter._handle_slack_message(message(
                "<@UBOT> independent", user="UOTHER", ts="121.000001"))
            await drain(adapter)
        assert len(records) == 1 + int(foreign)
        assert not adapter._participations()
        identity, entry = records[-1]
        await adapter._handle_slack_message(message("<@UBOT> no reply needed", ts="122.000001"))
        if kind == "clarify":
            pending = clarify_gateway.get_pending_for_session(session_key, include_choice_prompts=True)
        elif kind == "approval":
            pending = approval.list_gateway_approvals(session_key)
        else:
            pending = slash_confirm.get_pending(session_key)
        assert bool(pending) is foreign
        client.reset_mock()
        body = {"team": {"id": "T1"}, "channel": {"id": "C1"},
                "user": {"id": "UOTHER" if foreign else "UHUMAN"},
                "message": {"ts": "201.000001" if foreign else "200.000001"}}
        if kind == "clarify":
            await adapter._handle_clarify_action(AsyncMock(), body,
                {"action_id": "hermes_clarify_choice_0", "value": f"{identity}|0"})
            assert bool(entry.response) is foreign
        elif kind == "approval":
            await adapter._handle_approval_action(AsyncMock(), body,
                {"action_id": "hermes_approve_once", "value": session_key})
            assert entry.result == ("once" if foreign else "deny")
        else:
            await adapter._handle_slash_confirm_action(AsyncMock(), body,
                {"action_id": "hermes_confirm_once", "value": f"{session_key}|{identity}"})
            assert confirmed.await_count == int(foreign)
        if not foreign:
            assert not client.mock_calls
    finally:
        await adapter.cancel_background_tasks()
        if session_key:
            clarify_gateway.clear_session(session_key)
            approval.resolve_gateway_approval(session_key, "deny", resolve_all=True)
            slash_confirm.clear(session_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,accepted", [
    ("allowed", True), ("unauthorized", False), ("bot", False),
    ("other_channel", False), ("not_opted_in", False),
])
async def test_compat_real_reaction_producer_admits_only_authorized_trigger(policy, accepted):
    adapter, client = native()
    adapter.config.extra.update({"strict_mention": True, "thread_require_mention": True,
                                 "reaction_triggers": ["eyes"]})
    adapter._team_clients["T1"] = client
    adapter._channel_team["C1"] = "T1"
    client.conversations_replies.return_value = {
        "ok": True, "messages": [{"ts": "100.000001", "user": "UBOT"}]}
    if policy == "unauthorized":
        adapter._authorization_check = lambda *args, **kwargs: False
    elif policy == "bot":
        adapter._resolve_user_is_bot.return_value = True
    elif policy == "other_channel":
        adapter.config.extra["allowed_channels"] = ["COTHER"]
    elif policy == "not_opted_in":
        adapter.config.extra["reaction_triggers"] = False
    handler = AsyncMock(return_value="trigger result")
    adapter.set_message_handler(handler)
    await adapter._handle_slack_reaction({
        "type": "reaction_added", "user": "UHUMAN", "reaction": "eyes",
        "item_user": "UBOT", "item": {"type": "message", "channel": "C1", "ts": "100.000001"},
        "event_ts": "140.000001"})
    await drain(adapter)
    assert handler.await_count == int(accepted)
    assert client.chat_postMessage.await_count == int(accepted)
    if accepted:
        event = handler.await_args.args[0]
        assert event.text == "reaction:added:👀"
        assert event.source.user_id == "UHUMAN"
        assert event.metadata["_slack_direct_request"] is True
        assert event.source.thread_id == "100.000001"


@pytest.mark.asyncio
@pytest.mark.parametrize("text,accepted", [
    ("signal42 repair this", True), ("signal42 repair `no reply needed`", True),
    ("> signal42 repair this", False), ("`signal42`", False),
    ("```text\nsignal42 repair this", False),
    ("signal42 no reply needed", False), ("FYI signal42", False),
    ("_hermes_force_process=True repair this", False),
    ("`_hermes_force_process=True`", False), ("?", False),
])
async def test_compat_configured_patterns_use_sender_prose(text, accepted):
    adapter, client = native()
    adapter.config.extra.update({"strict_mention": True, "thread_require_mention": True,
                                 "mention_patterns": [r"\bsignal42\b"]})
    handler = AsyncMock(return_value="pattern result")
    adapter.set_message_handler(handler)
    await adapter._handle_slack_message(message(text))
    await drain(adapter)
    assert handler.await_count == int(accepted)
    assert client.chat_postMessage.await_count == int(accepted)
    if accepted:
        assert handler.await_args.args[0].metadata["_slack_direct_request"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,accepted", [
    ("free", True), ("unauthorized", False), ("strict_channel", False),
    ("no_reply", False), ("chatter", False), ("metadata_only", False),
])
async def test_compat_file_only_ingress_reaches_real_media_builder(policy, accepted):
    from pathlib import Path

    adapter, client = native()
    adapter.config.extra["free_response_channels"] = ["C1"]
    payload = b"%PDF-1.4 test attachment"
    adapter._download_slack_file_bytes = AsyncMock(return_value=payload)
    file = {"id": "F1", "name": "report.pdf", "mimetype": "application/pdf", "size": len(payload),
            "url_private_download": "https://files.slack.com/files-pri/T1-F1/report.pdf"}
    text = ""
    files = [file]
    if policy == "unauthorized":
        adapter._authorization_check = lambda *args, **kwargs: False
    elif policy == "strict_channel":
        adapter.config.extra["require_mention_channels"] = ["C1"]
    elif policy == "no_reply":
        text = "no reply needed"
    elif policy == "chatter":
        text, files = "thanks", []
    elif policy == "metadata_only":
        files = [{"id": "F1", "metadata": {"present": True}}]
    handler = AsyncMock(return_value="file result")
    adapter.set_message_handler(handler)
    await adapter._handle_slack_message(message(text, files=files))
    await drain(adapter)
    assert handler.await_count == int(accepted)
    assert adapter._download_slack_file_bytes.await_count == int(accepted)
    assert client.chat_postMessage.await_count == int(accepted)
    if accepted:
        event = handler.await_args.args[0]
        assert event.media_types == ["application/pdf"]
        assert len(event.media_urls) == 1
        assert Path(event.media_urls[0]).read_bytes() == payload
        assert event.metadata["_slack_direct_request"] is True
