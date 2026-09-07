"""Slack branding changes presentation, never callback identity or user content."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType
from plugins.platforms.slack.adapter import SlackAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_registry", [False, True])
@pytest.mark.parametrize("umbrella", ["/jarvis", "/hermes"])
@pytest.mark.parametrize(
    ("text", "expected", "message_type"),
    [
        ("", "/help", MessageType.COMMAND),
        ("status", "/status", MessageType.COMMAND),
        ("compact", "/compress", MessageType.COMMAND),
        (
            "Explain Hermes and /hermes using ~/.hermes and HERMES_HOME",
            "Explain Hermes and /hermes using ~/.hermes and HERMES_HOME",
            MessageType.TEXT,
        ),
    ],
)
async def test_jarvis_and_legacy_umbrella_route_without_exposing_legacy_ack(
    monkeypatch, empty_registry, umbrella, text, expected, message_type
):
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    adapter._app = MagicMock()
    handle_message = AsyncMock()
    monkeypatch.setattr(adapter, "handle_message", handle_message)
    monkeypatch.setattr(adapter, "_register_plugin_action_handlers", MagicMock())
    monkeypatch.setattr(adapter, "_wire_plugin_handlers", MagicMock())
    if empty_registry:
        monkeypatch.setattr("hermes_cli.commands_platforms.slack_native_slashes", lambda: [])

    adapter._register_bolt_handlers()
    pattern = adapter._app.command.call_args.args[0]
    assert pattern.fullmatch(umbrella)
    assert not pattern.fullmatch("/jarvis-extra")
    listener = adapter._app.command.return_value.call_args.args[0]
    ack = AsyncMock()
    payload = {
        "command": umbrella, "text": text, "user_id": "U1", "channel_id": "C1",
        "team_id": "T1", "thread_ts": "111.222", "response_url": "https://example.test/reply",
    }
    await listener(ack, payload)

    ack.assert_awaited_once_with(response_type="ephemeral", text="Running `/jarvis`…")
    assert handle_message.await_args is not None
    event = handle_message.await_args.args[0]
    assert event.text == expected
    assert event.message_type == message_type
    assert event.raw_message is payload
    assert event.source.thread_id == "111.222"
    assert bool(adapter._slash_command_contexts) == (message_type == MessageType.COMMAND)
    assert adapter._slash_command_text({"command": "/model", "text": " Hermes  "}) == "/model  Hermes  "


@pytest.mark.asyncio
async def test_jarvis_owned_labels_preserve_payloads_and_callback_ids(monkeypatch):
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    adapter._app = MagicMock()
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "123.456"}
    client.api_call.return_value = {"ok": True, "ts": "123.456"}
    monkeypatch.setattr(adapter, "_get_client", MagicMock(return_value=client))
    monkeypatch.setattr(adapter, "stop_typing", AsyncMock())

    assert await adapter.create_handoff_thread("C1", "Hermes research") == "123.456"
    assert client.chat_postMessage.await_args.kwargs["text"] == ":thread: Jarvis handoff — *Hermes research*"

    task = {"id": "hermes-task", "title": "Research Hermes", "status": "in_progress"}
    result = await adapter.send_native_task_card_progress("C1", [task], reply_to="111.222")
    assert result.success
    chunks = client.api_call.await_args.kwargs["json"]["chunks"]
    assert chunks[0]["title"] == "Jarvis is working"
    assert chunks[1]["id"] == task["id"]
    assert chunks[1]["title"] == task["title"]

    content = "Hermes document: /hermes HERMES_HOME ~/.hermes https://hermes.example.test"
    assert (await adapter.send("C1", content)).success
    assert content in client.chat_postMessage.await_args.kwargs["text"]

    command = "hermes status --path ~/.hermes"
    assert (await adapter.send_exec_approval("C1", command, "hermes-session")).success
    approval = client.chat_postMessage.await_args.kwargs
    assert command in approval["blocks"][0]["text"]["text"]
    buttons = approval["blocks"][1]["elements"]
    assert [button["action_id"] for button in buttons] == [
        "hermes_approve_once", "hermes_approve_session", "hermes_approve_always", "hermes_deny",
    ]
    assert all(button["value"] == "hermes-session" for button in buttons)

    assert (await adapter.send_slash_confirm(
        "C1", "Hermes document", content, "hermes-session", "hermes-confirm"
    )).success
    confirm = client.chat_postMessage.await_args.kwargs
    assert content in confirm["blocks"][0]["text"]["text"]
    assert [button["action_id"] for button in confirm["blocks"][1]["elements"]] == [
        "hermes_confirm_once", "hermes_confirm_always", "hermes_confirm_cancel",
    ]
