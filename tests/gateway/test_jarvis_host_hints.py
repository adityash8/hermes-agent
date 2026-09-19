"""Jarvis-only host-operation hints on Slack, without changing other platforms."""
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.slash_commands import GatewaySlashCommandsMixin
from hermes_cli.commands_platforms import slack_native_slashes


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.SLACK, Platform.TELEGRAM])
async def test_host_operation_hints_are_platform_scoped(monkeypatch, platform):
    runner = SimpleNamespace(
        adapters={},
        _failed_platforms={Platform.TELEGRAM: {"paused": False}},
        _pause_failed_platform=lambda *args, **kwargs: None,
    )
    event = SimpleNamespace(
        content="/platform pause telegram", source=SimpleNamespace(platform=platform),
    )
    pause = await GatewaySlashCommandsMixin._handle_platform_command(runner, event)
    monkeypatch.setattr("gateway.slash_commands._execute", lambda *args: SimpleNamespace(
        data={"bundles": [], "dir": "/tmp/.hermes/bundles"}, text="",
    ))
    bundles = await GatewaySlashCommandsMixin._handle_bundles_command(runner, event)
    if platform == Platform.SLACK:
        assert "hermes" not in (pause + bundles).lower()
        assert "/jarvis platform resume telegram" in pause
        assert "Jarvis skill bundles" in bundles
    else:
        assert "hermes gateway restart" in pause
        assert "hermes bundles create" in bundles


def test_default_advertised_slashes_have_no_backend_brand(monkeypatch):
    monkeypatch.setattr("hermes_cli.commands_platforms._iter_plugin_command_entries", lambda: [])
    for name, description, hint in slack_native_slashes():
        assert "hermes" not in (name + description + hint).lower()
