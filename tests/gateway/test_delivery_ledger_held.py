"""A redelivery refused for authorization is held, not retried to abandonment (EZ-841)."""
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform

SESSION = "agent:main:slack:channel:C1"


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")


def _record(oid="ob-1"):
    dl.record_obligation(obligation_id=oid, session_key=SESSION, platform="slack", chat_id="C1",
                         thread_id="171.001", content="the final answer")


def _orphan(oid="ob-1"):
    with dl._connect() as conn:
        conn.execute("UPDATE delivery_obligations SET owner_pid=999999999, owner_started_at=1 "
                     "WHERE obligation_id=?", (oid,))


def _row(oid="ob-1"):
    with dl._connect() as conn:
        r = conn.execute("SELECT state, attempts, last_error FROM delivery_obligations "
                         "WHERE obligation_id=?", (oid,)).fetchone()
    return None if r is None else {"state": r[0], "attempts": r[1], "last_error": r[2]}


def _runner(error):
    from gateway.run import GatewayRunner

    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=MagicMock(success=False, error=error))
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.SLACK: adapter}
    runner._profile_adapters = {}
    runner._active_profile_name = lambda: "default"
    store = MagicMock()
    store.clear_resume_pending = AsyncMock()
    store.mark_resume_pending = AsyncMock()
    store._store = None
    runner.session_store = None
    runner._async_session_store = store
    return runner, adapter, store


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["slack_participation_revoked", "slack_thread_deleted"])
async def test_authorization_refusal_is_held_across_restarts(error):
    _record()
    for _boot in range(dl.MAX_ATTEMPTS + 2):
        _orphan()
        runner, adapter, store = _runner(error)
        assert await runner._redeliver_pending_obligations() == 0
    # Only the first boot could claim it; later sweeps never touch a held row.
    adapter.send.assert_not_awaited()
    assert _row() == {"state": "held", "attempts": 0, "last_error": error}
    assert dl.sweep_recoverable() == []
    assert dl.pending_retries() == []


@pytest.mark.asyncio
async def test_held_row_keeps_session_marked_without_auto_resume():
    from gateway.run import GatewayRunner

    _record()
    _orphan()
    runner, _, store = _runner("slack_participation_revoked")
    await runner._redeliver_pending_obligations()
    store.clear_resume_pending.assert_awaited_once_with(SESSION)
    store.mark_resume_pending.assert_awaited_once_with(SESSION, reason="delivery_held")
    assert "delivery_held" not in GatewayRunner._AUTO_RESUME_REASONS


@pytest.mark.asyncio
async def test_other_send_failures_still_spend_attempts():
    _record()
    _orphan()
    runner, _, store = _runner("channel_not_found")
    await runner._redeliver_pending_obligations()
    assert _row()["state"] == "failed"
    assert _row()["attempts"] == 1
    store.mark_resume_pending.assert_not_awaited()


def _prune(now):
    with dl._DB_LOCK, dl._transaction() as conn:
        dl._prune_unlocked(conn, now)


def test_held_rows_outlive_normal_retention_then_expire():
    _record("held")
    _record("done")
    assert dl.mark_held("held", "slack_thread_deleted")
    dl.mark_delivered("done")
    _prune(time.time() + dl._RETENTION_SECONDS + 60)
    assert _row("done") is None
    assert _row("held")["state"] == "held"
    _prune(time.time() + dl._HELD_RETENTION_SECONDS + 60)
    assert _row("held") is None


def test_held_rows_never_displace_live_rows_under_the_cap(monkeypatch):
    monkeypatch.setattr(dl, "_MAX_ROWS", 2)
    for oid in ("h1", "h2"):
        _record(oid)
        assert dl.mark_held(oid, "slack_participation_revoked")
    _record("live")
    _prune(time.time())
    assert _row("live")["state"] == "pending"
    assert [_row(oid) is None for oid in ("h1", "h2")].count(True) == 1


def test_release_held_returns_row_to_redelivery():
    _record()
    assert dl.mark_held("ob-1", "slack_participation_revoked")
    assert not dl.mark_held("ob-1", "slack_participation_revoked")
    assert dl.release_held("ob-1")
    assert _row() == {"state": "failed", "attempts": 0, "last_error": None}
    assert not dl.release_held("ob-1")
