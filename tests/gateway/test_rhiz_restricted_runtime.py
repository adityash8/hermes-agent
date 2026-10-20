"""Rhiz runs on the ordinary Slack agent, narrowed by gateway.restricted_users (EZ-1072/EZ-1073).

The stateless sandbox lane these tests used to drive was retired (EZ-1093) and the module now
serves CookUnity only (EZ-1196); see test_cookunity_access for that lane. What remains to prove
is that the ordinary resolver fails closed for a configured sender and never inherits every
globally enabled MCP server. SPI reads stay pinned in test_rhiz_spi_reads.
"""
from types import SimpleNamespace

import pytest

from gateway import client_context_restricted as lane
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from plugins.platforms.slack.adapter import SlackAdapter
from plugins.platforms.slack.restricted_grant import NO_MCP, grant_toolsets

RHIZ = lane.USER
MCP = {"composio", "clawmem", "qmd", "semrush", "mixpanel", "tableau", "ubersuggest"}
LIVE_GRANT = ["clarify", "linear", "todo", "vision", "web", "mixpanel", "tableau", "meta-ads", "ubersuggest"]
OWNER_DEFAULT = ["clarify", "composio", "cronjob", "delegation", "file", "memory", "session_search",
                 "terminal", "todo", "web"]


def config(grant=..., *, channels=None, rhiz=True):
    slack = {}
    if rhiz:
        slack[RHIZ] = {"enabled_toolsets": LIVE_GRANT} if grant is ... else grant
    if channels:
        slack["_channels"] = channels
    return {"gateway": {"restricted_users": {"slack": slack}},
            "platform_toolsets": {"slack": list(OWNER_DEFAULT)},
            "mcp_servers": {name: {"enabled": True, "command": "must-not-start"} for name in MCP}}


def source(user=RHIZ, chat="COTHER", platform=Platform.SLACK):
    return SessionSource(platform, chat, scope_id=lane.WORKSPACE, user_id=user, chat_type="group")


@pytest.fixture
def resolve(monkeypatch):
    adapter = object.__new__(SlackAdapter)
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda s: adapter if s.platform == Platform.SLACK else None

    def run(cfg, src=None):
        monkeypatch.setattr("gateway.run._load_gateway_config", lambda: cfg)
        src = src or source()
        return set(runner._resolve_enabled_toolsets_for_source(cfg, src, src.platform.value))
    run.adapter, run.runner = adapter, runner
    return run


def test_live_shaped_grant_keeps_named_mcp_allowlist_only(resolve):
    tools = resolve(config())
    assert tools & MCP == {"mixpanel", "tableau", "ubersuggest"}
    assert not tools & {"terminal", "file", "memory", "delegation", "cronjob", "session_search", "composio"}


@pytest.mark.parametrize("grant", [["clarify", "todo", "web"], ["clarify", "todo", "web", "spi"]])
def test_grant_without_mcp_names_inherits_no_mcp_server(resolve, grant):
    tools = resolve(config({"enabled_toolsets": grant}))
    assert not tools & MCP
    assert {"clarify", "todo", "web"} <= tools
    assert not tools & {"terminal", "file", "memory", "delegation"}


@pytest.mark.parametrize("grant", [
    {"enabled_toolsets": []}, {"enabled_toolsets": "terminal"}, {"enabled_toolsets": {"terminal": True}},
    "terminal", ["terminal"],
])
def test_empty_or_malformed_rhiz_grant_fails_closed(resolve, grant):
    assert resolve(config(grant)) == set()


def test_unreadable_config_fails_closed(resolve, monkeypatch):
    def broken():
        raise OSError("config unavailable")
    monkeypatch.setattr("gateway.run._load_gateway_config", broken)
    assert resolve.adapter.toolsets_for_source(source()) == [NO_MCP]
    assert resolve.runner._resolve_enabled_toolsets_for_source(config(), source(), "slack") == []


def test_empty_adapter_override_never_widens(resolve, monkeypatch):
    monkeypatch.setattr(resolve.adapter, "toolsets_for_source", lambda s: [])
    assert resolve.runner._resolve_enabled_toolsets_for_source(config(), source(), "slack") == []


def test_unrelated_user_and_other_platform_keep_platform_default(resolve):
    cfg = config({"enabled_toolsets": []})
    owner = resolve(cfg, source(user="UOTHER"))
    assert {"terminal", "file", "memory", "delegation", "composio"} <= owner
    cfg["platform_toolsets"]["telegram"] = list(OWNER_DEFAULT)
    assert "composio" in resolve(cfg, source(platform=Platform.TELEGRAM))


def test_channel_policy_precedes_sender_grant(resolve):
    cfg = config(channels={"CPOLICY": {"enabled_toolsets": [NO_MCP]}, "CWIDE": {"enabled_toolsets": ["web", "qmd"]}})
    assert resolve(cfg, source(chat="CPOLICY")) == set()
    assert resolve(cfg, source(chat="CWIDE")) & MCP == {"qmd"}
    assert grant_toolsets(cfg, RHIZ, "CPOLICY") == [NO_MCP]


def test_retired_sandbox_lane_does_not_capture_rhiz():
    assert lane.selected(source()) is False
    assert lane.selected(source(chat=lane.CODEWORDS)) is False
