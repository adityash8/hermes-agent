"""Native Slack capability must reflect registered, usable tool schemas."""
import pytest


@pytest.mark.parametrize("enabled,registered,available,token_present,expected", [
    (True, True, True, True, True),
    (True, False, True, True, False),
    (False, True, True, True, False),
    (True, True, False, True, False),
    (True, True, True, False, False),
])
def test_native_slack_capability_checks_actual_tools(
    monkeypatch, enabled, registered, available, token_present, expected
):
    from gateway.session import _slack_tools_loaded
    from tools import registry as registry_module
    from tools.registry import ToolRegistry
    from agent.secret_scope import set_secret_scope, reset_secret_scope

    isolated = ToolRegistry()
    monkeypatch.setattr(registry_module, "registry", isolated)
    monkeypatch.setattr("tools.mcp_tool_discovery.get_registered_mcp_server_names", lambda: set())
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *a, **kw: {"slack"} if enabled else set())
    if registered:
        isolated.register(
            name="test_slack_read_message", toolset="slack",
            schema={"name": "test_slack_read_message", "description": "Offline fixture", "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kwargs: "{}", check_fn=lambda: available,
        )
    scope = set_secret_scope({"SLACK_BOT_TOKEN": "xoxb-offline-fixture"} if token_present else {})
    try:
        assert _slack_tools_loaded() is expected
    finally:
        reset_secret_scope(scope)
