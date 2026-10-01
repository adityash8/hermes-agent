"""Short-lived requester invitations, separate from live Slack output permission."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from time import monotonic

from gateway.client_context_codewords import CHANNEL, WORKSPACE, seal_event
from gateway.client_context_codewords_history import slack_timestamp

try:
    from .participation import _participation_scope
except ImportError:
    from participation import _participation_scope  # ty: ignore[unresolved-import]


_TTL = 15 * 60
_FOLLOWUPS = 8
_RECEIPTS = 128


@dataclass
class _Continuation:
    key: tuple[str, str, str, str]
    policy: object
    client: object
    credential: str = field(repr=False)
    bot: str
    generation: int
    revision: int
    expires: float
    summoned_at: Decimal
    last_ts: Decimal
    remaining: int = _FOLLOWUPS
    active: object | None = None


def _records(adapter):
    return adapter._lazy_attr("_codewords_followups", dict)


def _key(source):
    return (source.scope_id or "", source.chat_id, source.thread_id or "", source.user_id)


def _current(adapter, receipt, source):
    key = _key(source)
    client = adapter._team_clients.get(WORKSPACE)
    cutoff = max(adapter._lazy_attr("_slack_requester_stops", dict).get(key, ""),
                 vars(adapter).get("_slack_retired_stop_cutoff", ""))
    return (key == receipt.key and key[:2] == (WORKSPACE, CHANNEL)
            and bool(key[2]) and not source.is_bot
            and adapter._codewords_channel_allowed(key[:3])
            and not adapter._slack_strict_mention()
            and not adapter._slack_thread_require_mention()
            and monotonic() < receipt.expires
            and getattr(source, "_codewords_policy", None) == receipt.policy
            and adapter._is_interactive_user_authorized(source.user_id, source=source) is True
            and client is receipt.client and getattr(client, "token", None) == receipt.credential
            and adapter._team_bot_user_ids.get(WORKSPACE) == receipt.bot
            and adapter._deletion_generation(key[:3]) == receipt.generation
            and not adapter._deletion_record(key[:3]).get("muted")
            and vars(adapter).get("_codewords_history_revision", 0) == receipt.revision
            and (not cutoff or Decimal(cutoff) < receipt.summoned_at))


def continuation(adapter, event, source):
    """Native admission can borrow only a still-valid invitation, never history."""
    receipt = _records(adapter).get(_key(source))
    if receipt is None:
        return None
    try:
        if not _current(adapter, receipt, source):
            _records(adapter).pop(receipt.key, None)
            return None
        timestamp = Decimal(slack_timestamp(event.get("ts")))
        if (receipt.remaining > 0 and event.get("thread_ts") == source.thread_id
                and timestamp > receipt.last_ts):
            return receipt
    except (AttributeError, TypeError, ValueError, ArithmeticError):
        _records(adapter).pop(receipt.key, None)
    return None


def revoke(adapter, source, timestamp):
    receipt = _records(adapter).get(_key(source))
    if receipt is not None and Decimal(slack_timestamp(timestamp)) >= receipt.summoned_at:
        _records(adapter).pop(receipt.key, None)


def started(adapter, event) -> bool:
    """Create/consume an invitation only inside the actual admitted worker."""
    grant = _participation_scope.get()
    if grant is None:
        return False
    source = event.source
    key = _key(source)
    valid = (grant.adapter is adapter
             and grant.key == key[:3] and grant.requester == key[3]
             and grant.task is asyncio.current_task()
             and adapter._session_tasks.get(grant.session_key) is grant.task
             and grant.request_ts == str(event.message_id or "")
             and adapter._participation_valid(grant)
             and event.metadata.get("_slack_native_ingress") is True
             and getattr(event, "_codewords_request", None) == seal_event(event))
    if not valid:
        if grant.adapter is adapter and grant.task is asyncio.current_task():
            grant.revoked, grant.live = True, False
        return False
    records = _records(adapter)
    receipt = records.get(key)
    try:
        timestamp = Decimal(slack_timestamp(event.message_id))
        if receipt is not None and receipt.active is event:
            if _current(adapter, receipt, source):
                return True
            raise ValueError("expired_native_followup")
        if event.metadata.get("_codewords_summon") is True:
            client = adapter._team_clients.get(WORKSPACE)
            credential = getattr(client, "token", None)
            bot = adapter._team_bot_user_ids.get(WORKSPACE)
            if (key[:2] != (WORKSPACE, CHANNEL) or not key[2]
                    or not isinstance(credential, str) or not credential or not bot):
                raise ValueError("invalid_native_followup_identity")
            receipt = _Continuation(
                key, source._codewords_policy, client, credential, bot,
                grant.generation, vars(adapter).get("_codewords_history_revision", 0),
                monotonic() + _TTL, timestamp, timestamp, active=event)
            records.pop(key, None)
            records[key] = receipt
            while len(records) > _RECEIPTS:
                records.pop(next(iter(records)))
        else:
            pending = getattr(event, "_codewords_followup", None)
            if (receipt is None or pending is not receipt or receipt.active is not None
                    or receipt.remaining <= 0 or timestamp <= receipt.last_ts
                    or not _current(adapter, receipt, source)):
                raise ValueError("expired_native_followup")
            receipt.remaining -= 1
            receipt.last_ts = timestamp
            receipt.active = event
        return True
    except (AttributeError, TypeError, ValueError, ArithmeticError):
        # Processing hooks swallow exceptions; revoking the real grant closes
        # provider/read/delivery paths even when a queued receipt expires here.
        grant.revoked, grant.live = True, False
        owned = getattr(event, "_codewords_followup", None)
        if receipt is not None and (receipt is owned or receipt.active is event):
            records.pop(key, None)
        return False


def completed(adapter, event, success: bool) -> None:
    records = _records(adapter)
    receipt = records.get(_key(event.source))
    if receipt is None or receipt.active is not event:
        return
    try:
        keep = success is True and _current(adapter, receipt, event.source)
    except (AttributeError, TypeError, ValueError, ArithmeticError):
        keep = False
    if keep:
        receipt.active = None
    else:
        records.pop(receipt.key, None)
