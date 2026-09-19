"""Exercise deletion through real Bolt authorization, middleware and listeners."""
import copy
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("slack_bolt.async_app")
from slack_bolt.async_app import AsyncApp
from slack_bolt.request.async_request import AsyncBoltRequest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from gateway.config import PlatformConfig
from plugins.platforms.slack import adapter as slack_module


META = {"team_id": "T1", "thread_id": "100.000001"}
BODY = {
    "type": "event_callback", "team_id": "T1", "event_id": "EvDelete",
    "authorizations": [{"team_id": "T1", "user_id": "UBOT", "is_bot": True}],
    "event": {
        "type": "message", "subtype": "message_deleted", "hidden": True,
        "channel": "C1", "deleted_ts": "101.000001", "ts": "110.000001",
        "previous_message": {"user": "UBOT", "ts": "101.000001", "thread_ts": META["thread_id"]},
    },
}


async def connected_adapter(monkeypatch):
    # Real SDK classes are essential: old handler-only tests skipped self-event filtering.
    assert isinstance(AsyncApp, type)
    monkeypatch.setattr(AsyncWebClient, "api_call", AsyncMock(side_effect=AssertionError("unexpected network")))
    auth = AsyncMock(return_value=AsyncSlackResponse(
        client=None, http_verb="POST", api_url="https://slack.com/api/auth.test", req_args={},
        data={"ok": True, "team_id": "T1", "user_id": "UBOT", "bot_id": "BBOT"},
        headers={}, status_code=200))
    post = AsyncMock(return_value={"ok": True, "ts": "101.000001"})
    monkeypatch.setattr(AsyncWebClient, "auth_test", auth)
    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", post)
    monkeypatch.setattr(slack_module, "get_secret", lambda name: "xapp-fake")
    monkeypatch.setattr(slack_module, "AsyncApp", lambda **kwargs: AsyncApp(process_before_response=True, **kwargs))
    adapter = slack_module.SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    monkeypatch.setattr(adapter, "_acquire_platform_lock", Mock(return_value=True))
    monkeypatch.setattr(adapter, "_release_platform_lock", Mock())
    monkeypatch.setattr(adapter, "_start_socket_mode_handler", Mock())
    monkeypatch.setattr(adapter, "_ensure_socket_watchdog", Mock())
    monkeypatch.setattr(adapter, "_wire_plugin_handlers", Mock())
    monkeypatch.setattr(adapter, "stop_typing", AsyncMock())
    handler = AsyncMock(wraps=adapter._handle_slack_message)
    monkeypatch.setattr(adapter, "_handle_slack_message", handler)
    assert await adapter.connect()
    assert adapter._app is not None
    return adapter, post, auth, handler


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_author", ["UBOT", None])
async def test_real_bolt_deletion_reaches_thread_fence(monkeypatch, previous_author):
    adapter, post, auth, handler = await connected_adapter(monkeypatch)
    assert (await adapter.send("C1", "progress", metadata=META)).success
    body = copy.deepcopy(BODY)
    if previous_author is None:
        body["event"].pop("previous_message")
    response = await adapter._app.async_dispatch(AsyncBoltRequest(body=body, mode="socket_mode"))
    assert response.status == 200
    handler.assert_awaited_once()
    assert not (await adapter.send("C1", "resurrected", metadata=META)).success
    assert post.await_count == 1
    assert adapter._deletion_record(("T1", "C1", META["thread_id"]))["muted"]
    await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_kind", ["own_message", "own_mention", "invalid_auth", "other_author"])
async def test_deletion_exception_preserves_auth_and_self_echo_gates(monkeypatch, event_kind):
    adapter, post, auth, handler = await connected_adapter(monkeypatch)
    body = copy.deepcopy(BODY)
    if event_kind in ("own_message", "own_mention"):
        body["event"] = {"type": "message" if event_kind == "own_message" else "app_mention",
                         "user": "UBOT", "bot_id": "BBOT", "channel": "C1", "ts": "111.000001", "text": "echo"}
    elif event_kind == "invalid_auth":
        auth.side_effect = SlackApiError("invalid auth", {"ok": False, "error": "invalid_auth"})
    else:
        body["event"]["previous_message"]["user"] = "UOTHER"
    await adapter._app.async_dispatch(AsyncBoltRequest(body=body, mode="socket_mode"))
    if event_kind == "other_author":
        handler.assert_awaited_once()
    else:
        handler.assert_not_awaited()
    assert not adapter._deletion_record(("T1", "C1", META["thread_id"]))
    post.assert_not_awaited()
    await adapter.disconnect()
