"""One native Codewords chart, exact upload stages and operation-owned cleanup."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx

from gateway.client_context_codewords import CHANNEL, WORKSPACE, native_identity, seal_event
from gateway.client_context_codewords_chart import Chart, FILENAME, TITLE, summary
from gateway.client_context_policy import require
from gateway.platforms.base import ProcessingOutcome, SendResult

try:
    from .participation import _participation_scope
except ImportError:
    from participation import _participation_scope  # ty: ignore[unresolved-import]

logger = logging.getLogger(__name__)
_STAGE_TIMEOUT = 10.0
_operation: ContextVar[Any] = ContextVar("codewords_chart_operation", default=None)


def _transport_identity(client):
    require(client.base_url == "https://slack.com/api/" and client.retry_handlers == []
            and client.default_params == {} and isinstance(client.headers, dict)
            and not any(key.lower() in {"authorization", "cookie", "host"} for key in client.headers))
    return (tuple(sorted(client.headers.items())), client.proxy, client.ssl, client.session,
            client.trust_env_in_session)


@dataclass(frozen=True)
class _Receipt:
    adapter: Any
    client: Any
    token: str
    bot: str
    transport: tuple
    event: Any
    seal: tuple
    grant: Any
    key: tuple
    requester: str
    generation: int
    owner: asyncio.Task
    validate: Any
    reply: Any
    chart: Chart
    comment: str

    def identity_valid(self):
        return (self.adapter._team_clients.get(WORKSPACE) is self.client
                and self.client.token == self.token
                and self.adapter._team_bot_user_ids.get(WORKSPACE) == self.bot
                and _transport_identity(self.client) == self.transport
                and self.grant.adapter is self.adapter and self.grant.task is self.owner
                and self.grant.key == self.key and self.grant.requester == self.requester
                and self.grant.generation == self.generation
                and native_identity(self.grant.source) == self.seal[0]
                and self.grant.request_ts == self.seal[2]
                and getattr(self.grant.source, "_codewords_policy", None) == self.seal[-1])

    def live(self):
        require(self.identity_valid() and self.adapter._participation_valid(self.grant)
                and not self.owner.cancelling() and not self.owner.done()
                and self.seal == seal_event(self.event) == self.event._codewords_request
                and self.adapter.authorize_turn(self.event.source) is True)
        self.validate()
        require(type(self.chart.png) is bytes and self.chart.png.startswith(b"\x89PNG\r\n\x1a\n")
                and 100 < len(self.chart.png) <= 2_000_000
                and hashlib.sha256(self.chart.png).hexdigest() == self.chart.sha256
                and self.comment == summary(self.chart.comparison)
                and self.reply._codewords_validate is self.validate)


@dataclass(frozen=True)
class _Allocation:
    file_id: str
    url: str


@dataclass
class _Upload:
    receipt: _Receipt
    used: bool = False
    closed: bool = False
    allocated: _Allocation | None = None
    published: bool = False
    cleaned: bool = False
    cleanup_attempted: bool = False
    active: Any = None


@dataclass
class _Operation:
    state: _Upload
    method: str
    expected: dict
    task: asyncio.Task | None = None
    consumed: bool = False


def prepare(adapter, event, reply, chart, validate):
    """Mint only on the admitted owner task, after the actual read loop returns."""
    grant = _participation_scope.get()
    owner = asyncio.current_task()
    require(grant is not None and owner is not None and grant.task is owner
            and grant.adapter is adapter and native_identity(grant.source) == native_identity(event.source)
            and grant.key[:2] == (WORKSPACE, CHANNEL) and isinstance(chart, Chart)
            and str(reply) == summary(chart.comparison))
    assert grant is not None and owner is not None  # Checked above; require is not a type guard.
    client = adapter._team_clients[WORKSPACE]
    require(isinstance(client.token, str) and bool(client.token))
    receipt = _Receipt(adapter, client, client.token, adapter._team_bot_user_ids[WORKSPACE],
                       _transport_identity(client), event, seal_event(event), grant, grant.key,
                       grant.requester, grant.generation, owner, validate, reply, chart, str(reply))
    receipt.live()
    state = _Upload(receipt)
    event._codewords_upload = state
    reply._codewords_upload = state


def check_sdk_call(client, args, kwargs):
    """Reject SDK auth/verb/transport overrides hidden beside the exact payload."""
    operation = _operation.get()
    if operation is None:
        return
    receipt = operation.state.receipt
    if (client is not receipt.client or args != (operation.method,)
            or set(kwargs) != {"params"}):
        receipt.adapter._deny_participation()


def check_operation(adapter, method, payload, client):
    operation = _operation.get()
    if operation is None:
        return False
    state, receipt = operation.state, operation.state.receipt
    if (receipt.adapter is not adapter or receipt.client is not client
            or operation.task is not asyncio.current_task() or operation.consumed
            or state.active is not operation or _participation_scope.get() is not receipt.grant
            or method != operation.method or not isinstance(payload, dict)
            or {key: value for key, value in payload.items() if value is not None} != operation.expected):
        adapter._deny_participation()
    try:
        require(receipt.identity_valid())
        if method == "files.delete":
            require(state.cleanup_attempted and state.allocated is not None and not state.cleaned
                    and operation.expected == {"file": state.allocated.file_id})
        else:
            require(state.used and not state.closed)
            receipt.live()
    except Exception:
        adapter._deny_participation()
    operation.consumed = True
    return True


def _byte_client():
    return httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=_STAGE_TIMEOUT)


async def _upload_bytes(state):
    receipt, allocated = state.receipt, state.allocated
    assert allocated is not None
    payload = {"url": allocated.url, "data": receipt.chart.png}
    receipt.adapter._check_deletion_scope(api_method="files.uploadBytes", payload=payload,
                                         sdk_client=receipt.client)
    async with _byte_client() as client:
        async with client.stream("POST", allocated.url, content=receipt.chart.png,
                                 headers={"Content-Type": "image/png",
                                          "Content-Length": str(len(receipt.chart.png))}) as response:
            require(response.status_code == 200)


async def _invoke(operation):
    state, receipt = operation.state, operation.state.receipt
    operation.task = asyncio.current_task()
    token = _operation.set(operation)
    try:
        async with asyncio.timeout(_STAGE_TIMEOUT):
            method, payload = operation.method, operation.expected
            if method == "files.uploadBytes":
                await _upload_bytes(state)
                return
            kwargs = dict(payload)
            if method == "files.completeUploadExternal":
                kwargs["files"] = json.loads(kwargs["files"])
            result = await getattr(receipt.client, method.replace(".", "_"))(**kwargs)
            require(result.get("ok") is True)
            if method == "files.getUploadURLExternal":
                file_id = result.get("file_id")
                require(type(file_id) is str and re.fullmatch(r"F[A-Z0-9]{8,64}", file_id))
                # Ownership precedes URL validation and any possible publication.
                state.allocated = _Allocation(file_id, result.get("upload_url"))
                require(type(state.allocated.url) is str and re.fullmatch(
                    r"https://files\.slack\.com/upload/v1/[A-Za-z0-9_-]{1,4096}", state.allocated.url))
            elif method == "files.completeUploadExternal":
                files = result.get("files")
                require(state.allocated is not None and isinstance(files, list) and len(files) == 1
                        and isinstance(files[0], dict) and files[0].get("id") == state.allocated.file_id)
                state.published = True
            elif method == "files.delete":
                state.cleaned = True
    finally:
        _operation.reset(token)


async def _settle(task):
    # Cancellation never transfers authority to start another publish operation.
    # Every child owns one bounded stage; repeated parent cancellation cannot orphan it.
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _stage(state, method, payload):
    operation = _Operation(state, method, payload)
    require(state.active is None)
    state.active = operation
    task = asyncio.create_task(_invoke(operation))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        state.closed = True
        try:
            await _settle(task)
        except Exception:
            pass  # The caller still cleans the allocated ID after unknown acceptance.
        raise
    finally:
        state.active = None


async def _cleanup(state):
    state.closed = True
    if state.allocated is None or state.cleanup_attempted:
        return
    state.cleanup_attempted = True
    try:
        require(state.receipt.identity_valid())
        await _stage(state, "files.delete", {"file": state.allocated.file_id})
    except (Exception, asyncio.CancelledError):
        if not state.cleaned:
            logger.warning("codewords_chart_cleanup_unconfirmed")


async def send(adapter, content, chat_id, thread, client):
    """Return (delivery result, fallback text), with no automatic upload retries."""
    state = getattr(content, "_codewords_upload", None)
    if not isinstance(state, _Upload):
        return None, None
    receipt = state.receipt
    try:
        require(receipt.reply is content and receipt.adapter is adapter and receipt.client is client
                and receipt.owner is asyncio.current_task() and _participation_scope.get() is receipt.grant
                and state is getattr(receipt.event, "_codewords_upload", None)
                and not state.used and not state.closed
                and chat_id == CHANNEL and thread == (receipt.key[2] or receipt.event.message_id))
        receipt.live()
    except Exception:
        return SendResult(success=False, error="codewords_chart_denied"), None
    state.used = True
    try:
        await _stage(state, "files.getUploadURLExternal", {"filename": FILENAME, "length": len(receipt.chart.png)})
        receipt.live()
        assert state.allocated is not None
        await _stage(state, "files.uploadBytes", {"url": state.allocated.url, "data": receipt.chart.png})
        receipt.live()
        await _stage(state, "files.completeUploadExternal", {
            "files": json.dumps([{"id": state.allocated.file_id, "title": TITLE}]),
            "channel_id": CHANNEL, "thread_ts": thread, "initial_comment": receipt.comment})
        receipt.live()
        return SendResult(success=True), None
    except asyncio.CancelledError:
        await _cleanup(state)
        raise
    except Exception:
        await _cleanup(state)
        try:
            receipt.live()
        except Exception:
            return SendResult(success=False, error="codewords_chart_denied"), None
        # A useful text fallback is still an incomplete chart request for UX purposes.
        receipt.event._codewords_failed = True
        limitation = ("chart upload unavailable; showing validated counts."
                      if state.allocated is None or state.cleaned
                      else "chart delivery could not be confirmed; showing validated counts.")
        return None, receipt.comment + "\n• " + limitation


async def processing_complete(adapter, event, outcome):
    state = getattr(event, "_codewords_upload", None)
    if not isinstance(state, _Upload) or state.receipt.adapter is not adapter:
        return
    if state.receipt.owner is not asyncio.current_task():
        return
    try:
        require(outcome == ProcessingOutcome.SUCCESS and not getattr(event, "_codewords_failed", False))
        state.receipt.live()
    except Exception:
        event._codewords_failed = True
        await _cleanup(state)
    finally:
        state.closed = True
