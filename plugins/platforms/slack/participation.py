"""Native Slack task permission, independent of historical conversation membership.

Channel outputs require a task grant or explicit scheduled-delivery metadata.
Standalone SDK/tool clients are outside this adapter's authority.
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from dataclasses import dataclass, field
from copy import copy
from uuid import uuid4
from itertools import count
from weakref import WeakValueDictionary
from types import SimpleNamespace
from typing import NoReturn

from tools.interrupt import reset_tool_execution_check, set_tool_execution_check

logger = logging.getLogger(__name__)

try:
    from .deletion import SlackDeletionMixin, DeletedSurfaceError, _delivery_scope, _scheduled_delivery
except ImportError:  # flat plugin-directory loading
    from deletion import SlackDeletionMixin, DeletedSurfaceError, _delivery_scope, _scheduled_delivery


@dataclass
class _Participation:
    adapter: object
    key: tuple[str, str, str]
    requester: str
    session_key: str
    generation: int
    task: asyncio.Task | None = None
    live: bool = True
    parent: _Participation | None = None
    approval_id: str | None = None
    request_ts: str = ""
    revoked: bool = False
    source: object | None = None
    root_messages: set[str] = field(default_factory=set)


_participation_scope: ContextVar[_Participation | None] = ContextVar(
    "slack_participation", default=None)
_codewords_release: ContextVar[tuple | None] = ContextVar("slack_codewords_release", default=None)
_codewords_history_read: ContextVar[tuple | None] = ContextVar("slack_codewords_history_read", default=None)
_cookunity_history_read: ContextVar[tuple | None] = ContextVar("slack_cookunity_history_read", default=None)


def _bind_participation(grant):
    return (_participation_scope.set(grant), set_tool_execution_check(
        lambda: grant.adapter._participation_valid(grant)
        and not grant.adapter._codewords_surface(grant.key)))


def _reset_participation(tokens):
    participation, execution = tokens
    reset_tool_execution_check(execution)
    _participation_scope.reset(participation)


class ParticipationDenied(DeletedSurfaceError):
    def __init__(self):
        RuntimeError.__init__(self, "slack_participation_revoked")


def participation_interaction(kind):
    """Bind a button callback to its recorded requester and actual pending prompt."""
    def decorate(method):
        @wraps(method)
        async def wrapped(self, ack, body, action):
            team = self._event_team_id({}, body)
            channel = (body.get("channel") or {}).get("id", "")
            if channel.startswith("D"):
                return await method(self, ack, body, action)
            user = (body.get("user") or {}).get("id", "")
            ts = (body.get("message") or {}).get("ts", "")
            grant = self._interaction_participation(
                team, channel, ts, user, kind, action.get("value", ""))
            if grant is None:
                await ack()
                return
            grant.task = asyncio.current_task()
            callbacks = self._lazy_attr("_slack_callback_participations", dict)
            identity = uuid4().hex
            callbacks[identity] = grant
            token = _bind_participation(grant)
            deletion_token = _delivery_scope.set((self, grant.key, grant.generation))
            try:
                self._check_deletion_scope()
                return await method(self, ack, body, action)
            except DeletedSurfaceError:
                return
            finally:
                grant.live = False
                callbacks.pop(identity, None)
                _delivery_scope.reset(deletion_token)
                _reset_participation(token)
        return wrapped
    return decorate


def fenced_slack_client(client_type, check, **kwargs):
    """Fence each stage on the constructed client, preserving SDK/factory identity.

    Bound methods belong to this client only; concurrent callers keep their own
    ContextVar permission even when the SDK invokes another upload stage.
    """
    client = client_type(**kwargs)
    api_call = getattr(client, "api_call", None)
    if callable(api_call):
        api_call = vars(api_call).get("_slack_unfenced_method", api_call)

        @wraps(api_call)
        async def checked_api_call(*args, **kwargs):
            try:
                from .codewords_upload import check_sdk_call
            except ImportError:
                from codewords_upload import check_sdk_call  # ty: ignore[unresolved-import]
            check_sdk_call(client, args, kwargs)
            # Deletion is cleanup, including removal of a post accepted late.
            api_method = args[0] if args else kwargs.get("api_method")
            if api_method != "chat.delete":
                check(api_method=api_method, sdk_client=client, payload=(
                    kwargs.get("json") or kwargs.get("data") or kwargs.get("params") or {}))
            return await api_call(*args, **kwargs)

        checked_api_call._slack_unfenced_method = api_call
        client.api_call = checked_api_call

    upload_file = getattr(client, "_upload_file", None)
    if callable(upload_file):
        upload_file = vars(upload_file).get("_slack_unfenced_method", upload_file)

        @wraps(upload_file)
        async def checked_upload_file(*args, **kwargs):
            check(api_method="files.uploadBytes", sdk_client=client)
            return await upload_file(*args, **kwargs)

        checked_upload_file._slack_unfenced_method = upload_file
        client._upload_file = checked_upload_file
    return client


class SlackParticipationMixin(SlackDeletionMixin):
    def _prepare_codewords_chart(self, event, reply, chart, validate):
        try:
            from .codewords_upload import prepare
        except ImportError:
            from codewords_upload import prepare  # ty: ignore[unresolved-import]
        prepare(self, event, reply, chart, validate)

    @contextmanager
    def _codewords_history_read_scope(self, client, method, payload, validate):
        """Permit one exact approved-channel read on its own task."""
        from gateway.client_context_codewords import CHANNELS, WORKSPACE

        grant = _participation_scope.get()
        allowed = {"channel", "ts", "limit", "oldest", "latest", "inclusive", "cursor"}
        if (grant is None or grant.adapter is not self or grant.key[0] != WORKSPACE
                or grant.key[1] not in CHANNELS or not self._codewords_surface(grant.key)
                or not self._participation_valid(grant)
                or self._authenticated_client_team(client) != WORKSPACE
                or method not in {"conversations.replies", "conversations.history"}
                or payload.get("channel") not in CHANNELS or set(payload) - allowed
                or type(payload.get("limit")) is not int or not 0 < payload["limit"] <= 15
                or method == "conversations.replies" and not isinstance(payload.get("ts"), str)):
            self._deny_participation()
        validate()
        token = _codewords_history_read.set((self, client, method, dict(payload), validate,
                                             asyncio.current_task(), grant))
        try:
            yield
            validate()
        finally:
            _codewords_history_read.reset(token)

    @contextmanager
    def _cookunity_history_read_scope(self, source, client, method, payload, validate):
        """Permit bounded reads only for this task's live CookUnity native grant."""
        from gateway.client_context_cookunity import CHANNEL, WORKSPACE
        from gateway.client_context_restricted import identity, native

        grant = _participation_scope.get()
        try:
            receipt = native(source, self)
            grant_source_receipt = native(grant.source, self) if grant is not None else None
        except Exception:
            self._deny_participation()
        expected_key = (WORKSPACE, CHANNEL, source.thread_id or "")
        allowed = {"channel", "ts", "limit", "oldest", "latest", "inclusive", "cursor"}
        if (grant is None or grant.adapter is not self or grant.key != expected_key
                or grant.requester != source.user_id or grant.source is None
                or grant_source_receipt is not receipt or receipt.client is not client
                or self._authenticated_client_team(client) != WORKSPACE
                or method not in {"conversations.replies", "conversations.history"}
                or not isinstance(payload, dict) or payload.get("channel") != CHANNEL
                or set(payload) - allowed or type(payload.get("limit")) is not int
                or not 0 < payload["limit"] <= 15
                or method == "conversations.replies" and not isinstance(payload.get("ts"), str)
                or grant.task is None or grant.task.done()
                or self._session_tasks.get(grant.session_key) is not grant.task
                or not self._participation_valid(grant)):
            self._deny_participation()
        validate()
        read = (self, source, client, method, dict(payload), validate, asyncio.current_task(),
                grant, receipt, grant.task, grant.key, grant.requester, grant.generation,
                identity(source))
        token = _cookunity_history_read.set(read)
        try:
            yield
        finally:
            try:
                self._validate_cookunity_history_read(read)
            finally:
                _cookunity_history_read.reset(token)

    def _validate_cookunity_history_read(self, read):
        from gateway.client_context_cookunity import CHANNEL, WORKSPACE
        from gateway.client_context_restricted import identity, native

        (adapter, source, client, method, payload, validate, task, grant, receipt,
         grant_task, key, requester, generation, source_identity) = read
        allowed = {"channel", "ts", "limit", "oldest", "latest", "inclusive", "cursor"}
        if (adapter is not self or _participation_scope.get() is not grant
                or asyncio.current_task() is not task or grant.adapter is not self
                or grant.key != key or key != (WORKSPACE, CHANNEL, source.thread_id or "")
                or grant.requester != requester or requester != source.user_id
                or grant.task is not grant_task or grant_task is None or grant_task.done()
                or self._session_tasks.get(grant.session_key) is not grant_task
                or grant.generation != generation or grant.source is None
                or identity(source) != source_identity or identity(grant.source) != source_identity
                or receipt.client is not client or method not in {"conversations.replies", "conversations.history"}
                or not isinstance(payload, dict) or payload.get("channel") != CHANNEL
                or set(payload) - allowed or type(payload.get("limit")) is not int
                or not 0 < payload["limit"] <= 15
                or method == "conversations.replies" and not isinstance(payload.get("ts"), str)
                or self._authenticated_client_team(client) != WORKSPACE
                or not self._participation_valid(grant)):
            self._deny_participation()
        try:
            if native(source, self) is not receipt or native(grant.source, self) is not receipt:
                self._deny_participation()
            validate()
        except Exception:
            self._deny_participation()

    def _codewords_owner_grant(self, grant):
        check = getattr(self, "_client_context_codewords_owner", None)
        return (grant is not None and grant.adapter is self and check is not None
                and check(grant.source) is True)

    def _codewords_surface(self, key):
        selector = getattr(self, "_client_context_codewords", None)
        grant = _participation_scope.get()
        if (grant is not None and grant.adapter is self and grant.key[:2] == key[:2]
                and self._codewords_owner_grant(grant)):
            return False
        return selector is not None and selector(SimpleNamespace(
            platform=self.platform, scope_id=key[0], chat_id=key[1])) is True

    def _codewords_channel_allowed(self, key):
        from gateway.client_context_codewords import CHANNELS, WORKSPACE

        allowed = self._slack_allowed_channels()
        return (key[0] == WORKSPACE and key[1] in CHANNELS and self._codewords_surface(key)
                and not self._is_ignored_channel(key[1])
                and (not allowed or key[1] in allowed))

    @staticmethod
    def _participation_text(text):
        """Only the sender's own prose can address or silence this task."""
        # A punctuation boundary cannot manufacture mentions or control phrases
        # across omitted material, and still counts as chatter when it stands alone.
        text = "\n".join("." if line.lstrip().startswith(">") else line
                         for line in (text or "").splitlines())
        prose, delimiter, start = [], None, 0
        for match in re.finditer(r"`+", text):
            if delimiter is None:
                prose.extend((text[start:match.start()], "."))
                delimiter = match.group()
            elif match.group() == delimiter:
                delimiter = None
                start = match.end()
        # An unmatched opener leaves the entire remaining literal untrusted.
        if delimiter is None:
            prose.append(text[start:])
        return re.sub(r"<@([A-Z0-9_]+)\|[^>\n]*>", r"<@\1>", "".join(prose))

    @staticmethod
    def _silence_requested(prose):
        # The caller supplies sender prose already sanitized exactly once.
        text = prose.replace("’", "'")
        # Explicit negative instructions take precedence over mentions and channel opt-in.
        return bool(re.search(
            r"\b(?:no\s+(?:reply|response|answer)\s+(?:needed|required|necessary)"
            r"|(?:do\s+not|don't)\s+(?:reply|respond|answer)"
            r"|(?:need\s+not|no\s+need\s+to)\s+(?:reply|respond|answer))\b",
            text, flags=re.IGNORECASE))

    @staticmethod
    def _participation_chatter(prose):
        text = re.sub(r"<@[A-Z0-9_]+>", "", prose).strip()
        text = text.strip(".,!?:; ").casefold()
        # Acknowledgements and informational notices are not task continuation.
        return (not text or text in {"thanks", "thank you", "thx", "ok", "okay", "got it",
                                     "ack", "acknowledged", "noted", "great", "nice", "👍", "🙏"}
                or bool(re.fullmatch(
                    r"(?:thanks|thank you)(?:,?\s+that answers it|\s+for the update)", text))
                or (bool(re.match(r"^(?:just\s+)?(?:fyi|for your information)\b", text))
                    and not re.search(
                        r"\b(?:please\s+|(?:can|could|would|will)\s+you\b)"
                        r"|[,;.!\n]\s*(?:now\s+)?(?:summarize|review|explain|check|compare|draft|send|find|fix)\s+\S"
                        r"|[,;.!\n]\s*(?:what|why|how|where|when|who)\b[^?]*\?", text)))

    def _trace_participation(self, key, event_ts, reason, decision):
        # Only bounded identifiers, never arbitrary Slack payloads or exception text.
        refs = [str(value or "") for value in (*key, event_ts)]
        refs = [value if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else "" for value in refs]
        logger.info("slack_participation decision=%s reason=%s workspace=%s channel=%s thread=%s event=%s",
                    decision, reason, *refs)

    async def _response_needed(self, event, team_id, channel_id, text, *, scoped=False):
        """A veto only: this never supplies permission or cancels work on ordinary chatter."""
        prose = self._participation_text(text)
        bot_uid = self._team_bot_user_ids.get(team_id, self._bot_user_id)
        direct = bool((bot_uid and f"<@{bot_uid}>" in prose)
                      or self._slack_message_matches_mention_patterns(prose))
        key = (team_id or self._channel_team.get(channel_id, ""), channel_id,
               self._session_thread_ts(event, event.get("ts", ""),
                                       event.get("channel_type") == "mpim", {}) or "")
        self_uids = {uid for uid in (bot_uid, self._bot_user_id) if uid}
        reason = None
        is_dm = channel_id.startswith("D")
        if not is_dm and not direct and self._slack_message_addressed_to_other_user(prose, self_uids):
            reason = "human_addressed"
        elif self._silence_requested(prose):
            reason = "no_reply"
            await self._revoke_requester_for_no_reply(event, team_id, channel_id)
        elif is_dm:
            return True
        elif not scoped and self._has_pending_requester_reply(key, event.get("user", "")):
            return True
        else:
            has_media = any(isinstance(file, dict) and (
                file.get("url_private_download") or file.get("url_private")
                or (file.get("id") and file.get("file_access") == "check_file_info"))
                for file in event.get("files", []))
            if self._participation_chatter(prose):
                body = re.sub(r"<@[^>\n]+>", "", prose).strip(".,!?:; \n").casefold()
                # Attachments can be the request, but do not turn an explicit FYI
                # or acknowledgement into one. Scoped Q&A does not consume files.
                if body or not has_media or scoped:
                    reason = "fyi" if re.match(r"^(?:just\s+)?(?:fyi|for your information)\b", body) else "ack"
        if reason:
            self._trace_participation(key, event.get("ts", ""), reason, "suppress")
        return reason is None

    def _free_response_admission(self, channel, is_thread_reply):
        return (channel not in self._slack_require_mention_channels()
                and (channel in self._slack_free_response_channels() or not self._slack_require_mention())
                and not (is_thread_reply and self._slack_thread_require_mention()))

    def _participations(self):
        return self._lazy_attr("_slack_participations", dict)

    @staticmethod
    def _ancestry_revoked(grant):
        while grant is not None:
            if grant.revoked:
                return True
            grant = grant.parent
        return False

    def _grant_source(self, grant):
        return grant.source or self.build_source(
            chat_id=grant.key[1], chat_type="group", user_id=grant.requester,
            thread_id=grant.key[2] or None, scope_id=grant.key[0] or None)

    def _participation_valid(self, grant):
        from gateway.client_context_codewords import has_receipt

        if hasattr(grant.source, "_codewords_owner") and not self._codewords_owner_grant(grant):
            return False
        codewords = (self._codewords_surface(grant.key) or has_receipt(grant.source)) and not self._codewords_owner_grant(grant)
        return (grant.live and not grant.revoked
                and not self._ancestry_revoked(grant)
                and (not codewords or self._codewords_channel_allowed(grant.key))
                and self._deletion_generation(grant.key) == grant.generation
                and not self._deletion_record(grant.key).get("muted")
                and self._is_interactive_user_authorized(
                    grant.requester, source=self._grant_source(grant)))

    def _requester_task(self, key, user_id):
        for grant in self._participations().values():
            if (grant.key == key and grant.requester == user_id
                    and self._participation_valid(grant)
                    and grant.task is not None and not grant.task.done()
                    and self._session_tasks.get(grant.session_key) is grant.task):
                return grant
        return None

    @staticmethod
    def _pending_reply(session_key, *, include_approval=True):
        from tools import clarify_gateway
        from tools.approval import has_blocking_approval
        entry = clarify_gateway.get_pending_for_session(session_key, include_choice_prompts=True)
        return bool((entry and not entry.event.is_set())
                    or (include_approval and has_blocking_approval(session_key)))

    def _has_pending_requester_reply(self, key, user_id):
        owner = self._requester_task(key, user_id)
        return bool(owner and self._pending_reply(owner.session_key, include_approval=False))

    async def _deletion_write(self, client, method, **kwargs):
        result = await super()._deletion_write(client, method, **kwargs)
        grant = _participation_scope.get()
        if (grant is not None and grant.adapter is self and not grant.key[2]
                and method in {"chat_postMessage", "chat_startStream", "chat.startStream"}):
            try:
                from .adapter import _slack_response_payload
            except ImportError:
                from adapter import _slack_response_payload
            response = _slack_response_payload(result)
            ts = str(response.get("ts") or response.get("message_ts") or "")
            if ts:
                grant.root_messages.add(ts)
        return result

    @staticmethod
    def _owns_root_surface(grant, key):
        return (not grant.key[2] and key[:2] == grant.key[:2]
                and key[2] in grant.root_messages)

    def _deletion_payload_key(self, method, payload, team_id=""):
        key = super()._deletion_payload_key(method, payload, team_id)
        message_id = str(payload.get("ts") or payload.get("timestamp") or "")
        grant = _participation_scope.get()
        if (message_id and grant is not None and grant.adapter is self
                and self._known_message_surface(key, message_id) is None):
            # Slack omits thread_ts on edits/reactions. Only observed message
            # receipts can prove that their target belongs to this request.
            self._deny_participation()
        if (grant is not None and grant.adapter is self and key != grant.key
                and (method not in {"chat_update", "chat.update", "chat_appendStream", "chat_stopStream",
                                    "chat.appendStream", "chat.stopStream", "reactions_add", "reactions_remove",
                                    "reactions.add", "reactions.remove"}
                     or not message_id or message_id != key[2]
                     or not self._owns_root_surface(grant, key))):
            self._deny_participation()
        return key

    def _check_deletion_scope(self, *, api_method=None, payload=None, sdk_client=None):
        try:
            from .codewords_upload import check_operation as chart_operation
        except ImportError:
            from codewords_upload import check_operation as chart_operation  # ty: ignore[unresolved-import]
        if chart_operation(self, api_method, payload, sdk_client):
            return
        try:
            from .codewords_ux import check_operation
        except ImportError:
            from codewords_ux import check_operation  # ty: ignore[unresolved-import]
        if check_operation(self, api_method, payload, sdk_client):
            return
        super()._check_deletion_scope()
        grant = _participation_scope.get()
        scope = _delivery_scope.get()
        read = _codewords_history_read.get()
        if read is not None:
            if (read[0] is not self or read[1] is not sdk_client or read[2] != api_method
                    or read[5] is not asyncio.current_task() or read[6] is not grant
                    or not self._participation_valid(grant)
                    or not isinstance(payload, dict)
                    or {key: value for key, value in payload.items() if value is not None} != read[3]):
                self._deny_participation()
            read[4]()
            return
        cookunity_read = _cookunity_history_read.get()
        if cookunity_read is not None:
            if (cookunity_read[0] is not self or cookunity_read[2] is not sdk_client
                    or cookunity_read[3] != api_method or cookunity_read[6] is not asyncio.current_task()
                    or cookunity_read[7] is not grant or not isinstance(payload, dict)
                    or {key: value for key, value in payload.items() if value is not None} != cookunity_read[4]):
                self._deny_participation()
            try:
                self._validate_cookunity_history_read(cookunity_read)
            except Exception:
                self._deny_participation()
            return
        if api_method == "files.completeUploadExternal" and payload and payload.get("channels"):
            # This adapter authorizes one destination; Slack's plural legacy
            # upload alias must not add recipients beside that destination.
            self._deny_participation()
        upload_stage = api_method in {"files.getUploadURLExternal", "files.uploadBytes"}
        destination_write = (api_method in {
                "chat.postMessage", "chat.postEphemeral", "chat.update", "chat.startStream",
                "chat.appendStream", "chat.stopStream", "reactions.add", "reactions.remove",
                "files.completeUploadExternal", "assistant.threads.setStatus", "assistant.threads.setTitle"}
                and payload and (payload.get("channel") or payload.get("channel_id")))
        if upload_stage or destination_write:
            client_team = self._authenticated_client_team(sdk_client)
            if (not client_team or (scope and scope[0] is self and scope[1][0]
                                    and scope[1][0] != client_team)):
                self._deny_participation()
            if upload_stage:
                # Upload URLs and bytes already reach a workspace before the
                # later file publication names its destination channel.
                expected = grant.key if grant is not None and grant.adapter is self else (
                    scope[1] if scope and scope[0] is self else None)
                if (expected is None or client_team != expected[0]
                        or self._deletion_record(expected).get("muted")):
                    self._deny_participation()
            else:
                key = self._deletion_payload_key(api_method, payload, client_team)
                record = self._deletion_record(key)
                if record.get("muted"):
                    self._deny_participation()
                scope = (self, key, record.get("generation", 0))
        key = scope[1] if scope and scope[0] is self else (
            grant.key if grant is not None and grant.adapter is self else None)
        isolated_grant = (grant is not None and grant.adapter is self
                          and (hasattr(grant.source, "_codewords_native_identity")
                               or hasattr(grant.source, "_codewords_policy")
                               or hasattr(grant.source, "_text_only_native_identity")
                               or hasattr(grant.source, "_text_only_policy")))
        if ((key is not None and (self._codewords_surface(key) or isolated_grant))
                or _codewords_release.get() is not None):
            if (grant is None or grant.adapter is not self
                    or grant.task is not None and grant.task is not asyncio.current_task()
                    or not self._participation_valid(grant)):
                self._deny_participation()
            if api_method is not None:
                release = _codewords_release.get()
                if (grant.task is not asyncio.current_task()
                        or api_method != "chat.postMessage" or release is None
                        or release[0] is not self or release[3] is not asyncio.current_task()
                        or not isinstance(payload, dict)
                        or {name: value for name, value in payload.items() if value is not None} != release[2]
                        or sdk_client is not release[4]
                        or getattr(sdk_client, "token", None) != release[5]
                        or self._team_bot_user_ids.get(grant.key[0]) != release[6]):
                    self._deny_participation()
                try:
                    release[1]()
                except Exception:
                    self._deny_participation()
        if grant is not None and grant.adapter is self:
            # Inherited task context identifies the requester, not permission to
            # move that request's output to another channel, team, or thread.
            if (not self._participation_valid(grant)
                    or (scope and scope[0] is self and scope[1] != grant.key
                        and not self._owns_root_surface(grant, scope[1]))):
                self._deny_participation()
            return
        if (scope and scope[0] is self and not scope[1][1].startswith("D")
                and not _scheduled_delivery.get()):
            self._deny_participation()

    def _deny_participation(self) -> NoReturn:
        grant = _participation_scope.get()
        scope = _delivery_scope.get()
        key = grant.key if grant is not None and grant.adapter is self else (
            scope[1] if scope and scope[0] is self else ("", "", ""))
        self._trace_participation(key, grant.request_ts if grant is not None else "",
                                  "grant_denied", "deny")
        raise ParticipationDenied()

    def authorize_turn(self, source):
        """Gateway pre-work guard, including internally synthesized watcher turns."""
        if source.chat_id.startswith("D"):
            return True
        grant = _participation_scope.get()
        return bool(grant and grant.adapter is self and self._participation_valid(grant)
                    and grant.requester == source.user_id
                    and grant.key == (source.scope_id or "", source.chat_id, source.thread_id or ""))

    def defer_pending_turns(self, session_key, overflow, *, draining=False):
        grant = _participation_scope.get()
        if grant is None or grant.adapter is not self:
            return False  # DMs retain the existing runner continuation behavior.
        # Reuse the runner's FIFO; do not recursively run its events under the
        # previous requester's grant. The adapter will consume each admission.
        queues = self._lazy_attr("_slack_followup_queues", dict)
        if draining:
            self._pending_messages.pop(session_key, None)
            queues.pop(session_key, []).clear()
            self._discard_text_debounce(session_key)
            if overflow is not None:
                overflow.clear()
            return True
        queued = queues.get(session_key)
        if queued and overflow is not queued:
            queued.extend(overflow or [])
            queued.sort(key=lambda event: event.metadata.get("_slack_admission_sequence", 0))
            if overflow is not None:
                overflow.clear()
        else:
            queued = overflow if overflow is not None else []
            queues[session_key] = queued
        if queued and session_key not in self._pending_messages:
            self._pending_messages[session_key] = queued.pop(0)
        return True

    def _promote_followup(self, session_key):
        queues = self._lazy_attr("_slack_followup_queues", dict)
        queue = queues.get(session_key)
        if queue and session_key not in self._pending_messages:
            self._pending_messages[session_key] = queue.pop(0)
        if not queue:
            queues.pop(session_key, None)

    def _spawn_drain_task(self, pending_event, session_key, delay: float = 0.0):
        self._promote_followup(session_key)
        super()._spawn_drain_task(pending_event, session_key, delay=delay)

    async def _drain_after(self, pending_event, session_key, delay, guard):
        # A backed-off head keeps the slot through the sleep, so the FIFO can only advance once
        # it is popped; promoting at spawn time would strand every later follow-up.
        if delay > 0:
            await asyncio.sleep(delay)
            await self._flush_text_debounce_now(session_key)
            pending_event = self._pending_messages.pop(session_key, None)
            if pending_event is None:
                self._cleanup_finished_session_task(session_key, guard)
                return
            self._promote_followup(session_key)
        await super()._drain_after(pending_event, session_key, 0.0, guard)

    async def _cancel_deleted_surface(self, key):
        sources = self._lazy_attr("_slack_surface_sessions", dict)
        sessions = [session for session, source in list(sources.items())
                    if (source.scope_id or "", source.chat_id, source.thread_id or "") == key]
        await super()._cancel_deleted_surface(key)
        queues = self._lazy_attr("_slack_followup_queues", dict)
        for session in sessions:
            queues.pop(session, []).clear()

    _REQUESTER_STOPS_MAX = 1024

    def _requester_stop_applies(self, event):
        source = event.source
        key = (source.scope_id or "", source.chat_id, source.thread_id or "", source.user_id)
        cutoff = max(self._lazy_attr("_slack_requester_stops", dict).get(key, ""),
                     self.__dict__.get("_slack_retired_stop_cutoff", ""))
        return bool(cutoff and event.message_id and str(event.message_id) <= cutoff)

    async def _revoke_requester_for_no_reply(self, event, team_id, channel_id, *, source=None):
        # A sender can withdraw earlier work even before its worker registers.
        if (event.get("_slack_changed_event_ts") or event.get("_hermes_force_process")
                or self._event_declares_bot_sender(event)):
            return
        user_id, ts = event.get("user", ""), str(event.get("ts") or "")
        key = (team_id or self._channel_team.get(channel_id, ""), channel_id,
               self._session_thread_ts(event, ts, event.get("channel_type") == "mpim", {}) or "")
        owner = self._requester_task(key, user_id)
        if source is not None and (
                getattr(source.platform, "value", source.platform) != "slack"
                or (source.scope_id or "", source.chat_id, source.thread_id or "") != key
                or source.user_id != user_id):
            return
        if (not ts or not self._is_interactive_user_authorized(
                    user_id, channel_id=channel_id, team_id=key[0],
                    thread_id=key[2] or None, source=source)):
            return
        # A newer owner does not protect callbacks from earlier requests.
        if owner is not None and ts <= owner.request_ts:
            owner = None
        stops = self._lazy_attr("_slack_requester_stops", dict)
        stops[(*key, user_id)] = max(ts, stops.get((*key, user_id), ""))
        if len(stops) > self._REQUESTER_STOPS_MAX:
            oldest = min(stops, key=stops.get)
            # Forget identities, never their revocation boundary. Under capacity
            # pressure ancient delayed events fail closed across all surfaces.
            self._slack_retired_stop_cutoff = max(
                self.__dict__.get("_slack_retired_stop_cutoff", ""), stops.pop(oldest))
        callbacks = []
        for callback in list(self._lazy_attr("_slack_callback_participations", dict).values()):
            if (callback.key == key and callback.requester == user_id
                    and (not callback.request_ts or callback.request_ts < ts)):
                ancestor = callback
                while ancestor is not None:
                    ancestor.revoked, ancestor.live = True, False
                    ancestor = ancestor.parent
                if callback.task is not None and callback.task is not asyncio.current_task():
                    callback.task.cancel()
                    callbacks.append(callback.task)
        if owner is not None:
            owner.revoked, owner.live = True, False
        if owner is None:
            self._revoke_requester_prompts(key, user_id, ts)
            removed = set()
            for identity, queued in list(self._admissions().items()):
                source = queued.source
                if ((source.scope_id or "", source.chat_id, source.thread_id or "") == key
                        and source.user_id == user_id
                        and (not queued.message_id or str(queued.message_id) <= ts)):
                    removed.add(identity)
                    self._admissions().pop(identity, None)
            for session, queued in list(self._pending_messages.items()):
                if queued.metadata.get("_slack_participation_id") in removed:
                    self._pending_messages.pop(session, None)
            for session, state in list(self._text_debounce_store().items()):
                if state.event.metadata.get("_slack_participation_id") in removed:
                    self._discard_text_debounce(session)
            queues = self._lazy_attr("_slack_followup_queues", dict)
            for session, queue in list(queues.items()):
                queue[:] = [queued for queued in queue
                            if queued.metadata.get("_slack_participation_id") not in removed]
                if queue and session not in self._pending_messages:
                    self._pending_messages[session] = queue.pop(0)
                if not queue:
                    queues.pop(session, None)
            if callbacks:
                await asyncio.gather(*callbacks, return_exceptions=True)
            return
        self._trace_participation(key, ts, "no_reply", "revoke")
        # Revoke before any await: inherited callbacks and SDK stages see this flag.
        session_key = owner.session_key
        admissions = self._admissions()
        survivors = []
        for identity, queued in list(admissions.items()):
            if self._event_session_key(queued) != session_key:
                continue
            if queued.source.user_id == user_id and (
                    not queued.message_id or str(queued.message_id) <= ts):
                admissions.pop(identity, None)
            else:
                survivors.append(queued)
        survivors.sort(key=lambda queued: queued.metadata["_slack_admission_sequence"])
        self._pending_messages.pop(session_key, None)
        queues = self._lazy_attr("_slack_followup_queues", dict)
        queues.pop(session_key, []).clear()
        self._discard_text_debounce(session_key)
        source = self._lazy_attr("_slack_surface_sessions", dict)[session_key]
        cancellations = self._lazy_attr("_slack_requester_cancellations", dict)

        async def cancel_and_resume():
            try:
                if self._session_tasks.get(session_key) is owner.task:
                    await self.request_session_cancellation(session_key, source)
                if callbacks:
                    await asyncio.gather(*callbacks, return_exceptions=True)
            finally:
                # The runner signals hard interruption before these wake blocked
                # workers. Cancel their prompts without clearing session approvals.
                from tools import clarify_gateway, slash_confirm
                from tools.approval import resolve_gateway_approval
                clarify_gateway.clear_session(session_key)
                slash_confirm.clear(session_key)
                resolve_gateway_approval(session_key, "deny", resolve_all=True)
                if survivors:
                    first, *rest = survivors
                    if rest:
                        self._pending_messages[session_key] = rest[0]
                        queues[session_key] = rest[1:]
                    self._start_session_processing(first, session_key)

        cancellation = asyncio.create_task(cancel_and_resume())
        cancellations[session_key] = cancellation
        try:
            await asyncio.shield(cancellation)
        finally:
            if cancellation.done() and cancellations.get(session_key) is cancellation:
                cancellations.pop(session_key, None)

    def _admissions(self):
        # Queues own the event, not this registry. Debounce/FIFO storage remains
        # authoritative and discarded events cannot leave reusable invitations.
        return self._lazy_attr("_slack_admissions", WeakValueDictionary)

    def _admit_event(self, event, generation):
        source = event.source
        key = (source.scope_id or "", source.chat_id, source.thread_id or "")
        receipts = self._lazy_attr("_slack_inbound_surfaces", dict)
        for message_id in (event.message_id, source.thread_id):
            if message_id:
                receipts[(*key[:2], str(message_id))] = key
        self._trim_oldest_dict_entries(receipts, self._BOT_TS_MAX)
        identity = uuid4().hex
        event.metadata["_slack_participation_id"] = identity
        event.metadata["_slack_admission_sequence"] = next(
            self._lazy_attr("_slack_admission_counter", count))
        event.metadata["_slack_deletion_generation"] = generation
        self._admissions()[identity] = event
        self._trace_participation(key, event.message_id, "request", "admit")

    def queue_message(self, session_key, text):
        """Retain authorized residual steering when the runner caps recursion."""
        from gateway.platforms.base import MessageEvent

        grant = _participation_scope.get()
        if (not text or grant is None or grant.adapter is not self
                or grant.session_key != session_key
                or self._requester_task(grant.key, grant.requester) is not grant):
            return
        source = self._lazy_attr("_slack_surface_sessions", dict).get(session_key)
        if source is None or not self.authorize_turn(source):
            return
        event = MessageEvent(text=text, source=source)
        self._admit_event(event, grant.generation)
        pending = self._pending_messages.get(session_key)
        if pending is not None:
            # This continues the current task before unrelated queued requests,
            # matching the uncapped runner without displacing their FIFO order.
            self._lazy_attr("_slack_followup_queues", dict).setdefault(session_key, []).insert(0, pending)
        self._pending_messages[session_key] = event

    async def handle_message(self, event):
        source = event.source
        if source.chat_id.startswith("D"):
            return await super().handle_message(event)
        if event.metadata.get("_slack_native_ingress") is not True:
            return
        key = (source.scope_id or "", source.chat_id, source.thread_id or "")
        if not self._is_interactive_user_authorized(
                source.user_id, source=source):
            return
        direct = event.metadata.get("_slack_direct_request") is True
        session_key = self._event_session_key(event)
        cancellation = self._lazy_attr("_slack_requester_cancellations", dict).get(session_key)
        if cancellation is not None:
            await asyncio.shield(cancellation)
        if self._requester_stop_applies(event):
            return
        owner = self._requester_task(key, source.user_id)
        # A shared-session pending interceptor cannot consume another person's request.
        if self._pending_reply(session_key) and owner is None:
            return
        if not direct and owner is None:
            return
        generation = self._deletion_generation(key)
        # A continuation can fall back to the adapter queue. Keep its event-bound
        # admission until actual drain, just like a mentioned request.
        if direct or owner is not None:
            self._admit_event(event, generation)
        grant = owner or _Participation(self, key, source.user_id, session_key, generation,
                                         request_ts=str(event.message_id or ""), source=copy(source))
        token = _bind_participation(grant)
        try:
            self._check_deletion_scope()
            running = self._session_tasks.get(session_key)
            if direct and owner is None and running is not None and not running.done():
                if session_key in self._pending_messages:
                    self._lazy_attr("_slack_followup_queues", dict).setdefault(session_key, []).append(event)
                else:
                    self._pending_messages[session_key] = event
            else:
                await super().handle_message(event)
        except DeletedSurfaceError:
            pass  # Revocation is a silent outcome, including inline replies.
        finally:
            if owner is None:
                grant.live = False
            _reset_participation(token)

    async def _process_message_background(self, event, session_key):
        source = event.source
        if source.chat_id.startswith("D"):
            return await super()._process_message_background(event, session_key)
        identity = event.metadata.get("_slack_participation_id")
        admitted = self._admissions().pop(identity, None) if identity else None
        key = (source.scope_id or "", source.chat_id, source.thread_id or "")
        generation = event.metadata.get("_slack_deletion_generation")
        if (admitted is not event or generation != self._deletion_generation(key)
                or self._deletion_record(key).get("muted")):
            if self._session_tasks.get(session_key) is asyncio.current_task():
                self._finish_session_task(session_key, self._active_sessions.get(session_key))
            return
        grant = _Participation(self, key, source.user_id, session_key, generation,
                               task=asyncio.current_task(), request_ts=str(event.message_id or ""), source=copy(source))
        self._participations()[identity] = grant
        token = _bind_participation(grant)
        try:
            try:
                self._check_deletion_scope()
            except DeletedSurfaceError:
                # The base lifecycle has not started, so it cannot release this
                # rejected worker's guard. Never clear a replacement task's guard.
                if self._session_tasks.get(session_key) is asyncio.current_task():
                    self._finish_session_task(session_key, self._active_sessions.get(session_key))
                return
            await super()._process_message_background(event, session_key)
        finally:
            grant.live = False
            _reset_participation(token)
            self._participations().pop(identity, None)

    def _revoke_requester_prompts(self, key, user_id, ts):
        from tools import clarify_gateway, slash_confirm
        from tools.approval import resolve_gateway_approval

        for original, approval_id, clarify_id, confirm_id in list(
                self._lazy_attr("_slack_prompt_participation", dict).values()):
            if (original.key != key or original.requester != user_id
                    or (original.request_ts and original.request_ts >= ts)):
                continue
            original.revoked, original.live = True, False
            session = original.session_key
            # Cancel exact prompt identities; a shared session may now belong
            # to another requester whose pending prompt must remain intact.
            if clarify_id:
                with clarify_gateway._lock:
                    entry = clarify_gateway._entries.get(clarify_id)
                    if entry and entry.session_key == session:
                        clarify_gateway._entries.pop(clarify_id)
                        ids = clarify_gateway._session_index.get(session, [])
                        if clarify_id in ids:
                            ids.remove(clarify_id)
                        if not ids:
                            clarify_gateway._session_index.pop(session, None)
                        if not entry.event.is_set():
                            entry.response = ""
                            entry.event.set()
            if approval_id:
                resolve_gateway_approval(session, "deny", request_id=approval_id)
            if confirm_id:
                with slash_confirm._lock:
                    pending = slash_confirm.get_pending(session)
                    if pending and pending["confirm_id"] == confirm_id:
                        slash_confirm.clear(session)

    def _remember_prompt_participation(self, channel, ts, metadata, *, clarify_id=None, confirm_id=None):
        grant = _participation_scope.get()
        if grant is not None and grant.adapter is self:
            prompts = self._lazy_attr("_slack_prompt_participation", dict)
            prompts[(grant.key[0], channel, ts)] = (
                grant, (metadata or {}).get("approval_request_id"), clarify_id, confirm_id)
            self._trim_oldest_dict_entries(prompts, self._BOT_TS_MAX)

    def _interaction_participation(self, team, channel, message_ts, user, kind, value):
        prompt = self._lazy_attr("_slack_prompt_participation", dict).get((team, channel, message_ts))
        original, approval_id, clarify_id, confirm_id = prompt if prompt else (None, None, None, None)
        if (original is None or self._ancestry_revoked(original) or original.requester != user
                or self._deletion_generation(original.key) != original.generation
                or self._deletion_record(original.key).get("muted")
                or not self._is_interactive_user_authorized(user, source=self._grant_source(original))):
            return None
        session_key = original.session_key
        pending = False
        if kind == "clarify":
            from tools import clarify_gateway
            with clarify_gateway._lock:
                entry = clarify_gateway._entries.get(value.split("|", 1)[0])
                pending = bool(entry and entry.clarify_id == clarify_id
                               and entry.session_key == session_key and not entry.event.is_set())
        elif kind == "approval":
            from tools.approval import list_gateway_approvals
            pending = bool(value == session_key and approval_id and any(
                entry.get("request_id") == approval_id for entry in list_gateway_approvals(session_key)))
        elif kind == "slash-confirm":
            from tools import slash_confirm
            entry = slash_confirm.get_pending(session_key)
            pending = bool(entry and entry["confirm_id"] == confirm_id
                           and value == f"{session_key}|{entry['confirm_id']}"
                           and not slash_confirm._is_stale(entry, slash_confirm.DEFAULT_TIMEOUT_SECONDS))
        elif kind == "model-picker":
            # The prompt record binds requester and generation; the handler owns stage/expiry.
            pending = True
        if not pending:
            return None
        return _Participation(self, original.key, user, session_key, original.generation,
                              parent=original, approval_id=approval_id, request_ts=original.request_ts,
                              source=copy(self._grant_source(original)), root_messages=original.root_messages)

    def _interaction_approval_id(self):
        grant = _participation_scope.get()
        return grant.approval_id if grant is not None and grant.adapter is self else None
