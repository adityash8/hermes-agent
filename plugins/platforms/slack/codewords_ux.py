"""Exact native request indicators, separate from model tools and output grants."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from gateway.client_context_codewords import CHANNEL, WORKSPACE, seal_event
from gateway.client_context_codewords_history import slack_timestamp
from gateway.platforms.base import ProcessingOutcome

try:
    from . import codewords_followups
    from .participation import _participation_scope
except ImportError:
    import codewords_followups  # ty: ignore[unresolved-import]  # Flat plugin loading.
    from participation import _participation_scope  # ty: ignore[unresolved-import]


_operation: ContextVar[tuple | None] = ContextVar("codewords_processing_operation", default=None)
_REACTION_TIMEOUT = 3.0


@dataclass
class _Indicator:
    adapter: Any
    client: Any
    credential: str
    bot: str
    event: Any
    seal: tuple
    grant: Any
    owner: asyncio.Task
    timestamp: str
    revision: int
    added: bool = False
    add_task: asyncio.Task | None = None
    terminal: str | None = None

    def identity_valid(self):
        return (self.adapter._team_clients.get(WORKSPACE) is self.client
                and self.client.token == self.credential
                and self.adapter._team_bot_user_ids.get(WORKSPACE) == self.bot
                and self.grant.adapter is self.adapter
                and self.grant.task is self.owner
                and self.grant.key[:2] == (WORKSPACE, CHANNEL)
                and self.grant.request_ts == self.timestamp)

    def live(self):
        return (self.identity_valid() and self.adapter._participation_valid(self.grant)
                and not self.owner.cancelling() and not self.owner.done()
                and self.adapter._reactions_enabled()
                and vars(self.adapter).get("_codewords_history_revision", 0) == self.revision
                and self.seal == seal_event(self.event)
                and self.event._codewords_request == self.seal
                and self.adapter.authorize_turn(self.event.source) is True)


def check_operation(adapter, api_method, payload, sdk_client) -> bool:
    """Revocation permits only removal of this task's acknowledged indicator."""
    operation = _operation.get()
    if operation is None:
        return False
    state, method, expected, task, cleanup = operation
    if (state.adapter is not adapter or state.client is not sdk_client
            or task is not asyncio.current_task() or _participation_scope.get() is not state.grant
            or api_method != method or not isinstance(payload, dict)
            or {key: value for key, value in payload.items() if value is not None} != expected
            or not state.identity_valid()):
        adapter._deny_participation()
    if cleanup:
        owned = state.added if expected["name"] == "eyes" else expected["name"] == state.terminal
        if not owned or method != "reactions.remove":
            adapter._deny_participation()
    elif not state.live():
        adapter._deny_participation()
    return True


def _reaction(state, emoji, *, remove=False):
    """Retain a bounded in-flight receipt when cancellation races Slack acceptance."""
    method = "reactions.remove" if remove else "reactions.add"
    payload = {"channel": CHANNEL, "timestamp": state.timestamp, "name": emoji}

    async def invoke():
        token = _operation.set((state, method, payload, asyncio.current_task(), remove))
        try:
            async with asyncio.timeout(_REACTION_TIMEOUT):
                check_operation(state.adapter, method, payload, state.client)
                response = await getattr(state.client, method.replace(".", "_"))(**payload)
            if response.get("ok") is not True:
                return False
            if emoji == "eyes":
                state.added = not remove
            elif emoji in {"white_check_mark", "x"}:
                state.terminal = None if remove else emoji
            return True
        except Exception:
            return False
        finally:
            _operation.reset(token)

    return asyncio.create_task(invoke())


async def _settle_reaction(state, task):
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        state.event._codewords_failed = True
        # Only the already-started operation survives; its own deadline bounds
        # settlement even when the parent receives repeated cancellation.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise


async def processing_start(adapter, event) -> bool:
    if not hasattr(event, "_codewords_request"):
        return False
    if not codewords_followups.started(adapter, event) or not adapter._reactions_enabled():
        return True
    grant = _participation_scope.get()
    assert grant is not None and grant.task is not None  # Established by started().
    client = adapter._team_clients.get(WORKSPACE)
    credential = getattr(client, "token", None)
    if not isinstance(credential, str) or not credential:
        return True
    state = _Indicator(adapter, client, credential, adapter._team_bot_user_ids[WORKSPACE],
                       event, seal_event(event), grant, grant.task,
                       slack_timestamp(event.message_id),
                       vars(adapter).get("_codewords_history_revision", 0))
    if not state.live():
        return True
    event._codewords_indicator = state
    state.add_task = _reaction(state, "eyes")
    await _settle_reaction(state, state.add_task)
    return True


async def processing_complete(adapter, event, outcome) -> bool:
    try:
        return await _processing_complete(adapter, event, outcome)
    except BaseException:
        if hasattr(event, "_codewords_request"):
            event._codewords_failed = True
        raise
    finally:
        if hasattr(event, "_codewords_request"):
            try:
                from .codewords_upload import processing_complete as finish_chart
            except ImportError:
                from codewords_upload import processing_complete as finish_chart
            try:
                await finish_chart(adapter, event, outcome)
            finally:
                try:
                    state = getattr(event, "_codewords_indicator", None)
                    if (state is not None and state.adapter is adapter
                            and state.owner is asyncio.current_task() and state.terminal
                            and (not state.live() or state.terminal == "white_check_mark"
                                 and getattr(event, "_codewords_failed", False))):
                        await _settle_reaction(state, _reaction(state, state.terminal, remove=True))
                finally:
                    codewords_followups.completed(adapter, event, outcome == ProcessingOutcome.SUCCESS
                                                  and not getattr(event, "_codewords_failed", False))


async def _processing_complete(adapter, event, outcome) -> bool:
    if not hasattr(event, "_codewords_request"):
        return False
    if getattr(event, "_codewords_failed", False):
        outcome = ProcessingOutcome.FAILURE
    state = getattr(event, "_codewords_indicator", None)
    if state is None or state.adapter is not adapter or state.owner is not asyncio.current_task():
        return True
    # The add task owns only the exact eyes operation; it cannot continue model
    # work or send anything else after its parent is cancelled or revoked.
    if state.add_task is not None:
        await _settle_reaction(state, state.add_task)
    if not state.added:
        return True
    removed = await _settle_reaction(state, _reaction(state, "eyes", remove=True))
    final = {ProcessingOutcome.SUCCESS: "white_check_mark", ProcessingOutcome.FAILURE: "x"}
    if removed and outcome in final and state.live():
        await _settle_reaction(state, _reaction(state, final[outcome]))
    return True
