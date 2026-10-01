"""Real native SDK boundary and inherited-task isolation; no live Slack calls."""
import asyncio

import pytest

from tests.gateway.test_client_context import corpus, runner, wire  # noqa: F401
from tests.gateway.test_slack_channel_isolation import (  # noqa: F401
    CHANNEL, native_text, drain_native,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "removed", "generation"])
async def test_native_sdk_rechecks_policy_immediately_before_post(
        native_text, runner, wire, monkeypatch, change):
    pytest.importorskip("slack_sdk.web.async_base_client")
    adapter, _, payload = native_text
    calls = []

    async def transport(*, api_url, **kwargs):
        calls.append(api_url.rsplit("/", 1)[-1])
        if api_url.endswith("users.info"):
            data = {"ok": True, "user": {"id": "U092L0QKYMB", "is_bot": False}}
        elif api_url.endswith("conversations.replies"):
            data = {"ok": True, "messages": [], "has_more": False}
        else:
            data = {"ok": True, "ts": "200.1"}
        return {"status_code": 200, "headers": {}, "data": data}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    client = adapter._new_web_client("synthetic", None)
    adapter._team_clients = {"T07MMEX5658": client}
    adapter._app.client = client
    adapter.set_authorization_check(lambda *args, **kwargs: True)
    post = client.chat_postMessage

    async def delayed_post(**kwargs):
        if change == "removed":
            runner.config.client_context = {"enabled": False}
        elif change == "generation":
            runner.config.client_context["codewords_read_channels"][CHANNEL]["generation"] = "v2"
        return await post(**kwargs)

    monkeypatch.setattr(client, "chat_postMessage", delayed_post)
    await adapter._handle_slack_message(payload, {"team_id": "T07MMEX5658"})
    await drain_native(adapter)
    assert len(wire.captured) == 1
    assert calls == ["users.info", "reactions.add", "conversations.replies"] + (
        ["chat.postMessage", "reactions.remove", "reactions.add"] if change is None else ["reactions.remove"])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["scheduled", "background", "media", "slash"])
async def test_target_has_no_background_scheduled_media_or_slash_escape(
        native_text, tmp_path, kind):
    adapter, client, _ = native_text
    metadata = {"team_id": "T07MMEX5658", "thread_id": "100.0"}
    if kind == "scheduled":
        metadata["job_id"] = "old-unrestricted-job"
    if kind == "media":
        attachment = tmp_path / "private.txt"
        attachment.write_text("PRIVATE_ATTACHMENT")
        await adapter.send_document(CHANNEL, str(attachment), metadata=metadata)
    elif kind == "slash":
        await adapter._handle_slash_command({
            "team_id": "T07MMEX5658", "channel_id": CHANNEL, "user_id": "U07MJJMMK9T",
            "command": "/reset", "response_url": "https://invalid.test/private",
        })
        assert not adapter._slash_command_contexts
    else:
        await adapter.send(CHANNEL, "PRIVATE_BACKGROUND_OUTPUT", metadata=metadata)
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_isolated_native_task_cannot_execute_or_defer_tools_and_output(native_text, wire):
    from tools.interrupt import tool_execution_allowed

    adapter, client, payload = native_text
    original = adapter._message_handler
    allowed = []

    async def checked(event):
        allowed.append(tool_execution_allowed())

        async def child():
            allowed.append(tool_execution_allowed())
            result = await adapter.send(CHANNEL, "PRIVATE_DEFERRED_OUTPUT", metadata={
                "team_id": "T07MMEX5658", "thread_id": "100.0", "job_id": "old-job",
            })
            assert not result.success

        await asyncio.create_task(child())
        return await original(event)

    adapter._message_handler = checked
    await adapter._handle_slack_message(payload, {"team_id": "T07MMEX5658"})
    await drain_native(adapter)
    assert allowed == [False, False]
    assert len(wire.captured) == 1
    assert client.chat_postMessage.await_count == 1
    assert "PRIVATE_DEFERRED_OUTPUT" not in client.chat_postMessage.call_args.kwargs["text"]
