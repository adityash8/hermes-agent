"""Local Slack branding preserves shared commands and user-owned manifest content."""

import argparse
import json

import pytest

from hermes_cli import commands_platforms
from hermes_cli.commands import COMMAND_REGISTRY, gateway_help_lines
from hermes_cli.slack_cli import _build_full_manifest, slack_manifest_command
from hermes_cli.subcommands.slack import build_slack_parser


@pytest.mark.parametrize("experience", ["assistant", "agent"])
def test_manifest_uses_jarvis_defaults_and_preserves_custom_content(experience, capsys):
    parser = argparse.ArgumentParser()
    build_slack_parser(parser.add_subparsers(dest="command"), cmd_slack=lambda args: 0)
    argv = ["slack", "manifest"] + (["--agent-view"] if experience == "agent" else [])
    assert slack_manifest_command(parser.parse_args(argv)) == 0
    manifest = json.loads(capsys.readouterr().out)
    display = manifest["display_information"]
    assert display["name"] == manifest["features"]["bot_user"]["display_name"] == "Jarvis"
    assert display["description"] == "Your Jarvis agent on Slack"
    assert manifest["features"][f"{experience}_view"][f"{experience}_description"].startswith(
        "Chat with Jarvis "
    )
    entries = manifest["features"]["slash_commands"]
    assert entries[0]["command"] == "/jarvis"
    assert entries[0]["description"] == "Talk to Jarvis or run a subcommand"
    assert "/hermes" not in {entry["command"] for entry in entries}
    assert {entry["url"] for entry in entries} == {"https://hermes-agent.local/slack/commands"}
    assert all(entry["should_escape"] is False for entry in entries)

    content = "Hermes docs: `hermes setup`, ~/.hermes/skills/, https://hermes.example/doc."
    custom = _build_full_manifest("Hermes custom app", content, long_description=content * 3)
    assert custom["display_information"]["name"] == "Hermes custom app"
    assert custom["display_information"]["description"] == content
    assert custom["display_information"]["long_description"] == content * 3


def test_slack_builtin_help_is_branded_without_rewriting_plugin_content(monkeypatch):
    content = "Hermes docs: `hermes setup`, ~/.hermes/skills/, https://hermes.example/doc."
    monkeypatch.setattr(commands_platforms, "_iter_plugin_command_entries", lambda: [
        ("hermes", "Do not advertise legacy entry point", ""),
        ("jarvis", "Do not override primary entry point", ""),
        ("custom", content, "[payload]"),
    ])
    # Keep room for plugins so the collision behavior is exercised independently of Slack's cap.
    selected = {"reload-skills", "snapshot", "goal", "busy", "plan", "version", "update"}
    monkeypatch.setattr(commands_platforms, "_gateway_available_commands", lambda: [
        cmd for cmd in COMMAND_REGISTRY if cmd.name in selected
    ])
    slashes = {name: description for name, description, _hint in commands_platforms.slack_native_slashes()}
    assert slashes["jarvis"] == "Talk to Jarvis or run a subcommand"
    assert "hermes" not in slashes
    assert slashes["custom"] == content
    assert slashes["reload-skills"] == "Refresh Jarvis skills after installs or removals"
    assert "Jarvis" in slashes["snapshot"]
    assert slashes["plan"] == "Write a Jarvis implementation plan without executing anything"
    lines = commands_platforms.slack_gateway_help_lines()
    assert all(line.startswith("`/jarvis ") for line in lines)
    assert any("`/jarvis version` -- Show Jarvis version" in line for line in lines)
    assert any("(alias: `/jarvis v`)" in line for line in lines)
    assert any("Refresh Jarvis skills after installs or removals" in line for line in lines)
    assert not any(line.startswith("`/jarvis hermes") for line in lines)
    # Shared gateway and Telegram copy retains its original source strings.
    original = {cmd.name: cmd.description for cmd in COMMAND_REGISTRY}
    telegram = dict(commands_platforms.telegram_bot_commands(include_plugins=False))
    assert telegram["reload_skills"] == original["reload-skills"]
    assert telegram["snapshot"] == original["snapshot"]
    assert any("Show Hermes Agent version" in line for line in gateway_help_lines())
