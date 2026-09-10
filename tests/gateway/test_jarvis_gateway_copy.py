"""Slack presentation changes must never rebrand dynamic command data."""

from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.slash_commands import (_BUSY_MODE_BEHAVIOR, GatewaySlashCommandsMixin,
                                    _slack_brand)
from gateway.slash_commands_status import GatewayStatusCommandsMixin
from gateway.run_busy import GatewayBusySessionMixin
from gateway.run_inbound import GatewayInboundMixin


PAYLOAD = "Hermes /hermes ~/.hermes/skills HERMES_HOME https://hermes.example/doc"


def event(text, platform):
    return MessageEvent(text=text, source=SessionSource(
        platform=platform, chat_id="c1", user_id="u1", chat_type="dm"))


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,brand", [(Platform.SLACK, "Jarvis"), (Platform.TELEGRAM, "Hermes")])
@pytest.mark.parametrize("surface", ["help", "commands", "version", "status", "pause", "paused_notice", "busy", "update"])
async def test_system_labels_preserve_dynamic_payloads(monkeypatch, platform, brand, surface):
    monkeypatch.setenv("HERMES_LANGUAGE", "en")
    runner = cast(Any, SimpleNamespace())
    evt = event("/" + surface, platform)
    if surface in {"help", "commands"}:
        monkeypatch.setattr("agent.skill_commands.get_skill_commands", lambda: {
            "/sample": {"description": PAYLOAD}})
        monkeypatch.setattr("hermes_cli.plugins.get_plugin_commands", lambda: {})
        runner._telegramized_command_reply = lambda _event, text: text
        handler = getattr(GatewaySlashCommandsMixin, f"_handle_{surface}_command")
        if surface == "commands":
            # Check all pages: a core description override and the dynamic skill tail.
            texts = [await handler(runner, event(f"/commands {page}", platform))
                     for page in range(1, 8)]
            text = "\n".join(texts)
        else:
            text = await handler(runner, evt)
            assert f"**{brand} Commands**" in text
        assert f"while {brand} is working" in text
        assert PAYLOAD in text
        if platform == Platform.SLACK:
            assert "`/jarvis status`" in text
            assert "`/jarvis reload-skills" in text
            assert "Re-scan ~/.hermes/skills" not in text
        else:
            assert "`/status`" in text
            assert "/jarvis" not in text
    elif surface == "version":
        monkeypatch.setattr("hermes_cli.banner.format_banner_version_label",
                            lambda: "Hermes Agent v1.0 · local Hermes-build")
        text = await GatewaySlashCommandsMixin._handle_version_command(runner, evt)
        assert text.startswith("Jarvis v" if platform == Platform.SLACK else "Hermes Agent v")
        assert text.endswith("Hermes-build")
    elif surface == "status":
        stamp = datetime(2026, 9, 7)
        entry = SimpleNamespace(session_key="key", session_id="Hermes-session",
                                created_at=stamp, updated_at=stamp)
        runner.async_session_store = SimpleNamespace(get_or_create_session=AsyncMock(return_value=entry))
        runner._running_agents = {}
        runner.adapters = {platform: None}
        runner._queue_depth = lambda *args, **kwargs: 0
        runner._cached_agent_for = lambda key: None
        runner._status_session_db_facts = AsyncMock(return_value=(PAYLOAD, {}, 0, {}))
        monkeypatch.setattr("gateway.slash_commands_status._status_model_route",
                            lambda *args: ("Hermes-model", "Hermes-provider", 0, 0))
        text = await GatewayStatusCommandsMixin._handle_status_command(runner, evt)
        assert f"**{brand} Gateway Status**" in text
        assert PAYLOAD in text
        assert "Hermes-model" in text and "Hermes-provider" in text and "Hermes-session" in text
    elif surface == "pause":
        monkeypatch.setattr("agent.estop.get_state", lambda: {"reason": PAYLOAD})
        text = await GatewayBusySessionMixin._handle_pause_command(runner, evt)
        assert f"{brand} is already paused" in text
        assert PAYLOAD in text
    elif surface == "paused_notice":
        monkeypatch.setattr("agent.estop.paused_reply", lambda:
                            f"⏸️ Hermes is paused ({PAYLOAD}). New work is on hold; run `hermes resume` to pick things back up.")
        monkeypatch.setattr("agent.estop.get_state", lambda: {"reason": PAYLOAD})
        runner._hm_estop_turn_allowed = lambda *args: False
        text = GatewayInboundMixin._hm_estop_gate(runner, evt, evt.source, False)
        assert text is not None
        assert text.startswith(f"⏸️ {brand} is paused")
        assert PAYLOAD in text
        if platform == Platform.SLACK:
            assert "`hermes resume`" not in text
            assert text.endswith("Ask an admin to resume Jarvis on the host.")
        else:
            assert "`hermes resume`" in text
    elif surface == "busy":
        monkeypatch.setattr("cli.save_config_value", lambda *args: True)
        runner._busy_profile_name_for_source = lambda source: None
        runner._load_busy_text_mode = lambda: "queue"
        runner._adapter_for_source = lambda source: None
        text = str(await GatewaySlashCommandsMixin._handle_busy_command(runner, event("/busy queue", platform)))
        assert f"while {brand} is busy" in text
    else:
        runner._UPDATE_ALLOWED_PLATFORMS = {platform}
        monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
        monkeypatch.setattr("gateway.run._resolve_hermes_bin", lambda: None)
        text = await GatewaySlashCommandsMixin._handle_update_command(runner, evt)
        assert f"{brand} is running" in text
        if platform == Platform.SLACK:
            assert "hermes" not in text.lower()
        else:
            assert "`hermes`" in text and "`hermes update`" in text


# Upstream prose the Slack copy must not depend on, reworded as a future upstream merge might.
DRIFTED_PAUSE = f"⏸️ Hermes is on hold ({PAYLOAD}) — run `hermes resume` when you are back."
DRIFTED_VERSION = "Hermes Agent 0.22 (2026-09-07) · local Hermes-build"
BUSY_WITH_MODEL = "Messages will be queued while Hermes is busy on Hermes-4-405B."
UPDATE_NOTICE = "⚕ Starting Hermes update… I'll stream progress here."


@pytest.mark.asyncio
async def test_slack_branding_survives_upstream_rewording(monkeypatch):
    """Slack branding must not hinge on upstream's exact wording, and must leave data tokens alone."""
    monkeypatch.setenv("HERMES_LANGUAGE", "en")
    runner = cast(Any, SimpleNamespace())
    evt = event("/version", Platform.SLACK)

    # Paused notice: rebuilt from the estop state, so a reworded upstream notice still reads Jarvis
    # and still never tells a Slack operator to run a host command. The reason stays verbatim.
    monkeypatch.setattr("agent.estop.paused_reply", lambda: DRIFTED_PAUSE)
    monkeypatch.setattr("agent.estop.get_state", lambda: {"reason": PAYLOAD})
    runner._hm_estop_turn_allowed = lambda *args: False
    assert GatewayInboundMixin._hm_estop_gate(runner, evt, evt.source, False) == (
        f"⏸️ Jarvis is paused ({PAYLOAD}). Ask an admin to resume Jarvis on the host.")

    # Version label: branded even when upstream drops the "v"; the build tag is data.
    monkeypatch.setattr("hermes_cli.banner.format_banner_version_label", lambda: DRIFTED_VERSION)
    assert await GatewaySlashCommandsMixin._handle_version_command(runner, evt) == (
        "Jarvis 0.22 (2026-09-07) · local Hermes-build")

    # Busy confirmation: the brand word only — a model name that starts with Hermes is data.
    monkeypatch.setitem(_BUSY_MODE_BEHAVIOR, "queue", ("queues for next turn", BUSY_WITH_MODEL))
    monkeypatch.setattr("cli.save_config_value", lambda *args: True)
    runner._busy_profile_name_for_source = lambda source: None
    runner._load_busy_text_mode = lambda: "queue"
    runner._adapter_for_source = lambda source: None
    busy = str(await GatewaySlashCommandsMixin._handle_busy_command(
        runner, event("/busy queue", Platform.SLACK)))
    assert "while Jarvis is busy on Hermes-4-405B." in busy

    # /update's own notice (its spawn path writes to the real Hermes home, so brand it directly).
    assert _slack_brand(UPDATE_NOTICE) == "⚕ Starting Jarvis update… I'll stream progress here."
