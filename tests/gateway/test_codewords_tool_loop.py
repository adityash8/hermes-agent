"""Bounded Codewords protocol through real SDK requests; no live integrations."""

import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI

from gateway import client_context as cc
from gateway import client_context_codewords as route
from gateway import client_context_codewords_db as db
from gateway import client_context_codewords_reads as reads
from gateway.client_context_policy import ContextDenied
from tests.gateway.test_client_context import corpus, forbidden, runner, wire
from tests.gateway.test_slack_channel_isolation import CHANNEL, sealed

DATES = {"start_date": "2020-01-01", "end_date": "2020-01-02"}
EXPECTED = {binding.name for binding in reads.bindings()}
TOOL_NAMES = EXPECTED | {"codewords_slack_history_read"}


def call(name="codewords_gsc_read", args=None, ident="call-1"):
    return {"id": ident, "type": "function", "function": {
        "name": name, "arguments": json.dumps(DATES if args is None else args),
    }}


def respond(wire, calls):
    wire.state.tool_calls = calls
    wire.state.finish = "tool_calls" if calls else "stop"
    wire.state.text = None if calls else "The requested integrations are unavailable."


@pytest.fixture
def unavailable(monkeypatch):
    """Exercise the real dispatcher without reading any installed credentials."""
    monkeypatch.setattr(reads, "_BINDINGS", tuple(
        replace(binding, blocker="synthetic_missing_binding") for binding in reads.bindings()
    ))
    monkeypatch.setattr(reads.transport, "_http_client", forbidden)
    monkeypatch.setattr(reads, "_secret", forbidden)
    monkeypatch.setattr(db, "inventory", forbidden)


@pytest.mark.asyncio
async def test_bound_schemas_and_total_budget_remove_tools_on_final_request(runner, wire, unavailable):
    trigger = sealed(runner)
    names = sorted(EXPECTED, reverse=True)[:route.MAX_CALLS]

    def next_response(body):
        index = len(wire.captured) - 1
        if index < len(names):
            name = names[index]
            args = {"operation": reads.OPERATIONS[name][0]} if name in reads.UNDATED else DATES
            respond(wire, [call(name, args, f"call-{index}")])
        else:
            respond(wire, [])

    wire.state.callback = next_response
    assert await runner._handle_message(trigger) == "The requested integrations are unavailable."
    assert len(wire.captured) == route.MAX_CALLS + 1
    tools = wire.captured[0]["tools"]
    assert {tool["function"]["name"] for tool in tools} == TOOL_NAMES
    assert len(tools) == len(TOOL_NAMES)
    for tool in tools:
        function = tool["function"]
        params = function["parameters"]
        assert params["additionalProperties"] is False
        if function["name"] == "codewords_slack_history_read":
            assert not {"channel", "thread", "workspace"} & set(params["properties"])
            continue
        keys = {"operation"} if function["name"] in reads.UNDATED else {"start_date", "end_date"}
        assert set(params["required"]) == keys
        allowed = keys | {"operation"}
        if function["name"] == "codewords_stripe_read":
            allowed |= {"comparison_start_date", "comparison_end_date"}
        assert set(params["properties"]) == allowed
    assert wire.captured[-1]["tools"] == []
    assert wire.captured[-1]["tool_choice"] == "none"
    results = [message for message in wire.captured[-1]["messages"] if message["role"] == "tool"]
    assert len(results) == route.MAX_CALLS
    assert {json.loads(message["content"])["operation"] for message in results} == set(names)
    assert all(json.loads(message["content"])["status"] == "unavailable" for message in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("malice", ["duplicate-key", "unknown-tool", "account", "sql", "url", "write", "duplicate-id", "too-many"])
async def test_invalid_later_sibling_denies_entire_batch_before_execution(runner, wire, monkeypatch, malice):
    trigger = sealed(runner)
    bad = call(ident="call-2")
    if malice == "duplicate-key":
        bad["function"]["arguments"] = '{"start_date":"2020-01-01","start_date":"2020-01-02","end_date":"2020-01-03"}'
    elif malice == "unknown-tool":
        bad["function"]["name"] = "terminal"
    elif malice == "write":
        bad = call("codewords_gtm_read", {"operation": "publish"}, "call-2")
    elif malice == "duplicate-id":
        bad["id"] = "call-1"
    elif malice != "too-many":
        key, value = {"account": ("account_id", "foreign-account"),
                      "sql": ("sql", "SELECT secret FROM private"),
                      "url": ("url", "https://foreign.invalid/")}[malice]
        bad["function"]["arguments"] = json.dumps({**DATES, key: value})
    calls = [call(), bad]
    if malice == "too-many":
        calls = [call(ident=f"call-{index}") for index in range(route.MAX_CALLS + 1)]
    execute = Mock(side_effect=forbidden)
    monkeypatch.setattr(reads, "execute", execute)
    respond(wire, calls)
    assert await runner._handle_message(trigger) is None
    execute.assert_not_called()
    assert len(wire.captured) == 1


@pytest.mark.asyncio
async def test_private_canaries_absent_across_rounds_and_following_turn(runner, wire, unavailable, monkeypatch):
    trigger = sealed(runner)
    private = "PRIVATE_CONTEXT_CANARY_962"
    runner._client_context_history = {"other-client": private}
    for attr in ("channel_prompt", "channel_context", "reply_to_text", "auto_skill", "raw_message", "metadata"):
        setattr(trigger, attr, private)
    trigger.source.chat_name = trigger.source.user_name = trigger.source.chat_topic = private
    monkeypatch.setattr(cc, "_prepare", forbidden)
    wire.state.callback = lambda body: respond(wire, [call()] if len(wire.captured) == 1 else [])
    assert await runner._handle_message(trigger)
    assert await runner._handle_message(sealed(runner))
    assert len(wire.captured) == 3
    assert [message["role"] for message in wire.captured[1]["messages"]] == ["system", "user", "assistant", "tool"]
    assert [message["role"] for message in wire.captured[2]["messages"]] == ["system", "user"]
    assert private not in json.dumps(wire.captured)
    assert "CODEWORDS_A_SYNTHETIC" not in json.dumps(wire.captured)
    assert "CLIENT_B_SYNTHETIC" not in json.dumps(wire.captured)
    assert runner._client_context_history == {"other-client": private}
    runner.session_store.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["resources", "config", "run"])
@pytest.mark.parametrize("moment", ["dispatch", "release"])
async def test_revocation_at_dispatch_and_release(runner, wire, unavailable, monkeypatch, mutation, moment):
    trigger = sealed(runner)

    def revoke():
        if mutation == "resources":
            monkeypatch.setattr(reads, "_BINDINGS", tuple(
                replace(binding, resources=(("foreign_resource", "revoked"),)) for binding in reads.bindings()
            ))
        elif mutation == "config":
            runner.config.client_context["codewords_read_channels"][CHANNEL]["generation"] = "revoked"
        else:
            monkeypatch.setattr(runner, "_is_session_run_current", lambda *args: False)

    execute = Mock(wraps=reads.execute)
    monkeypatch.setattr(reads, "execute", execute)
    if moment == "dispatch":
        respond(wire, [call()])
        wire.state.callback = lambda body: revoke()
    reply = await runner._handle_message(trigger)
    if moment == "dispatch":
        assert reply is None
    else:
        assert isinstance(reply, cc.ScopedReply)
        revoke()
        with pytest.raises(ContextDenied):
            reply._codewords_validate()
    execute.assert_not_called()
    assert len(wire.captured) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_stale_thread_cannot_request_next_round_or_release(runner, wire, monkeypatch, stop):
    trigger = sealed(runner)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    respond(wire, [call()])
    original_loop = route._completion_loop

    def completion(*args):
        try:
            return original_loop(*args)
        finally:
            finished.set()

    def slow_execute(name, arguments, check):
        reads.validate_call(name, arguments)
        entered.set()
        assert release.wait(10)
        check()
        return '{"status":"unavailable"}'

    monkeypatch.setattr(route, "_completion_loop", completion)
    monkeypatch.setattr(reads, "execute", slow_execute)
    if stop == "timeout":
        monkeypatch.setattr(route, "TURN_TIMEOUT", 2)
    task = asyncio.create_task(runner._handle_message(trigger))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is None
        assert not finished.is_set()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 10)
    assert len(wire.captured) == 1
    runner._pop_post_delivery_callback.assert_not_called()
    runner._hmwa_stop_typing_for_turn.assert_awaited_once()
    assert all(state.turn.agent is None for state in runner._sessions_map().values())


@pytest.mark.asyncio
@pytest.mark.parametrize("expires", [False, True])
async def test_total_deadline_spans_provider_rounds_and_rejects_late_completion(
    runner, wire, unavailable, monkeypatch, expires,
):
    trigger = sealed(runner)
    clock = SimpleNamespace(elapsed=0.0)
    monkeypatch.setattr(route, "time", SimpleNamespace(monotonic=lambda: clock.elapsed))
    provider_timeouts = []
    original_send = httpx.Client.send

    def send(client, request, **kwargs):
        provider_timeouts.append(request.extensions["timeout"])
        return original_send(client, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send)

    def response(body):
        # Advance only the route clock: no real sleeps or changes to SDK timeouts.
        clock.elapsed += cc.TIMEOUT - 1
        if len(wire.captured) < 3:
            respond(wire, [call(ident=f"call-{len(wire.captured)}")])
        else:
            if expires:
                clock.elapsed = route.TURN_TIMEOUT
            respond(wire, [])

    wire.state.callback = response
    reply = await runner._handle_message(trigger)
    assert len(wire.captured) == 3
    assert clock.elapsed > cc.TIMEOUT
    assert all(set(timeout.values()) == {cc.TIMEOUT} for timeout in provider_timeouts)
    if expires:
        assert reply is None
        runner._pop_post_delivery_callback.assert_not_called()
    else:
        assert reply == "The requested integrations are unavailable."
        clock.elapsed = route.TURN_TIMEOUT
        with pytest.raises(ContextDenied):
            reply._codewords_validate()
    runner._hmwa_stop_typing_for_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_codex_encrypted_reasoning_replays_only_inside_current_turn(runner, unavailable, monkeypatch):
    trigger = sealed(runner)
    runner.config.client_context["codewords_read_channels"][CHANNEL]["provider"] = "openai-codex"
    trigger.source._codewords_policy = route.route(runner.config, trigger.source)
    trigger._codewords_request = route.seal_event(trigger)
    captured = []

    def receive(request):
        body = json.loads(request.content)
        captured.append(body)
        item = ({"type": "function_call", "id": "fc-synthetic", "call_id": "call-synthetic",
                 "name": "codewords_gsc_read", "arguments": json.dumps(DATES), "status": "completed"}
                if len(captured) == 1 else {"type": "message", "role": "assistant", "status": "completed",
                 "id": "msg-synthetic", "content": [{"type": "output_text", "text": "Integration unavailable.",
                                                        "annotations": []}]})
        final = {"id": "response-synthetic", "object": "response", "created_at": 0, "status": "completed",
                 "model": "synthetic-main", "output": [item], "error": None, "incomplete_details": None}
        if len(captured) == 1:
            final["output"].insert(0, {"type": "reasoning", "id": "rs-synthetic", "summary": [],
                                        "encrypted_content": "SCOPED_ENCRYPTED_REASONING"})
        events = [{"type": "response.output_item.done", "output_index": index, "item": output}
                  for index, output in enumerate(final["output"])]
        events.append({"type": "response.completed", "response": final})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            text="".join(f"event: {entry['type']}\ndata: {json.dumps(entry)}\n\n" for entry in events))

    def create(**kwargs):
        return OpenAI(api_key="synthetic", base_url="https://synthetic.invalid/v1",
                      http_client=httpx.Client(transport=httpx.MockTransport(receive)))

    monkeypatch.setattr("agent.auxiliary_client._read_codex_access_token", lambda: "synthetic")
    monkeypatch.setattr("agent.auxiliary_client._create_openai_client", create)
    assert await runner._handle_message(trigger) == "Integration unavailable."
    assert await runner._handle_message(trigger) == "Integration unavailable."
    assert len(captured) == 3
    assert {tool["name"] for tool in captured[0]["tools"]} == TOOL_NAMES
    assert captured[0]["include"] == ["reasoning.encrypted_content"]
    assert {"type": "reasoning", "summary": [], "encrypted_content": "SCOPED_ENCRYPTED_REASONING"} in captured[1]["input"]
    assert captured[1]["input"][-1]["type"] == "function_call_output"
    assert json.loads(captured[1]["input"][-1]["output"])["status"] == "unavailable"
    assert "SCOPED_ENCRYPTED_REASONING" not in json.dumps(captured[2])
    assert all(body["store"] is False for body in captured)
    assert not runner.__dict__.get("_client_context_history")
