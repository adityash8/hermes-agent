"""Historical bot-authored thread roots are context, never fresh turn authority.

The context helpers still recover root authorship across restarts; admission
requires a direct request, configured opt-in, or live requester task.
"""

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest


# Mock slack-bolt / slack-sdk the same way test_slack_mention.py does.
def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock
    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock
    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        (
            "slack_bolt.adapter.socket_mode.async_handler",
            slack_bolt.adapter.socket_mode.async_handler,
        ),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("aiohttp", MagicMock())


_ensure_slack_mock()

import plugins.platforms.slack.adapter as _slack_mod  # noqa: E402

_slack_mod.SLACK_AVAILABLE = True

from plugins.platforms.slack.adapter import (  # noqa: E402
    SlackAdapter,
    _ThreadContextCache,
)

from gateway.config import Platform, PlatformConfig  # noqa: E402


BOT_USER_ID = "U_BOT_OWN"
CHANNEL_ID = "C_incident"
USER_ID = "U_engineer"
THREAD_TS = "1700000000.000100"


def _make_adapter(bot_authored_root: bool = False):
    """Build a bare SlackAdapter with the wake-decision state controlled.

    None of the 3 legacy in-memory checks pass by default: the bot didn't
    send via gateway, the thread wasn't @-mentioned, and there is no active
    session — exactly the post-restart / outside-send state.
    """
    adapter = object.__new__(SlackAdapter)
    adapter.platform = Platform.SLACK
    adapter.config = PlatformConfig(
        enabled=True,
        extra={"require_mention": True, "strict_mention": False},
    )
    adapter._bot_user_id = BOT_USER_ID
    adapter._team_bot_user_ids = {}
    adapter._bot_message_ts = set()
    adapter._mentioned_threads = set()
    adapter._thread_context_cache = {}

    adapter._has_active_session_for_thread = lambda **kw: False
    # Mock _fetch_thread_context so the miss-path doesn't make a real
    # Slack API call. Tests that need a populated cache pre-populate
    # _thread_context_cache directly.
    adapter._fetch_thread_context = AsyncMock(return_value="")
    # The 4th-check helper is mocked so wake-decision tests can control
    # its result without setting up the full cache path. Helper-specific
    # tests call the real method via the class instead.
    adapter._bot_authored_thread_root = AsyncMock(return_value=bot_authored_root)

    return adapter


# ---------------------------------------------------------------------------
# _should_wake_on_unmentioned_message — composes all 4 checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wake_decision_returns_false_when_not_thread_reply():
    """A top-level channel message (no thread_ts) should never wake the bot
    when require_mention is true — unchanged by this fix."""
    adapter = _make_adapter(bot_authored_root=True)
    wake = await adapter._should_wake_on_unmentioned_message(
        event_thread_ts=None,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        is_thread_reply=False,
    )
    assert wake is False


@pytest.mark.asyncio
async def test_wake_decision_rejects_historical_bot_authored_thread_root():
    """A past bot post alone cannot admit a new unmentioned request."""
    adapter = _make_adapter(bot_authored_root=True)
    wake = await adapter._should_wake_on_unmentioned_message(
        event_thread_ts=THREAD_TS,
        channel_id=CHANNEL_ID,
        user_id=USER_ID,
        is_thread_reply=True,
    )
    assert wake is False
    adapter._bot_authored_thread_root.assert_not_awaited()


# ---------------------------------------------------------------------------
# _bot_authored_thread_root — the API-derived, restart-surviving check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bot_authored_thread_root_fetches_on_cache_miss():
    """Cache miss → _fetch_thread_context runs; a successful fetch that
    populates parent_user_id with the bot's id yields True. This is the
    restart path: fresh process, empty caches, root authorship recovered
    from the Slack API."""
    adapter = _make_adapter()

    async def _fake_fetch(channel_id, thread_ts, current_ts, team_id=""):
        adapter._thread_context_cache[f"{channel_id}:{thread_ts}:{team_id}"] = (
            _ThreadContextCache(
                content="ctx",
                fetched_at=0,
                message_count=1,
                parent_text="bot-posted root",
                parent_user_id=BOT_USER_ID,
            )
        )
        return "ctx"

    adapter._fetch_thread_context = AsyncMock(side_effect=_fake_fetch)

    result = await SlackAdapter._bot_authored_thread_root(
        adapter, CHANNEL_ID, THREAD_TS
    )
    assert result is True
    adapter._fetch_thread_context.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_authored_thread_root_uses_per_team_bot_id():
    """Multi-workspace: the comparison must use the team's bot user id,
    not the primary workspace's."""
    adapter = _make_adapter()
    adapter._team_bot_user_ids = {"T2": "U_BOT_T2"}
    adapter._thread_context_cache = {
        f"{CHANNEL_ID}:{THREAD_TS}:T2": _ThreadContextCache(
            content="ctx",
            fetched_at=0,
            message_count=1,
            parent_text="root",
            parent_user_id="U_BOT_T2",
        ),
    }

    result = await SlackAdapter._bot_authored_thread_root(
        adapter, CHANNEL_ID, THREAD_TS, team_id="T2"
    )
    assert result is True
    # And the primary bot id must NOT match in that workspace.
    adapter._thread_context_cache[f"{CHANNEL_ID}:{THREAD_TS}:T2"].parent_user_id = (
        BOT_USER_ID
    )
    result = await SlackAdapter._bot_authored_thread_root(
        adapter, CHANNEL_ID, THREAD_TS, team_id="T2"
    )
    assert result is False
