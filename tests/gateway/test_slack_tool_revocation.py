"""Revoked native work must stop at the real registry dispatch boundary."""
import asyncio
import json
from unittest.mock import Mock

import pytest

import model_tools
from plugins.platforms.slack.participation import _participation_scope
from tests.gateway.test_slack_participation_delivery import META, native as native, start_turn
from tools.registry import registry


@pytest.fixture
def dispatch_probe(monkeypatch):
    name = "mcp_revocation_probe"
    registry.register(
        name=name, toolset="mcp-revocation-probe",
        schema={"name": name, "description": "Offline dispatch probe", "parameters": {
            "type": "object", "properties": {"payload": {"type": "string"}}}},
        handler=lambda args, **kwargs: json.dumps({"ok": True}),
    )
    calls = {}
    for tool in ("terminal", "discord", "read_file", name):
        handler = Mock(return_value=json.dumps({"ok": True}))
        entry = registry.get_entry(tool)
        monkeypatch.setattr(entry, "handler", handler)
        monkeypatch.setattr(entry, "is_async", False)
        calls[tool] = handler
    yield calls
    registry.deregister(name)


def dispatch(path):
    args = {"payload": "private request content"}
    if path == "registry":
        return registry.dispatch("terminal", args)
    if path == "deferred":
        return model_tools.handle_function_call(
            "tool_call", {"name": "mcp_revocation_probe", "arguments": args},
            enabled_toolsets=["mcp-revocation-probe"])
    return model_tools.handle_function_call(
        path, args, skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True, skip_tool_execution_middleware=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["terminal", "discord", "mcp_revocation_probe", "deferred", "registry"])
@pytest.mark.parametrize("end", ["no_reply", "complete", "mute", "authority_lost"])
async def test_revocation_blocks_real_dispatch(native, dispatch_probe, monkeypatch, path, end):
    adapter, _, _ = native
    context, release, owner = await start_turn(adapter)
    fresh = None
    try:
        assert json.loads(await asyncio.to_thread(context.run, dispatch, path))["ok"]
        if end in {"complete", "authority_lost"}:
            release.set()
            await owner
        elif end == "no_reply":
            await adapter._handle_slack_message({
                "type": "message", "channel": "C1", "team": "T1", "user": "UHUMAN",
                "ts": "300.000001", "thread_ts": META["thread_id"],
                "text": "<@UBOT> no reply needed"})
        else:
            await adapter._handle_slack_message({
                "type": "message", "subtype": "message_deleted", "channel": "C1", "team": "T1",
                "deleted_ts": "200.000001", "event_ts": "300.000001",
                "previous_message": {"user": "UBOT", "ts": "200.000001", "thread_ts": META["thread_id"]}})
        await asyncio.gather(owner, return_exceptions=True)
        fresh = await start_turn(adapter, "400.000001")
        assert json.loads(await asyncio.to_thread(fresh[0].run, dispatch, path))["ok"]
        if end == "authority_lost":
            # Reauthorization failures must be content-free, even if the checker raises.
            context = fresh[0].copy()
            def unavailable(*args, **kwargs):
                raise RuntimeError("private authorization failure")
            monkeypatch.setattr(adapter, "_authorization_check", unavailable)
        for handler in dispatch_probe.values():
            handler.reset_mock()
        result = json.loads(await asyncio.to_thread(context.run, dispatch, path))
        assert not any(handler.called for handler in dispatch_probe.values()), result
        assert result == {"error": "tool_execution_revoked"}
        if end != "authority_lost":
            assert context.run(_participation_scope.get) is not fresh[0].run(_participation_scope.get)
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        if fresh:
            fresh[1].set()
            await asyncio.gather(fresh[2], return_exceptions=True)


@pytest.mark.asyncio
async def test_unscoped_and_authorized_dispatch_preserve_scope(native, dispatch_probe, monkeypatch):
    adapter, client, _ = native
    # Explicit CLI and scheduled work have no native request grant.
    for cron in ("", "1"):
        monkeypatch.setenv("HERMES_CRON_SESSION", cron)
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "slack")
        for path in ("terminal", "discord", "read_file", "deferred"):
            assert json.loads(await asyncio.to_thread(dispatch, path))["ok"]
        assert _participation_scope.get() is None
    context, release, owner = await start_turn(adapter)
    try:
        assert json.loads(await asyncio.to_thread(context.run, dispatch, "read_file"))["ok"]
        # Real async registry bridge must preserve the native audience grant.
        entry = registry.get_entry("discord")

        async def send(args, **kwargs):
            result = await adapter.send(args["channel"], "result", metadata=META)
            return json.dumps({"ok": result.success})

        monkeypatch.setattr(entry, "handler", send)
        monkeypatch.setattr(entry, "is_async", True)
        client.reset_mock()
        accepted = context.run(registry.dispatch, "discord", {"channel": "C1"})
        assert json.loads(accepted)["ok"]
        client.chat_postMessage.assert_awaited_once()
        client.reset_mock()
        denied = context.run(registry.dispatch, "discord", {"channel": "C2"})
        assert not json.loads(denied)["ok"]
        assert not client.mock_calls

        # Revoke between the sync registry entry and the async handler's first instruction.
        run_async = model_tools._run_async
        grant = context.run(_participation_scope.get)
        async_handler = Mock(wraps=send)
        monkeypatch.setattr(entry, "handler", async_handler)

        def revoke_queued(coro):
            grant.revoked = True
            return run_async(coro)

        monkeypatch.setattr(model_tools, "_run_async", revoke_queued)
        result = context.run(registry.dispatch, "discord", {"channel": "C1"})
        assert json.loads(result) == {"error": "tool_execution_revoked"}
        async_handler.assert_not_called()
        assert not client.mock_calls
        # Ambient routing and a cron marker cannot bypass a revoked native context.
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        assert json.loads(context.run(dispatch, "terminal")) == {"error": "tool_execution_revoked"}
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
