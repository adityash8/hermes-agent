"""Every ``start_gateway`` exit must stop the planned-stop watcher thread it started (EZ-1139).

Only the normal shutdown tail used to stop it, so each early exit (clean exit, failed or raising
``runner.start()``, PID-claim refusal) leaked one polling thread for the life of the process. In a
full-order run those leaked watchers piled up and later tests hung.
"""

import threading
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig


@pytest.fixture(autouse=True)
def _startup_path_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.run._host_attach_or_none", AsyncMock(return_value=None))
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: None)
    monkeypatch.setattr("hermes_logging.setup_logging", lambda hermes_home, mode: tmp_path)
    monkeypatch.setattr("hermes_logging._add_rotating_handler", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.start_nous_auth_keepalive", lambda: None)
    yield
    from gateway import host_rendezvous as hr

    hr.release_host_lock(hr.ROLE_GATEWAY)
    hr.clear_record(hr.ROLE_GATEWAY)


def _runner_class(*, start_result=True, start_error=None, exit_cleanly=False, exit_code=None):
    class _Runner:
        def __init__(self, config):
            self.config = config
            self.should_exit_cleanly = exit_cleanly
            self.exit_reason = None
            self.exit_code = exit_code
            self.adapters = {}
            self._running = False
            self.should_exit_with_failure = False
            self._restart_requested = False
            self._restart_via_service = False

        async def start(self):
            if start_error is not None:
                raise start_error
            return start_result

        async def stop(self):
            return None

        async def wait_for_shutdown(self):
            return None

    return _Runner


def _live_watchers(before):
    return [t for t in threading.enumerate() if t.name == "planned-stop-watcher" and t not in before]


async def _run_start_gateway():
    from gateway.run import start_gateway

    return await start_gateway(config=GatewayConfig(), replace=False, verbosity=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner_kwargs, expected",
    [
        pytest.param({"exit_cleanly": True}, True, id="clean-exit"),
        pytest.param({"start_result": False}, False, id="start-returns-false"),
        pytest.param({}, None, id="startup-aborted-before-running"),
    ],
)
async def test_watcher_stopped_on_early_return(monkeypatch, runner_kwargs, expected):
    monkeypatch.setattr("gateway.run.GatewayRunner", _runner_class(**runner_kwargs))
    before = set(threading.enumerate())

    result = await _run_start_gateway()

    if expected is not None:
        assert result is expected
    assert _live_watchers(before) == []


@pytest.mark.asyncio
async def test_watcher_stopped_when_runner_start_raises(monkeypatch):
    monkeypatch.setattr("gateway.run.GatewayRunner", _runner_class(start_error=RuntimeError("boom")))
    before = set(threading.enumerate())

    with pytest.raises(RuntimeError, match="boom"):
        await _run_start_gateway()

    assert _live_watchers(before) == []


@pytest.mark.asyncio
async def test_watcher_stopped_on_fatal_exit_code(monkeypatch):
    monkeypatch.setattr("gateway.run.GatewayRunner", _runner_class(exit_cleanly=True, exit_code=78))
    before = set(threading.enumerate())

    with pytest.raises(SystemExit):
        await _run_start_gateway()

    assert _live_watchers(before) == []


@pytest.mark.asyncio
async def test_watcher_stopped_when_pid_claim_refused(monkeypatch):
    monkeypatch.setattr("gateway.run.GatewayRunner", _runner_class())
    monkeypatch.setattr("gateway.run._start_gateway_claim_pid_file", lambda force: False)
    before = set(threading.enumerate())

    assert await _run_start_gateway() is False

    assert _live_watchers(before) == []
