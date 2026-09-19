"""Slack-owned notification copy must not rename payloads or other platforms."""

import json
import queue
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.session import SessionSource
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,brand", [(Platform.SLACK, "Jarvis"), (Platform.TELEGRAM, "Hermes")])
@pytest.mark.parametrize("delivery", ["startup", "watcher", "timeout", "post_update", "failed_update"])
async def test_notification_brand_is_destination_scoped(tmp_path, monkeypatch, platform, brand, delivery):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner = object.__new__(gateway_run.GatewayRunner)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True)))
    runner.adapters = {platform: adapter}
    runner._thread_metadata_for_target = Mock(return_value=None)
    runner._peek_session_state = Mock(return_value=None)
    payload = "Hermes output: hermes update /tmp/hermes https://hermes.example HERMES_HOME"
    if delivery == "startup":
        home = HomeChannel(platform=platform, chat_id="C1", name="Hermes operations")
        runner.config = GatewayConfig(platforms={platform: PlatformConfig(enabled=True, home_channel=home)})
        await runner._send_home_channel_startup_notifications()
        assert adapter.send.call_args.args[1] == f"♻️ Gateway online — {brand} is back and ready."
        return

    (tmp_path / ".update_pending.json").write_text(json.dumps({"platform": platform.value, "chat_id": "C1"}))
    paths = runner._update_paths()
    if delivery != "failed_update":
        paths.output.write_text(payload)
    if delivery != "timeout":
        paths.exit_code.write_text("1" if delivery == "failed_update" else "0")
    if delivery in {"watcher", "timeout"}:
        await runner._watch_update_progress(timeout=0 if delivery == "timeout" else 2)
    else:
        assert await runner._send_update_notification()
    messages = [call.args[1] for call in adapter.send.call_args_list]
    assert f"{brand} update" in messages[-1]
    if platform == Platform.SLACK:
        assert all("hermes" not in message.lower() for message in messages)
        assert not any(payload in message for message in messages)
    elif delivery == "failed_update":
        assert "`hermes update`" in messages[-1]
    elif delivery != "timeout":
        assert any(payload in message for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,brand", [(Platform.SLACK, "Jarvis"), (Platform.TELEGRAM, "Hermes")])
@pytest.mark.parametrize("native_success", [True, False])
async def test_handoff_home_and_task_labels_preserve_user_content(monkeypatch, platform, brand, native_success):
    import gateway.run as gateway_run

    runner = object.__new__(gateway_run.GatewayRunner)
    source = SessionSource(platform=platform, chat_id="C1", user_id="U1")
    runner.config = GatewayConfig()
    runner.session_store = object()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, has_any_sessions=AsyncMock(return_value=True),
    )
    runner._deliver_platform_notice = AsyncMock()
    monkeypatch.setattr(gateway_run, "_home_target_env_var", lambda _: None)
    await runner._hmwa_first_contact_notes(source, [], [])
    notice = runner._deliver_platform_notice.call_args.args[1]
    assert f"where {brand} delivers" in notice
    assert ("/jarvis sethome" if platform == Platform.SLACK else "/sethome") in notice

    adapter = SimpleNamespace(
        create_handoff_thread=AsyncMock(return_value="T1"),
        send_native_task_card_progress=AsyncMock(return_value=SendResult(success=native_success)),
        send=AsyncMock(return_value=SendResult(success=True, message_id="M1")),
    )
    transport = SimpleNamespace(adapter=adapter)
    monkeypatch.setattr("gateway.delivery.resolve_delivery_transport", lambda *_: transport)
    runner._handoff_resolve_scope = Mock(return_value=(runner.config, {platform: adapter}))
    row = {"id": "session-123", "handoff_platform": platform.value, "title": "Hermes design /tmp/hermes"}
    with pytest.raises(RuntimeError, match="/jarvis sethome" if platform == Platform.SLACK else "/sethome"):
        await runner._handoff_resolve_destination(row, None)
    runner.config.platforms[platform] = PlatformConfig(
        enabled=True, home_channel=HomeChannel(platform=platform, chat_id="C1", name="Ops"),
    )
    await runner._handoff_resolve_destination(row, None)
    adapter.create_handoff_thread.assert_awaited_once_with("C1", f"{brand} — {row['title']}")

    progress_queue = queue.Queue()
    preview = "Hermes /tmp/hermes HERMES_HOME"
    progress_queue.put({"type": "tool.started", "tool_call_id": "hermes_tool_1", "tool_name": "terminal", "preview": preview})
    ctx = SimpleNamespace(
        source=source, progress_queue=progress_queue, _run_still_current=Mock(side_effect=[True, False]),
        agent_holder=[], _progress_reply_to=None, _progress_metadata=None,
    )
    turn = TurnRunner(runner, cast(TurnContext, ctx))
    monkeypatch.setattr(turn, "_track_progress_result", Mock())
    await turn._send_native_task_card_progress(adapter)
    card = adapter.send_native_task_card_progress.call_args.kwargs
    assert card["title"] == f"{brand} is working"
    assert card["fallback_text"].startswith(f"{brand} is working\n")
    assert card["tasks"] == [{"id": "hermes_tool_1", "title": f"terminal - {preview}", "status": "in_progress"}]
    if not native_success:
        assert adapter.send.call_args.kwargs["content"] == card["fallback_text"]
