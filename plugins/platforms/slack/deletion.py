"""Exact-surface deletion fences for the Slack adapter.

Only observed deletions are actionable; Slack does not replay every event missed
while offline. State survives reconnects/session resets in the profile home. The
adapter's single-writer token lock also owns this atomic file. Tombstones are not
expired: only a newer, authorized direct mention reopens a surface.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import json
import logging
import re
import shutil
import time
from typing import TYPE_CHECKING, Any, Callable

from agent.async_utils import consume_detached_task_result
from gateway.platforms.base import BasePlatformAdapter, SendResult
from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)
_CREATING_METHODS = {"chat_postMessage", "chat_startStream", "chat.startStream"}
_delivery_scope: contextvars.ContextVar[tuple[Any, tuple[str, str, str], int] | None] = contextvars.ContextVar("slack_deletion_scope", default=None)


_scheduled_delivery: contextvars.ContextVar[bool] = contextvars.ContextVar("slack_scheduled_delivery", default=False)


def _sent_receipt_ok(key, surface) -> bool:
    """Receipts are written as three strings; anything else would break the tuple lookup later."""
    try:
        sent = json.loads(key)
    except (ValueError, RecursionError):
        return False
    return all(isinstance(part, list) and len(part) == 3 and all(isinstance(item, str) for item in part)
               for part in (sent, surface))


_RECORD_FIELDS = {"generation": int, "cutoff": str, "deleted": list}


def _salvage_record(record) -> tuple[dict, int]:
    """Reset wrong-typed fields to their missing-field defaults (0, "", []); dropping the record would unmute it."""
    clean = dict(record)
    wrong = [field for field, kind in _RECORD_FIELDS.items() if field in clean and type(clean[field]) is not kind]
    for field in wrong:
        clean[field] = _RECORD_FIELDS[field]()
    if "cutoff" in wrong and clean.get("muted"):
        # An empty cutoff would let a replayed pre-deletion mention reopen the thread; only newer ones may.
        clean["cutoff"] = f"{time.time():.6f}"
    return clean, len(wrong)


def _salvage_deletion_state(state) -> tuple[dict, int]:
    """Drop malformed entries but keep every valid fence; ValueError when there is no thread map at all."""
    if not isinstance(state, dict) or not isinstance(state.get("threads"), dict):
        raise ValueError("Invalid Slack deletion state")
    dropped = 0
    text = json.dumps(state, ensure_ascii=False)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate escape parses but can never be saved again, so every later fence would be memory-only.
        state, dropped = json.loads(text.encode("utf-8", "replace")), 1
    clean = {**state, "threads": {}}
    for key, record in state["threads"].items():
        if not isinstance(record, dict):
            dropped += 1
            continue
        clean["threads"][key], reset = _salvage_record(record)
        dropped += reset
    for part in ("cleanup", "sent"):
        if not isinstance(state.get(part, {}), dict):
            clean[part], dropped = {}, dropped + 1
    if "sent" in clean:
        receipts = {key: surface for key, surface in clean["sent"].items() if _sent_receipt_ok(key, surface)}
        dropped += len(clean["sent"]) - len(receipts)
        clean["sent"] = receipts
    return clean, dropped


def _corrupt_copy_path(path):
    return path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")


class DeletedSurfaceError(RuntimeError):
    def __init__(self):
        super().__init__("slack_thread_deleted")


def deletion_guard(method):
    """Keep one deletion epoch through nested sends, retries and transport awaits."""
    signature = inspect.signature(method)

    @functools.wraps(method)
    async def guarded(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        metadata = bound.arguments.get("metadata")
        token = scheduled_token = None
        try:
            chat = bound.arguments.get("chat_id", "")
            if str(chat).startswith(("U", "W")):
                # Check inherited revocation before opening a DM, then fence the
                # resolved conversation so its deletion epoch covers uploads too.
                self._check_deletion_scope()
                chat = await self._dm_target(chat, metadata)
                bound.arguments["chat_id"] = chat
            key = self._deletion_key(
                chat, self._resolve_thread_ts(bound.arguments.get("reply_to"), metadata), metadata)
            message_id = bound.arguments.get("message_id")
            if message_id:
                key = self._sent_surface(key, message_id)
            scope = _delivery_scope.get()
            if scope is None or scope[:2] != (self, key):
                scope = (self, key, self._deletion_generation(key))
            token = _delivery_scope.set(scope)
            scheduled_token = _scheduled_delivery.set(
                _scheduled_delivery.get() or bool(isinstance(metadata, dict) and metadata.get("job_id")))
            self._check_deletion_scope()
            if message_id and message_id in self._deletion_record(key).get("deleted", []):
                raise DeletedSurfaceError()
            result = await method(*bound.args, **bound.kwargs)
            self._check_deletion_scope()
            return result
        except DeletedSurfaceError as exc:
            if signature.return_annotation in (None, "None"):
                return None
            return SendResult(success=False, error=str(exc))
        finally:
            if scheduled_token is not None:
                _scheduled_delivery.reset(scheduled_token)
            if token is not None:
                _delivery_scope.reset(token)
    return guarded


class SlackDeletionMixin(BasePlatformAdapter):
    @staticmethod
    def _slack_self_event_filter():
        """Keep Bolt's echo protection, except for authenticated deletion events.

        Bolt extracts a deleted message's original author as context.user_id.
        Its default self-event filter therefore swallows deletions of our replies.
        Ownership and self-cleanup checks still run in _handle_message_deleted.
        Import lazily so adapter discovery does not require the optional SDK.
        """
        from slack_bolt.middleware.ignoring_self_events.async_ignoring_self_events import (
            AsyncIgnoringSelfEvents,
        )

        class DeletionAwareSelfEvents(AsyncIgnoringSelfEvents):
            async def async_process(self, *, req, resp, next):
                event = req.body.get("event") or {}
                if event.get("type") == "message" and event.get("subtype") == "message_deleted":
                    return await next()
                return await super().async_process(req=req, resp=resp, next=next)

        return DeletionAwareSelfEvents()

    # Implemented by SlackAdapter; declared here for the topical mixin's type contract.
    _channel_team: dict[str, str]
    _team_bot_user_ids: dict[str, str]
    _bot_user_id: str | None
    _BOT_TS_MAX: int
    _slack_sent_surfaces: dict[tuple[str, str, str], tuple[str, str, str]]
    _slack_deletion_cancellations: dict[tuple[str, str, str], asyncio.Task]
    if TYPE_CHECKING:
        _lazy_attr: Callable[..., Any]
        _metadata_team_id: Callable[..., str]
        _event_team_id: Callable[..., str]
        _trim_oldest_dict_entries: Callable[..., None]
        _is_interactive_user_authorized: Callable[..., bool]

    def _deletion_state(self):
        if not hasattr(self, "_slack_deletions"):
            self._slack_deletion_path = path = get_hermes_home() / "slack-deletions.json"
            dropped = 0
            try:
                state, dropped = _salvage_deletion_state(json.loads(path.read_text(encoding="utf-8")))
            except FileNotFoundError:
                state = {"threads": {}, "cleanup": {}}
            except (OSError, ValueError, RecursionError) as exc:  # RecursionError: absurdly nested JSON
                # EZ-853: a bad file must not crash every Slack event. Reopen loudly, once,
                # and keep the file; the next observed deletion re-fences its surface.
                state = {"threads": {}, "cleanup": {}}
                quarantine = _corrupt_copy_path(path)
                try:
                    path.rename(quarantine)
                    kept = f"moved to {quarantine.name}"
                except OSError:
                    kept = "could not move it aside"
                logger.error(
                    "[Slack] Unreadable deletion state %s (%s); %s. Starting empty: threads deleted "
                    "before now are not fenced until deleted again.", path, type(exc).__name__, kept)
            self._slack_deletions = state
            if dropped:
                # Copy the original aside, then persist the salvaged state so a restart keeps the fences.
                quarantine = _corrupt_copy_path(path)
                try:
                    shutil.copy2(path, quarantine)
                    kept = f"copied to {quarantine.name}"
                except OSError:
                    kept = "could not copy it aside"
                logger.error("[Slack] Malformed entries in deletion state %s; dropped or reset %d (%s). "
                             "Every well-formed fence is kept.", path, dropped, kept)
                self._save_deletions()
        return self._slack_deletions

    def _save_deletions(self):
        if hasattr(self, "_slack_sent_surfaces"):
            self._slack_deletions["sent"] = {
                json.dumps(key): surface for key, surface in self._slack_sent_surfaces.items()}
        try:
            atomic_json_write(self._slack_deletion_path, self._slack_deletions, mode=0o600)
        except (OSError, ValueError, RecursionError):
            # ValueError: an unencodable string; RecursionError: indent=2 uses the shallower pure-Python encoder.
            logger.exception("[Slack] Cannot persist deletion state; mute is memory-only until restart")

    def _deletion_messages(self):
        return self._lazy_attr("_slack_sent_surfaces", lambda: {
            tuple(json.loads(key)): tuple(surface)
            for key, surface in self._deletion_state().get("sent", {}).items()})

    def _authenticated_client_team(self, client):
        teams = [team for team, known in self._team_clients.items() if known is client]
        team = teams[0] if len(teams) == 1 else ""
        if not teams and client is not None:
            team = vars(client).get("_slack_authenticated_team_id", "")
        return team if isinstance(team, str) else ""

    def _deletion_key(self, chat_id, thread_ts=None, metadata=None):
        team = self._metadata_team_id(metadata)
        scope = _delivery_scope.get()
        if scope and scope[0] is self and scope[1][1] == chat_id:
            team = team or scope[1][0]
            thread_ts = thread_ts or scope[1][2]
        team = team or self._channel_team.get(chat_id, "")
        if not team and chat_id and getattr(self, "_app", None) is not None:
            # Bind the selected authenticated workspace before capturing an epoch,
            # so retries and upload children cannot retain a blank-team alias.
            team = self._authenticated_client_team(self._get_client(chat_id))
        return str(team), str(chat_id), str(thread_ts or "")

    def _deletion_record(self, key):
        return self._deletion_state()["threads"].get(json.dumps(key), {})

    def _deletion_generation(self, key):
        return self._deletion_record(key).get("generation", 0)

    def _check_deletion_scope(self):
        scope = _delivery_scope.get()
        if scope and scope[0] is self:
            record = self._deletion_record(scope[1])
            if record.get("muted") or record.get("generation", 0) != scope[2]:
                raise DeletedSurfaceError()

    def _remember_cleanup(self, key, message_id):
        cleanup = self._deletion_state().setdefault("cleanup", {})
        cleanup[json.dumps((key[0], key[1], message_id))] = True
        # Retain recent cleanup receipts across reconnect/restart redeliveries.
        while len(cleanup) > self._BOT_TS_MAX:
            cleanup.pop(next(iter(cleanup)))
        self._save_deletions()

    def _forget_cleanup(self, key, message_id):
        self._deletion_state().get("cleanup", {}).pop(json.dumps((key[0], key[1], message_id)), None)
        self._save_deletions()

    def _known_message_surface(self, key, message_id):
        receipts = self._deletion_messages()
        inbound = self._lazy_attr("_slack_inbound_surfaces", dict)
        known = receipts.get((key[0], key[1], message_id)) or inbound.get((key[0], key[1], message_id))
        if known is None and not key[0]:
            matches = {surface for registry in (receipts, inbound)
                       for (team, chat, ts), surface in registry.items()
                       if chat == key[1] and ts == message_id}
            if len(matches) == 1:
                known = matches.pop()
        return known

    def _sent_surface(self, key, message_id):
        return self._known_message_surface(key, message_id) or key

    def _is_cleanup(self, key, message_id):
        return json.dumps((key[0], key[1], message_id)) in self._deletion_state().get("cleanup", {})

    async def _silence_deleted_surface(self, key, deleted_ts, event_ts, *, wait=True):
        if not key[1] or not key[2]:
            return
        state = self._deletion_state()
        record = self._deletion_record(key)
        # Replays of the same delete cannot re-mute a subsequently summoned thread.
        if deleted_ts in record.get("deleted", []):
            return
        record = {**record, "muted": True, "generation": record.get("generation", 0) + 1,
                  "cutoff": max(str(event_ts), record.get("cutoff", "")),
                  "deleted": [*record.get("deleted", []), deleted_ts]}
        state["threads"][json.dumps(key)] = record
        self._save_deletions()
        logger.info("[Slack] Silenced deleted bot surface workspace=%s channel=%s thread=%s", *key)
        cancellations = self._lazy_attr("_slack_deletion_cancellations", dict)
        task = cancellations.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._cancel_deleted_surface(key))
            cancellations[key] = task
        if wait:
            await asyncio.shield(task)

    async def _cancel_deleted_surface(self, key):
        # Do not clear wake caches: the tombstone dominates them until a fresh summon.
        sources = self._lazy_attr("_slack_surface_sessions", dict)
        for session_key, source in list(sources.items()):
            if (source.scope_id or "", source.chat_id, source.thread_id or "") == key:
                try:
                    await self.request_session_cancellation(session_key, source)
                except Exception:
                    logger.exception("[Slack] Could not cancel deleted surface session")

    async def _handle_message_deleted(self, event, payload):
        previous = event.get("previous_message") or {}
        if not isinstance(previous, dict):
            return
        channel = str(event.get("channel") or "")
        team = self._event_team_id(event, payload) or self._channel_team.get(channel, "")
        deleted_ts = str(event.get("deleted_ts") or previous.get("ts") or "")
        message_key = (team, channel, deleted_ts)
        known = self._deletion_messages().get(message_key)
        bot_uid = self._team_bot_user_ids.get(team) if team else self._bot_user_id
        if not deleted_ts:
            return
        if not (known or (bot_uid and previous.get("user") == bot_uid)):
            if not previous.get("user"):
                # Socket Mode may beat the send receipt. Defer ownership proof until
                # an actual bot send returns this exact workspace/channel/message ID.
                pending = self._lazy_attr("_slack_unattributed_deletions", dict)
                pending[message_key] = event.get("event_ts") or event.get("ts") or f"{time.time():.6f}"
                self._trim_oldest_dict_entries(pending, self._BOT_TS_MAX)
            return
        key = known or (team, channel, str(previous.get("thread_ts") or deleted_ts))
        if self._is_cleanup(key, deleted_ts):
            return
        await self._silence_deleted_surface(
            key, deleted_ts, event.get("event_ts") or event.get("ts") or f"{time.time():.6f}")

    async def _deletion_accept_summon(self, key, event, user_id, is_dm, bot_uid, mention_text, *, source=None):
        pending = self._lazy_attr("_slack_deletion_cancellations", dict).get(key)
        if pending is not None:
            await asyncio.shield(pending)
            if self._slack_deletion_cancellations.get(key) is pending:
                self._slack_deletion_cancellations.pop(key, None)
        record = self._deletion_record(key)
        if not record.get("muted"):
            return True
        # Use the adapter's non-quoted Block Kit mention text, with flat mrkdwn
        # quotes/code removed. Patterns and edited old messages are not summons.
        direct_text = re.sub(r"```.*?```|`[^`\n]*`", "", mention_text, flags=re.DOTALL)
        direct_text = "\n".join(line for line in direct_text.splitlines()
                                if not line.lstrip().startswith(">"))
        if (not bot_uid or f"<@{bot_uid}>" not in direct_text
                or event.get("_slack_changed_event_ts") or event.get("_hermes_force_process")
                or str(event.get("ts") or "") <= record.get("cutoff", "")):
            return False
        if source is not None:
            if (getattr(source.platform, "value", source.platform) != "slack"
                    or (source.scope_id or "", source.chat_id, source.thread_id or "") != key
                    or source.user_id != user_id):
                return False
            authorized = self._is_interactive_user_authorized(user_id, source=source)
        else:
            authorized = self._is_sender_authorized(
                user_id, "dm" if is_dm else "group", key[1], thread_id=key[2])
        if authorized is not True:
            return False
        record.update(muted=False, generation=record.get("generation", 0) + 1)
        self._save_deletions()
        return True

    async def handle_message(self, event):
        source = event.source
        key = (source.scope_id or "", source.chat_id, source.thread_id or "")
        if self._deletion_record(key).get("muted"):
            return
        event.metadata["_slack_deletion_generation"] = self._deletion_generation(key)
        self._lazy_attr("_slack_surface_sessions", dict)[self._event_session_key(event)] = source
        await super().handle_message(event)

    async def _process_message_background(self, event, session_key):
        source = event.source
        key = (source.scope_id or "", source.chat_id, source.thread_id or "")
        generation = event.metadata.get("_slack_deletion_generation", self._deletion_generation(key))
        token = _delivery_scope.set((self, key, generation))
        try:
            self._check_deletion_scope()
            await super()._process_message_background(event, session_key)
        except DeletedSurfaceError:
            if self._session_tasks.get(session_key) is asyncio.current_task():
                self._release_session_guard(session_key)
        finally:
            _delivery_scope.reset(token)
            if session_key not in self._active_sessions:
                self._lazy_attr("_slack_surface_sessions", dict).pop(session_key, None)

    def _deletion_payload_key(self, method, payload, team_id=""):
        channel = payload.get("channel") or payload.get("channel_id") or ""
        key = self._deletion_key(channel, payload.get("thread_ts"), {"team_id": team_id})
        if method in _CREATING_METHODS | {
                "chat_postEphemeral", "files_upload_v2", "chat.postMessage",
                "chat.postEphemeral", "files.completeUploadExternal"}:
            # Missing thread_ts creates a root message, regardless of context.
            key = (key[0], key[1], str(payload.get("thread_ts") or ""))
        return self._sent_surface(key, str(payload.get("ts") or payload.get("timestamp") or ""))

    async def _deletion_call(self, client, method, *, team_id="", **kwargs):
        """Fence every Slack write, including fallback retries and completed late posts."""
        payload = kwargs.get("json", kwargs)
        scope = _delivery_scope.get()
        key = self._deletion_payload_key(method, payload, team_id)
        if scope is None or scope[:2] != (self, key):
            scope = (self, key, self._deletion_generation(key))
        token = _delivery_scope.set(scope)
        try:
            if method not in _CREATING_METHODS:
                return await self._deletion_write(client, method, **kwargs)
            # Keep the response receipt when owner cancellation races an accepted post.
            # The transport task retains this epoch and removes a late post itself.
            task = asyncio.create_task(self._deletion_write(client, method, **kwargs))
            writes = self._lazy_attr("_slack_deletion_writes", set)
            writes.add(task)
            task.add_done_callback(writes.discard)
            task.add_done_callback(consume_detached_task_result)
            return await asyncio.shield(task)
        finally:
            _delivery_scope.reset(token)

    async def _deletion_write(self, client, method, **kwargs):
        try:
            from .adapter import _slack_response_payload
        except ImportError:  # flat plugin-directory loading
            from adapter import _slack_response_payload  # ty: ignore[unresolved-import]
        self._check_deletion_scope()
        scope = _delivery_scope.get()
        payload = kwargs.get("json", kwargs)
        channel = payload.get("channel") or payload.get("channel_id") or ""
        key = scope[1] if scope and scope[0] is self else self._deletion_key(channel, payload.get("thread_ts"))
        message_id = str(payload.get("ts") or "")
        if message_id and message_id in self._deletion_record(key).get("deleted", []):
            raise DeletedSurfaceError()
        try:
            if method.startswith("chat."):
                result = await client.api_call(method, **kwargs)
            else:
                result = await getattr(client, method)(**kwargs)
            response = _slack_response_payload(result)
            if response.get("ok") is False:
                raise RuntimeError(response.get("error") or "Slack write failed")
        except Exception as exc:
            response = getattr(exc, "response", None)
            error = response.get("error") if hasattr(response, "get") else str(exc)
            if error == "message_not_found" and message_id and not self._is_cleanup(key, message_id):
                # An edit/stream target supplied by this adapter disappearing is deletion
                # evidence even when Socket Mode has not delivered its event yet.
                known = self._deletion_messages().get((key[0], key[1], message_id))
                known = known or (key if key[2] else None)
                if known:
                    # Do not await owner cancellation from a stream the owner awaits.
                    # This coroutine installs state before scheduling cancellation.
                    await self._silence_deleted_surface(
                        known, message_id, f"{time.time():.6f}", wait=False)
            raise
        ts = str(response.get("ts") or response.get("message_ts") or "")
        creates = method in _CREATING_METHODS
        if creates and ts:
            actual_key = key if key[2] else (key[0], key[1], ts)
            self._deletion_messages()[(key[0], key[1], ts)] = actual_key
            self._trim_oldest_dict_entries(self._deletion_messages(), self._BOT_TS_MAX)
            self._save_deletions()
            pending = self._lazy_attr("_slack_unattributed_deletions", dict).pop((key[0], key[1], ts), None)
            if pending and not self._is_cleanup(actual_key, ts):
                await self._silence_deleted_surface(actual_key, ts, pending, wait=False)
        try:
            self._check_deletion_scope()
        except DeletedSurfaceError:
            if creates and ts:
                self._remember_cleanup(key, ts)
                try:
                    await client.chat_delete(channel=channel, ts=ts)
                except Exception:
                    logger.exception("[Slack] Could not remove late post after deletion")
            raise
        return result
