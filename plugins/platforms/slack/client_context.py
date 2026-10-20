"""Admission from authenticated Bolt fields, before Slack enrichment or state changes."""

from copy import copy
from types import SimpleNamespace
import asyncio
import logging

from gateway.client_context import adapter_admission
from gateway.client_context_policy import identifier, require, slack_id
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource

logger = logging.getLogger(__name__)


async def admit_event(adapter, event, body=None, *, original_source=None, interaction=False):
    from gateway import client_context_restricted as restricted
    probe = SimpleNamespace(platform=Platform.SLACK,
                            chat_id=event.get("channel") or event.get("channel_id"),
                            user_id=event.get("user") or event.get("user_id"))
    if not is_codewords(adapter, probe) and any(
        isinstance(event.get(key), dict) and event[key].get("user") == restricted.USER
        for key in ("message", "previous_message")
    ):
        return "deny", None  # Nested authorship may deny an edit, never authorize a turn.
    if not adapter.client_context_enabled and not restricted.selected(probe):
        return "legacy", None
    try:
        # Select by the native destination before parsing identity. A bounded
        # policy must not make unrelated legacy events fail new identity checks.
        applies = getattr(adapter, "_client_context_applies", None)
        if applies is not None and not applies(probe) and not restricted.selected(probe):
            return "legacy", None
        codewords = is_codewords(adapter, probe)
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
        channel = slack_id(event.get("channel") or event.get("channel_id"))
        if codewords and event.get("subtype") in {"message_changed", "message_deleted"}:
            # Authenticated lifecycle evidence can revoke reads without supplying
            # an actor capable of starting a request or broadening the grant.
            from gateway.client_context_codewords import CHANNEL, WORKSPACE
            from gateway.client_context_codewords_history import slack_timestamp

            require(body is not None and event.get("type") == "message")
            require(team == WORKSPACE and channel == CHANNEL and team in adapter._team_clients
                    and bool(adapter._team_bot_user_ids.get(team)))
            slack_timestamp(event.get("event_ts") or event.get("ts"))
            changed = event.get("message") if event["subtype"] == "message_changed" else event.get("previous_message", {})
            require(type(changed) is dict and changed.get("channel", channel) == channel)
            changed_ts = slack_timestamp(changed.get("ts") if event["subtype"] == "message_changed" else event.get("deleted_ts"))
            require(not changed.get("ts") or changed["ts"] == changed_ts)
            if changed.get("thread_ts"):
                slack_timestamp(changed["thread_ts"])
            version = ""
            if event["subtype"] == "message_changed":
                edited = changed.get("edited", {})
                require(type(edited) is dict)
                version = slack_timestamp(edited.get("ts") or event.get("event_ts") or event.get("ts"))
            else:
                # Persisted deletion receipts survive reconnects and reopening.
                # A replay must not revoke the newer explicitly summoned turn.
                surface = adapter._sent_surface((team, channel, changed.get("thread_ts") or changed_ts), changed_ts)
                if changed_ts in adapter._deletion_record(surface).get("deleted", []):
                    return "deny", None
            changes = vars(adapter).get("_codewords_history_changes", {})
            require(type(changes) is dict)
            receipt = (team, channel, event["subtype"], changed_ts, version)
            if receipt in changes:
                return "deny", None
            revision = vars(adapter).get("_codewords_history_revision", 0)
            require(type(revision) is int and revision >= 0)
            adapter._codewords_history_revision = revision + 1
            if event["subtype"] == "message_deleted" and not adapter._is_ignored_channel(channel):
                await adapter._handle_message_deleted(event, body)
            changes[receipt] = True
            adapter._trim_oldest_dict_entries(changes, 512)
            adapter._codewords_history_changes = changes
            return "deny", None
        user = slack_id(event.get("user") or event.get("user_id"))
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
        from gateway.client_context_cookunity import EXTRA_USERS, CHANNEL as COOKUNITY_CHANNEL
        require(user not in EXTRA_USERS or channel == COOKUNITY_CHANNEL)
        if codewords:
            from gateway.client_context_codewords import CHANNEL, WORKSPACE, native_identity

            require(body is not None and not is_dm)
            require(team == WORKSPACE and channel == CHANNEL)
            require(interaction or channel_type in {"channel", "group"}
                    or channel_type in {None, ""} and event.get("type") == "app_mention")
            require(team in adapter._team_clients and bool(adapter._team_bot_user_ids.get(team)))
            require(not source.is_bot and event.get("subtype") in {None, ""})
            require(not event.get("files") and not event.get("attachments"))
            require(interaction or isinstance(event.get("text"), str) and bool(event["text"].strip()))
            # users.info is identity verification only. Do not use the adapter's
            # ordinary bot cache: its network-error fallback treats users as human.
            slack_client = adapter._team_clients[team]
            try:
                payload = await slack_client.users_info(user=user)
            except Exception:
                payload = await slack_client.users_info(user=user)
            require(payload.get("ok") is True)
            member = payload.get("user")
            require(isinstance(member, dict) and member.get("id") == user)
            # Slack Connect members can have a different home workspace. The
            # authenticated event and verified client bind the channel workspace.
            require(member.get("is_bot") is False)
            require(not member.get("is_workflow_bot") and not member.get("deleted"))
            source._codewords_native_identity = native_identity(source)
            policy_reader = getattr(adapter, "_client_context_codewords_policy", None)
            policy = policy_reader(source) if policy_reader is not None else None
            if policy is not None and policy.operator_user_id == user:
                from gateway.client_context_codewords_owner import issue
                from gateway.client_context_codewords_history import slack_timestamp
                require(not event.get("bot_id") and not event.get("bot_profile"))
                if interaction:
                    require(body.get("type") == "block_actions")
                    source.thread_id = (original_source.thread_id if original_source is not None
                                        else body.get("message", {}).get("thread_ts"))
                else:
                    require(event.get("type") in {"message", "app_mention"})
                    slack_timestamp(event.get("ts"))
                    source.thread_id = adapter._session_thread_ts(event, event["ts"], False, {})
                    slack_timestamp(source.thread_id)
                issue(adapter, source, policy, member, event.get("ts"))
            elif interaction:
                return "deny", None
        elif restricted.selected(source):
            require(original_source is None)
            await restricted.admit_native(adapter, event, body, source)
        if original_source is not None:
            require((original_source.platform, original_source.scope_id, original_source.chat_id,
                     original_source.user_id) == (Platform.SLACK, team, channel, user))
        if restricted.selected(source):
            # CookUnity must keep its fresh native receipt, never a legacy source.
            pass
        elif original_source is not None and not hasattr(source, "_codewords_owner"):
            source = copy(original_source)
        mode = await adapter_admission(adapter, source)
        if mode == "deny":
            return mode, source
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
                or not codewords and adapter._early_reject_unauthorized(user, channel, is_dm, source=source)):
            return "deny", source
        allowed = adapter._slack_allowed_channels()
        if not is_dm and allowed and channel not in allowed:
            return "deny", source
        return mode, source
    except Exception as error:
        channel = ts = None
        if isinstance(event, dict):
            channel = event.get("channel") or event.get("channel_id")
            ts = event.get("ts")
        logger.warning(
            "slack client_context admission denied channel=%s ts=%s error=%s",
            channel, ts, type(error).__name__,
        )
        return "deny", None


def is_codewords(adapter, source):
    selector = getattr(adapter, "_client_context_codewords", None)
    return selector is not None and selector(source) is True


async def send_codewords(adapter, chat_id, content, reply_to, metadata):
    """One exact fenced Slack text post; no mentions, automatic links or unfurls."""
    from gateway.client_context import ScopedReply
    from gateway.platforms.base import SendResult
    try:
        from .participation import _codewords_release
    except ImportError:
        from participation import _codewords_release

    validate = getattr(content, "_codewords_validate", None)
    if not isinstance(content, ScopedReply) or not callable(validate):
        return SendResult(success=False, error="codewords_release_denied")
    token = None
    try:
        validate()
        payload = {"channel": chat_id, "text": str(content), "mrkdwn": True,
                   "parse": "none", "link_names": False,
                   "unfurl_links": False, "unfurl_media": False}
        thread = adapter._resolve_thread_ts(reply_to, metadata)
        if thread:
            payload["thread_ts"] = thread
        client = adapter._client_for(chat_id, metadata)
        try:
            from .codewords_upload import send as send_chart
        except ImportError:
            from codewords_upload import send as send_chart  # ty: ignore[unresolved-import]
        chart_result, fallback = await send_chart(adapter, content, chat_id, thread, client)
        if chart_result is not None:
            return chart_result
        if fallback is not None:
            payload["text"] = fallback
        token = _codewords_release.set((adapter, validate, dict(payload), asyncio.current_task(),
                                       client, client.token,
                                       adapter._team_bot_user_ids.get(adapter._metadata_team_id(metadata))))
        adapter._check_deletion_scope(api_method="chat.postMessage", payload=payload, sdk_client=client)
        # Keep the write on its admitted task. The ordinary helper deliberately
        # forks shielded writes, which would authorize deferred target output.
        result = await adapter._deletion_write(client, "chat_postMessage", **payload)
        return SendResult(success=True, message_id=result.get("ts"), raw_response=result)
    except Exception:
        return SendResult(success=False, error="codewords_release_denied")
    finally:
        if token is not None:
            _codewords_release.reset(token)


async def interaction_is_legacy(adapter, body, *, original_source=None, admitted_sources=None):
    from gateway.client_context_restricted import USER
    from gateway.client_context_cookunity import EXTRA_USERS, CHANNEL as COOKUNITY_CHANNEL
    if (body.get("user", {}).get("id") in EXTRA_USERS
            or body.get("channel", {}).get("id") == COOKUNITY_CHANNEL):
        return False
    if not adapter.client_context_enabled and body.get("user", {}).get("id") != USER:
        return True
    # Only the actor/channel of the authenticated interaction, never action.value
    # or the embedded message author, can authorize an owner control.
    try:
        mode, admitted = await admit_event(adapter, {
            "channel": body.get("channel", {}).get("id"),
            "user": body.get("user", {}).get("id"),
        }, body, original_source=original_source, interaction=True)
        if mode == "legacy" and admitted_sources is not None and admitted is not None:
            admitted_sources.append(admitted)
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
    codewords = is_codewords(adapter, source)
    from gateway import client_context_restricted as restricted
    restricted_turn = not codewords and restricted.selected(source)
    routing_text = adapter._participation_text(text)
    bot_uid = adapter._team_bot_user_ids.get(source.scope_id, adapter._bot_user_id)
    native_summon = bool(bot_uid and f"<@{bot_uid}>" in routing_text)
    is_mentioned = native_summon or adapter._slack_message_matches_mention_patterns(routing_text)
    ts = event.get("ts", "")
    followup = None
    if codewords:
        from gateway.client_context_codewords import native_identity
        from gateway.client_context_codewords_history import slack_timestamp
        try:
            from . import codewords_followups
        except ImportError:
            import codewords_followups  # ty: ignore[unresolved-import]

        try:
            slack_timestamp(ts)
            source.thread_id = adapter._session_thread_ts(event, ts, False, {})
            slack_timestamp(source.thread_id)
        except ValueError:
            return
        source._codewords_native_identity = native_identity(source)
        self_uids = {uid for uid in (bot_uid, adapter._bot_user_id) if uid}
        if (not native_summon
                and adapter._slack_message_addressed_to_other_user(routing_text, self_uids)):
            return
        control = routing_text.replace(f"<@{bot_uid}>", "").strip().casefold()
        if adapter._silence_requested(routing_text) or control in {"stop", "/stop"}:
            if adapter._is_interactive_user_authorized(source.user_id, source=source) is True:
                codewords_followups.revoke(adapter, source, ts)
                await adapter._revoke_requester_for_no_reply(
                    event, source.scope_id, source.chat_id, source=source)
            return
        if adapter._participation_chatter(routing_text):
            return
        if not native_summon:
            followup = codewords_followups.continuation(adapter, event, source)
            if followup is None:
                return
        question = text.replace(f"<@{bot_uid}>", "").strip() if bot_uid else text.strip()
        if not question or question.startswith(("/", "!")):
            return
    elif restricted_turn:
        # Exact native summon only; ordinary acknowledgements stay silent.
        if not native_summon or adapter._silence_requested(routing_text):
            return
        current = routing_text.replace(f"<@{bot_uid}>", "").strip()
        bare = routing_text.strip() == f"<@{bot_uid}>"
        if not bare and (adapter._participation_chatter(routing_text)
                         or not current or current.startswith(("/", "!"))):
            return
        try:
            text = await restricted.thread_question(adapter, event, source, text, bare)
        except Exception:
            return
    elif not await adapter._response_needed(event, source.scope_id, source.chat_id, text, scoped=True):
        return
    if not followup and source.chat_type != "dm" and not await adapter._channel_gate_allows(
        channel_id=source.chat_id, routing_text=routing_text, bot_uid=bot_uid,
        is_mentioned=is_mentioned, is_thread_reply=bool(source.thread_id and source.thread_id != ts),
        event_thread_ts=source.thread_id, user_id=source.user_id, team_id=source.scope_id,
        is_dm=False, force_process=False, allow_thread_lookup=False, source=source,
    ):
        return
    if adapter._claim_inbound_event_dedup(source.scope_id or "", source.chat_id, ts):
        return
    if bot_uid:
        text = text.replace(f"<@{bot_uid}>", "").strip()
    source.thread_id = adapter._session_thread_ts(event, ts, source.chat_type == "dm", {})
    if codewords:
        from gateway.client_context_codewords import native_identity

        source._codewords_native_identity = native_identity(source)
        if not await adapter._deletion_accept_summon(
            (source.scope_id, source.chat_id, source.thread_id or ""), event,
            source.user_id, False, bot_uid, routing_text, source=source,
        ):
            return
    trigger = MessageEvent(
        text=text, source=source, message_id=ts,
        message_type=MessageType.DOCUMENT if event.get("files") else MessageType.TEXT,
        metadata={"slack_team_id": source.scope_id, "slack_channel_id": source.chat_id,
                  "slack_thread_ts": source.thread_id,
                  "_slack_native_ingress": True, "_slack_direct_request": True},
    )
    trigger._client_context_required = True
    if codewords:
        from gateway.client_context_codewords import seal_event

        trigger._codewords_request = seal_event(trigger)
        trigger.metadata["_codewords_summon"] = native_summon
        if followup is not None:
            setattr(trigger, "_codewords_followup", followup)
    elif restricted_turn:
        trigger._restricted_request = restricted.seal_event(trigger)
        # Skip Slack's operator follow-up queue/control handlers. Base admission
        # has a separate key and drops busy restricted messages before merging.
        if source.chat_type != "dm":
            surface = (source.scope_id, source.chat_id, source.thread_id or "")
            adapter._admit_event(trigger, adapter._deletion_generation(surface))
        from gateway.platforms.base import BasePlatformAdapter
        await BasePlatformAdapter.handle_message(adapter, trigger)
        return
    await adapter.handle_message(trigger)
