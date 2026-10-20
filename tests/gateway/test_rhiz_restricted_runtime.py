"""Current canonical restricted-runtime contract (filename retained for manifests).

Retirement: the Rhiz/SPI-specific route asserted by this module was replaced by a
CookUnity-only lane in baseline commit 7cf1d17a2c. The old suite required the
removed Rhiz/SPI route and is not a contract for ordinary Jarvis permissions.

Coverage now maps to the live contract as follows:
- Native CookUnity selection, user/workspace admission, receipt binding, and
  exact model tools: tests here plus test_cookunity_access's
  test_native_exact_humans_isolated_sdk_and_reply,
  test_real_authz_requires_unforgeable_receipt_and_preserves_rhiz_elsewhere,
  and test_denials_never_fall_back.
- CookUnity delivery, revocation, bounded reads, and safe tool execution:
  test_cookunity_access's test_native_model_read_roundtrip_uses_only_fixed_transport,
  test_before_reply_revalidation_silently_drops, and
  test_native_reply_fails_closed_during_formatting.
- Generic Slack admission, replay, and legacy fallback boundaries:
  test_client_context's test_native_event_and_plugin_admission_uses_actor_tuple,
  test_native_scoped_admission_keeps_replay_dedup, and
  test_unknown_route_directly_after_admission_never_uses_legacy; plus
  test_slack_channel_isolation's test_revoked_native_source_cannot_be_reused_for_background_or_hooks.
- Ordinary Rhiz grant resolution remains separately covered by
  test_cookunity_access::test_rhiz_ordinary_admission_and_toolset_resolver_remain_legacy
  and test_slack_channel_isolation::test_channel_grant_never_changes_global_authorization.

These tests do not claim generic ordinary-Jarvis privacy isolation.
"""
from copy import copy

import pytest

from gateway import client_context_cookunity as cookunity
from gateway import client_context_restricted as route
from gateway.client_context_policy import ContextDenied
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tests.gateway.test_client_context import corpus, runner, wire  # register shared fixture dependencies
from tests.gateway.test_slack_channel_isolation import native_text  # register native adapter fixture
from tests.gateway.test_cookunity_access import connectors, invoke, native


def test_current_lane_selects_cookunity_destination_not_rhiz_or_other_platform():
    cookunity_source = SessionSource(
        Platform.SLACK,
        cookunity.CHANNEL,
        scope_id=cookunity.WORKSPACE,
        user_id="U08R89W3X16",
        chat_type="group",
    )
    rhiz_source = SessionSource(
        Platform.SLACK,
        "COTHER",
        scope_id=cookunity.WORKSPACE,
        user_id=route.USER,
        chat_type="group",
    )
    non_slack_source = SessionSource(
        Platform.TELEGRAM,
        cookunity.CHANNEL,
        scope_id=cookunity.WORKSPACE,
        user_id="U08R89W3X16",
        chat_type="group",
    )

    assert route.selected(cookunity_source) is True
    assert route.selected(rhiz_source) is False
    assert route.selected(non_slack_source) is False


@pytest.mark.asyncio
async def test_native_receipt_binds_verified_user_workspace_channel_and_client(native, runner):
    from plugins.platforms.slack.client_context import admit_event

    adapter, client, event = native
    mode, source = await admit_event(adapter, event, {"team_id": cookunity.WORKSPACE})

    assert mode == "scoped"
    receipt = route.native(source, adapter)
    assert receipt.client is client
    assert receipt.identity == route.identity(source)

    # Exercise the real authorization branch against this same receiving adapter.
    runner._delivery_adapter_for.return_value = adapter
    assert GatewayRunner._is_user_authorized(runner, source) is True
    forged = copy(source)
    del forged._restricted_native
    assert GatewayRunner._is_user_authorized(runner, forged) is False

    for field, value in (("scope_id", "TOTHER"), ("chat_id", "COTHER"), ("user_id", "UOTHER")):
        changed = copy(source)
        setattr(changed, field, value)
        with pytest.raises(ContextDenied):
            route.native(changed, adapter)

    with pytest.raises(ContextDenied):
        route.native(source, object())

    adapter._team_clients[cookunity.WORKSPACE] = object()
    try:
        with pytest.raises(ContextDenied):
            route.native(source, adapter)
    finally:
        adapter._team_clients[cookunity.WORKSPACE] = client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
        "spi_ga4_report",
        "spi_gsc_report",
        "public_web_search",
        "terminal",
        "read_file",
        "mcp_composio_new_dynamic_tool",
    ],
)
async def test_current_toolset_excludes_retired_spi_and_generic_dispatch(native, connectors, wire, tool_name):
    adapter, client, _ = native
    wire.state.finish = "tool_calls"
    wire.state.tool_calls = [
        {
            "id": "untrusted-call",
            "type": "function",
            "function": {"name": tool_name, "arguments": "{}"},
        }
    ]

    await invoke(native)

    assert len(wire.captured) == 1
    exposed = {tool["function"]["name"] for tool in wire.captured[0]["tools"]}
    assert exposed == set(cookunity.METHODS) | {"cookunity_slack_history_read"}
    assert not any(name.startswith("spi_") for name in exposed)
    assert tool_name not in exposed
    for server in connectors.servers.values():
        server.session.call_tool.assert_not_awaited()
    client.chat_postMessage.assert_not_awaited()
