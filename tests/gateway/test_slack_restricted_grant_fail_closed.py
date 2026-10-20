"""Slack restricted grants: [] means zero tools end-to-end, and ``_default`` is honored.

Precedence: explicit ``_channels[channel]`` > explicit sender > ``_default``. ``None`` (no
matching entry anywhere) stays unrestricted platform resolution.
"""
from types import SimpleNamespace

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.tools_config import _get_platform_tools
from model_tools import get_tool_definitions
from plugins.platforms.slack.restricted_grant import grant_toolsets

OWNER_DM = "D0OWNER"
CLIENT = "CCLIENT"
SENDER = "USENDER"
OTHER = "UOTHER"


def _cfg(slack):
    return {"gateway": {"restricted_users": {"slack": slack}},
            "platform_toolsets": {"slack": ["memory", "terminal", "file", "web", "delegation", "cronjob"]}}


def _runner(override):
    gr = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(toolsets_for_source=lambda source: override)
    gr._delivery_adapter_for = lambda source: adapter
    return gr


def _src(chat=CLIENT, user=SENDER):
    return SessionSource(Platform.SLACK, chat, user_id=user, chat_type="group")


# --- adapter-level grant selection ---------------------------------------------------------

def test_default_applies_when_no_channel_or_sender_entry():
    cfg = _cfg({"_default": {"enabled_toolsets": ["web"]}})
    assert grant_toolsets(cfg, OTHER, CLIENT) == ["web"]


def test_precedence_channel_then_sender_then_default():
    cfg = _cfg({"_default": {"enabled_toolsets": ["web"]},
                SENDER: {"enabled_toolsets": ["file"]},
                "_channels": {OWNER_DM: {"enabled_toolsets": ["terminal"]}}})
    assert grant_toolsets(cfg, SENDER, OWNER_DM) == ["terminal"]
    assert grant_toolsets(cfg, OTHER, OWNER_DM) == ["terminal"]
    assert grant_toolsets(cfg, SENDER, CLIENT) == ["file"]
    assert grant_toolsets(cfg, OTHER, CLIENT) == ["web"]


def test_empty_explicit_grants_stay_empty_and_do_not_fall_through():
    cfg = _cfg({"_default": {"enabled_toolsets": ["web"]},
                SENDER: {"enabled_toolsets": []},
                "_channels": {OWNER_DM: {"enabled_toolsets": []}}})
    assert grant_toolsets(cfg, OTHER, OWNER_DM) == []
    assert grant_toolsets(cfg, SENDER, CLIENT) == []
    assert grant_toolsets(_cfg({"_default": {"enabled_toolsets": []}}), OTHER, CLIENT) == []


def test_sender_null_grant_keeps_platform_default_over_default_entry():
    # The owner opt-out shape: an explicit sender entry with ``enabled_toolsets: null`` wins
    # over ``_default`` and means unrestricted, while explicit channels still take precedence.
    cfg = _cfg({"_default": {"enabled_toolsets": ["web"]},
                SENDER: {"enabled_toolsets": None},
                "_channels": {OWNER_DM: {"enabled_toolsets": ["terminal"]}}})
    assert grant_toolsets(cfg, SENDER, CLIENT) is None
    assert grant_toolsets(cfg, SENDER, OWNER_DM) == ["terminal"]
    assert grant_toolsets(cfg, OTHER, CLIENT) == ["web"]


def test_reserved_keys_are_never_sender_ids_and_missing_stays_unrestricted():
    cfg = _cfg({"_channels": {OWNER_DM: {"enabled_toolsets": ["web"]}}})
    assert grant_toolsets(cfg, "_channels", CLIENT) is None
    assert grant_toolsets(_cfg({"_default": {"enabled_toolsets": ["web"]}}), "_default", CLIENT) == ["web"]
    assert grant_toolsets(_cfg({SENDER: {"enabled_toolsets": ["web"]}}), OTHER, CLIENT) is None
    assert grant_toolsets(_cfg({"_default": {"system_prompt": "x"}}), OTHER, CLIENT) is None
    assert grant_toolsets({}, OTHER, CLIENT) is None
    # A missing sender id cannot bypass channel or _default grants.
    assert grant_toolsets(_cfg({"_default": {"enabled_toolsets": ["web"]}}), None, CLIENT) == ["web"]
    assert grant_toolsets(cfg, None, OWNER_DM) == ["web"]
    assert grant_toolsets(cfg, None, CLIENT) is None


# --- gateway resolver: effective tools -----------------------------------------------------

def test_empty_override_resolves_to_zero_effective_tools():
    cfg = _cfg({SENDER: {"enabled_toolsets": []}})
    # Expanding [] through platform resolution leaks platform-native/plugin toolsets.
    enabled = GatewayRunner._resolve_enabled_toolsets_for_source(_runner([]), cfg, _src(), "slack")
    assert enabled == []
    assert get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True) == []


def test_none_override_remains_unrestricted_platform_resolution():
    cfg = _cfg({})
    enabled = GatewayRunner._resolve_enabled_toolsets_for_source(_runner(None), cfg, _src(), "slack")
    assert enabled == sorted(_get_platform_tools(cfg, "slack"))
    assert {"memory", "terminal"} <= set(enabled)


def test_nonempty_override_still_validated_through_platform_path():
    cfg = _cfg({})
    enabled = GatewayRunner._resolve_enabled_toolsets_for_source(_runner(["file", "discord_admin"]), cfg, _src(), "slack")
    expected = sorted(_get_platform_tools({**cfg, "platform_toolsets": {"slack": ["file", "discord_admin"]}}, "slack"))
    assert enabled == expected and "memory" not in enabled and "discord_admin" not in enabled


def test_end_to_end_default_and_empty_grants_through_adapter(monkeypatch):
    from plugins.platforms.slack.adapter import SlackAdapter
    cfg = _cfg({"_default": {"enabled_toolsets": ["web"]},
                SENDER: {"enabled_toolsets": []},
                "_channels": {OWNER_DM: {"enabled_toolsets": ["memory", "terminal"]}}})
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: cfg)
    adapter = object.__new__(SlackAdapter)
    gr = object.__new__(GatewayRunner)
    gr._delivery_adapter_for = lambda source: adapter
    resolve = lambda src: GatewayRunner._resolve_enabled_toolsets_for_source(gr, cfg, src, "slack")
    assert resolve(_src(CLIENT, SENDER)) == []
    other = resolve(_src(CLIENT, OTHER))
    assert other == sorted(_get_platform_tools({**cfg, "platform_toolsets": {"slack": ["web"]}}, "slack"))
    assert "memory" not in other and "terminal" not in other
    dm = resolve(_src(OWNER_DM, SENDER))
    assert {"memory", "terminal"} <= set(dm)
    assert cfg["platform_toolsets"]["slack"] == ["memory", "terminal", "file", "web", "delegation", "cronjob"]
