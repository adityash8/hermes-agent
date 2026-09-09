"""Admission from authenticated Bolt fields, before Slack enrichment or state changes."""

import logging

from gateway.client_context import adapter_admission
from gateway.client_context_policy import identifier, require
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource

logger = logging.getLogger(__name__)


async def admit_event(adapter, event, body=None):
    if not adapter.client_context_enabled:
        return "legacy", None
    channel = None
    try:
        # The outer workspace wins. Never recover missing identity from assistant
        # caches, forwarded blocks, authorizations, user names or channel labels.
        envelope = event if body is None else body
        team = envelope.get("team_id") or envelope.get("team")
        if isinstance(team, dict):
            team = team.get("id")
        team = identifier(team)
        for key in ("team_id", "team"):
            if event.get(key):
                require(event[key] == team)
        channel = identifier(event.get("channel") or event.get("channel_id"))
        user = identifier(event.get("user") or event.get("user_id"))
        channel_type = event.get("channel_type")
        require(channel_type in {None, "", "im", "mpim", "channel", "group"})
        is_dm = channel.startswith("D") and channel_type in {None, "", "im"}
        require(channel_type != "im" or is_dm)
        source = SessionSource(
            Platform.SLACK, channel, scope_id=team, user_id=user,
            chat_type="dm" if is_dm else "group", thread_id=event.get("thread_ts"),
            is_bot=adapter._event_declares_bot_sender(event)
            or user in {adapter._bot_user_id, adapter._team_bot_user_ids.get(team)}
            or adapter._user_is_bot_cache.get((team, user), False),
        )
        mode = await adapter_admission(adapter, source)
        if mode == "deny":
            return mode, source  # adapter_admission already logged the reason
        if event.get("subtype") == "message_changed":
            # Normalization must not replace the admitted actor or destination.
            updated = event.get("message")
            require(isinstance(updated, dict) and updated.get("user") == user)
            for key, value in (("channel", channel), ("team", team), ("team_id", team),
                               ("channel_type", channel_type)):
                require(not updated.get(key) or updated[key] == value)
        # Keep adapter restrictions in addition to the gateway authorization check.
        if (adapter._is_ignored_channel(channel)
                or (channel_type in {"im", "mpim"} or is_dm) and adapter._slack_disable_dms()
                or adapter._early_reject_unauthorized(user, channel, is_dm)):
            logger.info("[Slack] client context denied by adapter restrictions in channel %s", channel)
            return "deny", source
        allowed = adapter._slack_allowed_channels()
        if not is_dm and allowed and channel not in allowed:
            logger.info("[Slack] client context denied: channel %s is not an allowed channel", channel)
            return "deny", source
        return mode, source
    except Exception:
        # channel is only set once it passed identifier(); never log raw event fields.
        logger.warning("[Slack] client context denied: event failed admission checks in channel %s", channel or "unknown")
        return "deny", None


async def interaction_is_legacy(adapter, body):
    if not adapter.client_context_enabled:
        return True
    # Only the actor/channel of the authenticated interaction, never action.value
    # or the embedded message author, can authorize an owner control.
    try:
        mode, _ = await admit_event(adapter, {
            "channel": body.get("channel", {}).get("id"),
            "user": body.get("user", {}).get("id"),
        }, body)
        return mode == "legacy"
    except (AttributeError, TypeError):
        return False


async def dispatch_scoped(adapter, event, source):
    # Edits/deletions lacking their own authenticated actor cannot borrow authorship
    # from previous_message. Current text is the only Q&A input.
    if event.get("subtype") not in {None, "", "file_share"}:
        return
    text = event.get("text", "")
    if not isinstance(text, str):
        return
    bot_uid = adapter._team_bot_user_ids.get(source.scope_id, adapter._bot_user_id)
    is_mentioned = bool(bot_uid and f"<@{bot_uid}>" in text)
    is_mentioned = is_mentioned or adapter._slack_message_matches_mention_patterns(text)
    ts = event.get("ts", "")
    if source.chat_type != "dm" and not await adapter._channel_gate_allows(
        channel_id=source.chat_id, routing_text=text, bot_uid=bot_uid,
        is_mentioned=is_mentioned, is_thread_reply=bool(source.thread_id and source.thread_id != ts),
        event_thread_ts=source.thread_id, user_id=source.user_id, team_id=source.scope_id,
        is_dm=False, force_process=False, allow_thread_lookup=False,
    ):
        return
    if ts and adapter._dedup.is_duplicate(adapter._workspace_event_id(source.scope_id, ts)):
        return
    if bot_uid:
        text = text.replace(f"<@{bot_uid}>", "").strip()
    source.thread_id = adapter._session_thread_ts(event, ts, source.chat_type == "dm", {})
    trigger = MessageEvent(
        text=text, source=source, message_id=ts,
        message_type=MessageType.DOCUMENT if event.get("files") else MessageType.TEXT,
        metadata={"slack_team_id": source.scope_id, "slack_channel_id": source.chat_id,
                  "slack_thread_ts": source.thread_id},
    )
    trigger._client_context_required = True
    await adapter.handle_message(trigger)
