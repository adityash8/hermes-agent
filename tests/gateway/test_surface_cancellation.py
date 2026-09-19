"""Surface removal must interrupt generation without replying or draining queued input."""

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.run_adapters import GatewayAdapterLifecycleMixin
from gateway.session import SessionSource


class Adapter(BasePlatformAdapter):
    name = "surface-test"
    connect = disconnect = send = send_message = get_chat_info = start_listening = AsyncMock()


def wired_adapter():
    adapter = Adapter(PlatformConfig(), Platform.SLACK)
    adapter.set_owner_profile("secondary")
    runner = SimpleNamespace(
        session_store=object(),
        _handle_reaction_event=AsyncMock(),
        _recover_telegram_topic_thread_id=Mock(),
        _busy_text_mode="interrupt",
        _interrupt_and_clear_session=AsyncMock(),
    )
    # Multiplex handlers are closures, not bound runner methods.
    async def message_handler(event):
        return None

    GatewayAdapterLifecycleMixin._wire_adapter_handlers(
        cast(GatewayAdapterLifecycleMixin, runner), adapter, message_handler=message_handler,
        fatal_error_handler=AsyncMock(), busy_session_handler=AsyncMock(),
        authorization_check=lambda *a, **kw: True, platform_event_handler=AsyncMock(),
    )
    source = SessionSource(
        platform=Platform.SLACK, chat_id="C1", chat_type="group", user_id="U1",
        thread_id="123.1", scope_id="T1", profile="secondary",
    )
    return runner, adapter, source


@pytest.mark.asyncio
async def test_external_surface_cancellation_clears_before_unwind_and_hard_stops():
    runner, adapter, source = wired_adapter()
    key = "secondary-slack-thread"
    entered = asyncio.Event()
    unwound = asyncio.Event()
    timer = Mock()
    adapter._pending_messages[key] = object()
    adapter._text_debounce[key] = SimpleNamespace(cancel_timer=timer)
    adapter._active_sessions[key] = asyncio.Event()

    async def processing():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert key not in adapter._pending_messages
            assert key not in adapter._text_debounce
            runner._interrupt_and_clear_session.assert_awaited_once()
            unwound.set()

    task = asyncio.create_task(processing())
    adapter._track_session_task(key, task)
    await entered.wait()
    await adapter.request_session_cancellation(key, source)
    assert unwound.is_set()
    assert task.cancelled()
    assert key not in adapter._active_sessions
    timer.assert_called_once_with()
    call = runner._interrupt_and_clear_session.await_args
    assert call.args == (key, source)
    assert source.scope_id == "T1" and source.profile == "secondary"
    assert source._transport_adapter_ref() is adapter
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_surface_cancellation_from_own_send_does_not_await_itself():
    runner, adapter, source = wired_adapter()
    key = "own-send"
    requested = asyncio.Event()

    async def processing():
        await adapter.request_session_cancellation(key, source)
        requested.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(processing())
    adapter._track_session_task(key, task)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert requested.is_set()
    runner._interrupt_and_clear_session.assert_awaited_once()
