"""Slack's documented deletion envelope need not include previous_message."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


@pytest.fixture
def surface():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._app = MagicMock()
    adapter._bot_user_id = "UBOT"
    adapter._team_bot_user_ids = {"T1": "UBOT"}
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "101.000001"}
    client.chat_delete.return_value = {"ok": True}
    adapter._get_client = MagicMock(return_value=client)
    adapter.stop_typing = AsyncMock()
    return adapter, client, {"team_id": "T1", "thread_id": "100.000001"}


def envelope(channel="C1"):
    # Mirrors https://docs.slack.dev/reference/events/message/message_deleted/.
    event = {"type": "message", "subtype": "message_deleted", "hidden": True,
             "channel": channel, "ts": "110.000001", "deleted_ts": "101.000001"}
    return event, {"team_id": "T1", "event": event, "type": "event_callback",
                   "event_id": "EvDeletion"}


@pytest.mark.asyncio
async def test_documented_minimal_event_silences_known_own_reply(surface):
    adapter, client, metadata = surface
    assert (await adapter.send("C1", "first", metadata=metadata)).success
    event, payload = envelope()
    await adapter._handle_slack_message(event, payload)
    result = await adapter.send("C1", "must not resurrect", metadata=metadata)
    assert not result.success
    assert client.chat_postMessage.await_count == 1


@pytest.mark.asyncio
async def test_identical_timestamp_in_other_channel_does_not_silence(surface):
    adapter, client, metadata = surface
    assert (await adapter.send("C1", "first", metadata=metadata)).success
    event, payload = envelope("C2")
    await adapter._handle_slack_message(event, payload)
    assert (await adapter.send("C1", "still allowed", metadata=metadata)).success
    assert client.chat_postMessage.await_count == 2


@pytest.mark.asyncio
async def test_own_cleanup_minimal_event_does_not_silence(surface):
    adapter, client, metadata = surface
    assert (await adapter.send("C1", "temporary", metadata=metadata)).success
    assert await adapter.delete_message("C1", "101.000001")
    event, payload = envelope()
    await adapter._handle_slack_message(event, payload)
    assert (await adapter.send("C1", "final", metadata=metadata)).success
    assert client.chat_postMessage.await_count == 2


@pytest.mark.asyncio
async def test_deletion_bypasses_normal_message_dedup(surface):
    adapter, client, metadata = surface
    await adapter.send("C1", "reply", metadata=metadata)
    event, payload = envelope()
    adapter._dedup.is_duplicate(adapter._workspace_event_id("T1", event["ts"]))
    await adapter._handle_slack_message(event, payload)
    assert not (await adapter.send("C1", "resurrected", metadata=metadata)).success
    assert client.chat_postMessage.await_count == 1
