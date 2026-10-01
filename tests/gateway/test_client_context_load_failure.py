"""EZ-852: a broken config.yaml must not silently mute Slack.

Client context stays fail-closed for every channel, but the failure is logged as an
ERROR once, shown in /status, and the operator's own Slack home DM keeps working.
"""
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import client_context as cc
from gateway.client_context_restricted import CODEWORDS
from gateway.config import Platform, load_gateway_config
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands_status import GatewayStatusCommandsMixin

HOME_DM = "D0HOMEOWNER"
SECRET = "SENTINEL_EZ852_VALUE"
BROKEN = f"gateway:\n  token: [{SECRET}\n"  # unterminated flow list; the parser error quotes this line


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path.resolve() / "home"
    path.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(path))
    monkeypatch.setenv("SLACK_HOME_CHANNEL", HOME_DM)
    return path


def broken(home):
    (home / "config.yaml").write_text(BROKEN, encoding="utf-8")
    return load_gateway_config()


def slack(chat_id, user_id="U0OWNER"):
    return SimpleNamespace(platform=Platform.SLACK, chat_id=chat_id, user_id=user_id)


def test_load_failure_logs_one_error_with_path_and_line_but_no_config_text(home, caplog):
    caplog.set_level(logging.WARNING, logger="gateway.config")
    broken(home)
    load_gateway_config()  # a second load of the same broken file stays quiet

    errors = [r for r in caplog.records if r.name == "gateway.config" and r.levelno >= logging.WARNING]
    assert [r.levelno for r in errors] == [logging.ERROR]
    message = errors[0].getMessage()
    assert str(home / "config.yaml") in message and "line 2" in message
    assert SECRET not in message and "denies every Slack channel except the operator home DM" in message


def test_load_failure_is_recorded_without_config_values(home):
    config = broken(home)
    assert config.config_load_error.startswith("ParserError at line 2")
    assert SECRET not in config.config_load_error


@pytest.mark.parametrize("chat_id", ["C0CLIENT", CODEWORDS, "G0PRIVATE", "D0SOMEONEELSE"])
def test_load_failure_keeps_every_other_slack_surface_fail_closed(home, chat_id):
    assert cc.applies(broken(home), slack(chat_id))


def test_load_failure_lets_only_the_operator_home_dm_through(home):
    config = broken(home)
    assert not cc.applies(config, slack(HOME_DM))
    assert cc.applies(config, SimpleNamespace(platform=Platform.SLACK))  # adapter-level gate stays armed


def test_home_channel_that_is_not_a_dm_gets_no_exemption(home, monkeypatch):
    monkeypatch.setenv("SLACK_HOME_CHANNEL", "C0HOMECHANNEL")
    assert cc.applies(broken(home), slack("C0HOMECHANNEL"))


def test_no_home_channel_means_no_exemption(home, monkeypatch):
    monkeypatch.delenv("SLACK_HOME_CHANNEL")
    assert cc.applies(broken(home), slack(HOME_DM))


@pytest.mark.parametrize("legacy", [{"enabled": False}, {"enabled": True, "manifest": "clients.yaml"}],
                         ids=["legacy-off", "legacy-on"])
def test_load_failure_overrides_legacy_gateway_json_client_context(home, legacy):
    """A legacy gateway.json value cannot prove what the unreadable config.yaml said."""
    (home / "gateway.json").write_text(json.dumps({"client_context": legacy}), encoding="utf-8")
    config = broken(home)
    assert config.client_context == {}
    assert cc.applies(config, slack("C0CLIENT"))
    assert not cc.applies(config, slack(HOME_DM))


def test_healthy_config_with_malformed_client_context_still_denies_home_dm(home):
    (home / "config.yaml").write_text('{"gateway": {"client_context": {}}}', encoding="utf-8")
    config = load_gateway_config()
    assert config.config_load_error is None
    assert cc.applies(config, slack(HOME_DM))


@pytest.mark.asyncio
async def test_runner_admission_routes_home_dm_to_legacy_and_denies_clients(home):
    runner = SimpleNamespace(config=broken(home), _is_user_authorized_for_source=Mock(return_value=True))
    owner = SessionSource(platform=Platform.SLACK, chat_id=HOME_DM, chat_type="dm", user_id="U0OWNER")
    client = SessionSource(platform=Platform.SLACK, chat_id="C0CLIENT", chat_type="group", user_id="U0CLIENT")
    assert await cc.admission(runner, owner) == "legacy"
    assert await cc.admission(runner, client) == "deny"


async def _status(monkeypatch, config):
    monkeypatch.setenv("HERMES_LANGUAGE", "en")
    stamp = datetime(2026, 10, 2)
    runner = cast(Any, SimpleNamespace(config=config))
    entry = SimpleNamespace(session_key="key", session_id="sid", created_at=stamp, updated_at=stamp)
    runner.async_session_store = SimpleNamespace(get_or_create_session=AsyncMock(return_value=entry))
    runner._running_agents, runner.adapters = {}, {Platform.SLACK: None}
    runner._queue_depth = lambda *args, **kwargs: 0
    runner._cached_agent_for = lambda key: None
    runner._status_session_db_facts = AsyncMock(return_value=(None, {}, 0, {}))
    monkeypatch.setattr("gateway.slash_commands_status._status_model_route", lambda *args: ("m", "p", 0, 0))
    evt = MessageEvent(text="/status", source=SessionSource(
        platform=Platform.SLACK, chat_id=HOME_DM, user_id="U0OWNER", chat_type="dm"))
    return await GatewayStatusCommandsMixin._handle_status_command(runner, evt)


@pytest.mark.asyncio
async def test_status_shows_the_config_load_failure(home, monkeypatch):
    text = await _status(monkeypatch, broken(home))
    assert "config.yaml failed to load (ParserError at line 2" in text
    assert "except the Slack home DM" in text and SECRET not in text


@pytest.mark.asyncio
async def test_status_is_unchanged_when_config_loaded(home, monkeypatch):
    (home / "config.yaml").write_text("{}", encoding="utf-8")
    assert "failed to load" not in await _status(monkeypatch, load_gateway_config())
