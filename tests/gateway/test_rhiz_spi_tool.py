"""The spi toolset reaches only Rhiz's Slack grant and re-checks the bound turn per call (EZ-1074)."""
import json

import pytest

import toolsets
from gateway import client_context_restricted as lane
from gateway.session_context import clear_session_vars, set_session_vars
from hermes_cli.tools_config import _platform_default_keys
from tools import rhiz_spi_tool
from tools.registry import registry

SPI_TOOLS = {"spi_ga4_report", "spi_gsc_report"}


def config(grant=("clarify", "todo", "web", "spi")):
    return {"gateway": {"restricted_users": {"slack": {lane.USER: {"enabled_toolsets": list(grant)}}}}}


@pytest.fixture
def turn(monkeypatch):
    tokens = []

    def bind(cfg=None, **source):
        monkeypatch.setattr("gateway.run._load_gateway_config", lambda: config() if cfg is None else cfg)
        values = {"platform": "slack", "user_id": lane.USER, "scope_id": lane.WORKSPACE, "chat_id": "C1"}
        tokens.append(set_session_vars(**{**values, **source}))
    yield bind
    for t in tokens:
        clear_session_vars(t)


def test_spi_toolset_is_registered_and_never_a_platform_default():
    assert set(toolsets.resolve_toolset("spi")) == SPI_TOOLS
    assert {registry.get_toolset_for_tool(name) for name in SPI_TOOLS} == {"spi"}
    assert "spi" not in _platform_default_keys()
    for name, entry in toolsets.TOOLSETS.items():
        if name != "spi":
            assert not SPI_TOOLS & set(toolsets.resolve_toolset(name)), name


def test_rhiz_slack_turn_with_spi_grant_passes_guard(turn):
    turn()
    assert rhiz_spi_tool._rhiz_turn() is True


@pytest.mark.parametrize("source", [
    {"platform": "telegram"}, {"user_id": "UOWNER"}, {"scope_id": "TOTHER"}, {"platform": ""},
])
def test_other_sources_fail_guard(turn, source):
    turn(**source)
    assert rhiz_spi_tool._rhiz_turn() is False


@pytest.mark.parametrize("cfg", [config(("clarify", "web")), config(()), {}, None])
def test_grant_without_spi_fails_guard(turn, cfg):
    turn(cfg if cfg is not None else {"gateway": "broken"})
    assert rhiz_spi_tool._rhiz_turn() is False


def test_channel_policy_without_spi_overrides_sender_grant(turn):
    cfg = config()
    cfg["gateway"]["restricted_users"]["slack"]["_channels"] = {"C1": {"enabled_toolsets": ["web"]}}
    turn(cfg)
    assert rhiz_spi_tool._rhiz_turn() is False


def test_unbound_context_ignores_environment(monkeypatch):
    for key, value in {"HERMES_SESSION_PLATFORM": "slack", "HERMES_SESSION_USER_ID": lane.USER,
                       "HERMES_SESSION_SCOPE_ID": lane.WORKSPACE}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("gateway.run._load_gateway_config", config)
    assert rhiz_spi_tool._rhiz_turn() is False


def test_unreadable_config_fails_guard(turn, monkeypatch):
    turn()

    def broken():
        raise OSError("config unavailable")
    monkeypatch.setattr("gateway.run._load_gateway_config", broken)
    assert rhiz_spi_tool._rhiz_turn() is False


def test_handler_denies_before_any_io_for_non_rhiz(turn, monkeypatch):
    turn(user_id="UOWNER")
    monkeypatch.setattr("gateway.client_context_rhiz_reads._credential",
                        lambda: pytest.fail("credential read for a denied turn"))
    handler = registry.get_entry("spi_ga4_report").handler
    result = handler({"start_date": "2026-09-01", "end_date": "2026-09-07"})
    assert "denied" in json.loads(result)["error"]
