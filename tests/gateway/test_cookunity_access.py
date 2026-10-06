"""EZ-1196 native isolation with synthetic Slack/MCP and real provider SDK transport."""
import asyncio
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import hermes_yaml as yaml

from gateway import client_context_cookunity as cu
from gateway import client_context_restricted as lane
from gateway.client_context_policy import ContextDenied
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tests.gateway.test_client_context import corpus, forbidden, runner, wire
from tests.gateway.test_slack_channel_isolation import native_text, drain_native


@pytest.fixture
def connectors(tmp_path, monkeypatch):
    from tools import mcp_tool as core
    from tools.mcp_tool_scope import _server_key
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    def serve():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    monkeypatch.setattr(core, "_mcp_loop", loop)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    servers, raw = {}, {"mcp_servers": {}}
    for name in cu.BINDINGS:
        tools = []
        for _, (owner, remote, props, required) in cu.METHODS.items():
            if owner != name:
                continue
            props = copy.deepcopy(props)
            if name == "mixpanel":
                props = {"project_id": {}, "workspace_id": {}, "query": {}} if remote[0] != "Run-Query" else {
                    "project_id": {}, "workspace_id": {}, "report": {}, "report_type": {}, "skip_results": {}}
                required = ["project_id"]
            elif remote[0] in {"list-workbooks", "list-views"}:
                props = {"limit": {}}
            elif remote[0] == "search-content":
                props = {"terms": {}, "limit": {}, "filter": {}}
            tools.append(SimpleNamespace(name=remote[0], description="CookUnity read", annotations={"readOnlyHint": True},
                                         inputSchema={"type": "object", "properties": props, "required": required}))
        config = {"enabled": True, "url": f"https://{name}.synthetic.invalid/mcp",
                  "sampling": {"enabled": False}, "elicitation": {"enabled": False}}
        raw["mcp_servers"][name] = config
        server = SimpleNamespace(name=name, _config=copy.deepcopy(config), _tools=tools,
                                 _sampling=None, _elicitation=None, _rpc_lock=asyncio.Lock(),
                                 session=SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(
                                     isError=False, content=[SimpleNamespace(type="text", text='{"metric":42}')]))))
        servers[name] = server
        monkeypatch.setitem(core._servers, _server_key(name), server)
        monkeypatch.setitem(cu._scoped_servers, name, server)
    value = {"enabled": True, "generation": "EZ-1196-test", "expires_at": "2099-01-01T00:00:00Z",
             "provider": "openai", "model": "synthetic-main", "servers": {name: {
                 "binding": copy.deepcopy(binding), "declaration_sha256": cu.digest(raw["mcp_servers"][name]),
                 "live_config_sha256": cu.digest(servers[name]._config),
                 "tools_sha256": cu.digest(cu.metadata(servers[name]))} for name, binding in cu.BINDINGS.items()}}
    path = tmp_path / "cookunity-read-grant.json"
    def save():
        path.write_text(json.dumps(value), encoding="utf-8")
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    save()
    yield SimpleNamespace(servers=servers, raw=raw, value=value, save=save, path=path)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(2)
    assert not thread.is_alive()
    loop.close()


@pytest.fixture
def native(connectors, native_text, runner):
    adapter, client, event = native_text
    event.update(channel=cu.CHANNEL, user="U08R89W3X16")
    client.users_info.return_value["user"].update(id=event["user"], team_id=cu.WORKSPACE)
    adapter.set_authorization_check(lambda *a, **k: True)
    runner._agent_cache = {"old-full": "PRIVATE_AGENT"}
    runner._client_context_history = {"old-full": "PRIVATE_HISTORY"}
    return adapter, client, event


async def invoke(native, body=None):
    adapter, _, event = native
    await adapter._handle_slack_message(event, {"team_id": cu.WORKSPACE} if body is None else body)
    await drain_native(adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize("user", sorted(cu.USERS))
@pytest.mark.parametrize("event_type", ["message", "app_mention"])
async def test_native_exact_humans_isolated_sdk_and_reply(native, runner, wire, user, event_type):
    adapter, client, event = native
    event["user"] = user
    event["type"] = event_type
    if event_type == "app_mention":
        event.pop("channel_type", None)
    client.users_info.return_value["user"]["id"] = user
    await invoke(native)
    assert len(wire.captured) == 1
    body = wire.captured[0]
    assert {t["function"]["name"] for t in body["tools"]} == set(cu.METHODS)
    assert "PRIVATE_" not in json.dumps(body)
    assert client.chat_postMessage.await_count == 1
    assert client.chat_postMessage.call_args.kwargs["channel"] == cu.CHANNEL
    assert runner._agent_cache == {"old-full": "PRIVATE_AGENT"}
    assert runner._client_context_history == {"old-full": "PRIVATE_HISTORY"}
    assert runner.session_store is None
    for name in ("_run_agent", "_hmwa_prepare_turn", "_hmwa_resolve_session", "_hm_dispatch_idle_commands"):
        getattr(runner, name).assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["dm", "channel", "workspace", "missing_workspace", "user", "bot", "file", "slash", "bang", "receipt", "grant", "server", "callbacks"])
async def test_denials_never_fall_back(native, runner, wire, connectors, variant):
    adapter, client, event = native
    body = {"team_id": cu.WORKSPACE}
    if variant in {"dm", "channel"}:
        event["channel"] = "DOTHER" if variant == "dm" else "COTHER"
        event["channel_type"] = "im" if variant == "dm" else "channel"
    elif variant in {"workspace", "missing_workspace"}:
        body = {"team_id": "TOTHER"} if variant == "workspace" else {}
    elif variant == "user":
        event["user"] = "UOTHER"
    elif variant == "bot":
        client.users_info.return_value["user"]["is_bot"] = True
    elif variant == "file":
        event["files"] = [{}]
    elif variant in {"slash", "bang"}:
        event["text"] = "<@U0B2X1D8R17> " + ("/model" if variant == "slash" else "!approve")
    elif variant == "receipt":
        adapter._client_context_admission = None
        adapter.client_context_enabled = False
    elif variant == "grant":
        connectors.value["enabled"] = False
        connectors.save()
    elif variant == "server":
        connectors.servers["mixpanel"]._config["url"] = "https://foreign.invalid"
    else:
        connectors.servers["mixpanel"]._sampling = object()
    adapter._prefilter_inbound = AsyncMock(side_effect=forbidden)
    await invoke(native, body)
    assert not wire.captured
    client.chat_postMessage.assert_not_called()
    adapter._prefilter_inbound.assert_not_called()


@pytest.mark.asyncio
async def test_bare_mention_wakes_exact_prior_question_and_ack_silent(native, wire):
    adapter, client, event = native
    question = "<@U07MJJMMK9T> <@U0B2X1D8R17> please help me understand the CAC trend for Babish's campaign over the last 4 weeks and what are the drivers (specific campaigns, assets, changes in spend)"
    event.update(ts="1791307698.143179", thread_ts="1791307598.143179", text="<@U0B2X1D8R17>", user="U07MJJMMK9T")
    client.users_info.return_value["user"]["id"] = event["user"]
    client.conversations_replies.return_value["messages"] = [{"ts": event["thread_ts"], "user": "U08R89W3X16", "text": question}]
    await invoke(native)
    assert len(wire.captured) == 1 and question.replace("<@U0B2X1D8R17>", "").strip() in json.loads(wire.captured[0]["messages"][1]["content"])["current_request"]
    client.conversations_replies.assert_awaited_once_with(channel=cu.CHANNEL, ts=event["thread_ts"], limit=15)
    for i, text in enumerate(("<@U0B2X1D8R17> thanks", "<@U0B2X1D8R17> FYI", "<@U0B2X1D8R17> no reply needed")):
        event.update(ts=f"179130779{i}.143179", text=text)
        await invoke(native)
    assert len(wire.captured) == 1
    assert adapter._participation_chatter("<@U0B2X1D8R17>") is True  # Global silence remains.


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["foreign_thread", "empty", "control", "grant_changed"])
async def test_bare_wake_cannot_borrow_foreign_or_invalid_history(native, wire, connectors, variant):
    adapter, client, event = native
    event["text"] = "<@U0B2X1D8R17>"
    rows = [{"ts": "100.0", "user": event["user"], "text": "please explain CAC"}]
    if variant == "foreign_thread":
        rows[0].update(ts="99.0", thread_ts="99.0")
    elif variant == "empty":
        rows = []
    elif variant == "control":
        rows[0]["text"] = "/model"
    else:
        async def changed(**kwargs):
            connectors.value["generation"] = "revoked"
            connectors.save()
            return {"ok": True, "messages": rows}
        client.conversations_replies.side_effect = changed
    client.conversations_replies.return_value["messages"] = rows
    await invoke(native)
    assert not wire.captured
    client.chat_postMessage.assert_not_called()


def test_fixed_reads_pin_resources_and_reject_model_dispatch(connectors):
    result = cu.execute("cookunity_mixpanel_query", '{"event":"Purchase","weeks":4,"breakdown":"Campaign"}', lambda: None)
    assert "42" in result
    call = connectors.servers["mixpanel"].session.call_tool.call_args
    assert call.args == ("Run-Query",)
    assert call.kwargs["arguments"]["project_id"] == 396627
    assert call.kwargs["arguments"]["workspace_id"] == 2442595
    for name, args in [("delete_dashboard", {}), ("terminal", {}), ("cookunity_mixpanel_events", {"project_id": 1}),
                       ("cookunity_mixpanel_events", {"tool": "delete"}), ("cookunity_mixpanel_query", {"event": "Purchase", "weeks": 4, "report": {"project_id": 1}}),
                       ("cookunity_tableau_workbooks", {"site": "other"}), ("cookunity_tableau_data", {"viewId": "https://foreign.invalid"}),
                       ("cookunity_tableau_search", {"terms": "campaign", "connection": "other"})]:
        with pytest.raises(ContextDenied):
            cu.execute(name, json.dumps(args), lambda: None)
    for raw in ('{"query":"a","query":"b"}', '{"query":NaN}', '[]'):
        with pytest.raises((ContextDenied, ValueError)):
            cu.validate("cookunity_mixpanel_events", raw)


@pytest.mark.parametrize("variant", ["grant", "declaration", "live", "metadata", "session"])
def test_mid_read_changes_discard_output(connectors, variant):
    server = connectors.servers["mixpanel"]
    async def mutate(*a, **k):
        if variant == "grant":
            connectors.value["enabled"] = False
            connectors.save()
        elif variant == "declaration":
            connectors.raw["mcp_servers"]["mixpanel"]["enabled"] = False
            connectors.save()
        elif variant == "live":
            server._config["url"] = "https://foreign.invalid"
        elif variant == "metadata":
            server._tools[0].description = "changed"
        else:
            server.session = object()
        return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text="must be discarded")])
    server.session.call_tool.side_effect = mutate
    with pytest.raises(ContextDenied):
        cu.execute("cookunity_mixpanel_events", "{}", lambda: None)


@pytest.mark.asyncio
async def test_real_authz_requires_unforgeable_receipt_and_preserves_rhiz_elsewhere(native, runner):
    from plugins.platforms.slack.client_context import admit_event
    adapter, _, event = native
    _, source = await admit_event(adapter, event, {"team_id": cu.WORKSPACE})
    assert GatewayRunner._is_user_authorized(runner, source) is True
    for user in sorted(cu.EXTRA_USERS):
        forged = SessionSource(Platform.SLACK, cu.CHANNEL, scope_id=cu.WORKSPACE, user_id=user, chat_type="group")
        assert GatewayRunner._is_user_authorized(runner, forged) is False
    source.scope_id = "TOTHER"
    assert GatewayRunner._is_user_authorized(runner, source) is False
    rhiz = SessionSource(Platform.SLACK, "COTHER", scope_id=cu.WORKSPACE, user_id=lane.USER, chat_type="group")
    assert lane.selected(rhiz) is False
    from plugins.platforms.slack.restricted_grant import grant_toolsets
    config = {"gateway": {"restricted_users": {"slack": {lane.USER: {"enabled_toolsets": ["terminal", "file", "linear"]}}}}}
    assert grant_toolsets(config, lane.USER, "COTHER") == ["terminal", "file", "linear"]


@pytest.mark.asyncio
@pytest.mark.parametrize("home_team", [None, "TEXTERNAL"])
async def test_slack_connect_home_team_is_not_receiver_workspace(native, wire, home_team):
    _, client, _ = native
    client.users_info.return_value["user"]["team_id"] = home_team
    await invoke(native)
    assert len(wire.captured) == 1


@pytest.mark.asyncio
async def test_native_model_read_roundtrip_uses_only_fixed_transport(native, connectors, wire):
    def respond(body):
        first = len(wire.captured) == 1
        wire.state.tool_calls = [{"id": "read-1", "type": "function", "function": {
            "name": "cookunity_mixpanel_query", "arguments": '{"event":"Purchase","weeks":4}'}}] if first else None
        wire.state.text = None if first else "CookUnity Purchase count is 42 from Mixpanel."
        wire.state.finish = "tool_calls" if first else "stop"
    wire.state.callback = respond
    await invoke(native)
    assert len(wire.captured) == 2
    assert connectors.servers["mixpanel"].session.call_tool.await_count == 1
    assert "42" in wire.captured[1]["messages"][-1]["content"]
    assert native[1].chat_postMessage.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["grant", "bot", "client"])
async def test_before_reply_revalidation_silently_drops(native, connectors, wire, variant):
    adapter, client, event = native
    original = adapter._extract_response_content
    async def extract(*args, **kwargs):
        result = await original(*args, **kwargs)
        if variant == "grant":
            connectors.value["generation"] = "revoked-before-delivery"
            connectors.save()
        elif variant == "bot":
            adapter._team_bot_user_ids[cu.WORKSPACE] = "UOTHERBOT"
        elif variant == "client":
            adapter._team_clients[cu.WORKSPACE] = AsyncMock()
        return result
    adapter._extract_response_content = extract
    await invoke(native)
    assert len(wire.captured) == 1
    client.chat_postMessage.assert_not_called()


@pytest.mark.parametrize("name,args", [
    ("cookunity_tableau_workbooks", {}), ("cookunity_tableau_views", {}),
    ("cookunity_tableau_workbook", {"workbookId": "11111111-2222-3333-4444-555555555555"}),
    ("cookunity_tableau_view", {"viewId": "11111111-2222-3333-4444-555555555555"}),
    ("cookunity_tableau_data", {"viewId": "11111111-2222-3333-4444-555555555555"}),
    ("cookunity_tableau_search", {"terms": "Babish"}),
])
def test_tableau_positive_methods_stay_on_pinned_site(connectors, name, args):
    cu.execute(name, json.dumps(args), lambda: None)
    call = connectors.servers["tableau"].session.call_tool.call_args
    assert call.args == (cu.METHODS[name][1][0],)
    assert "site" not in call.kwargs["arguments"] and "host" not in call.kwargs["arguments"]
    assert connectors.value["servers"]["tableau"]["binding"] == cu.BINDINGS["tableau"]


@pytest.mark.parametrize("variant", ["missing", "expired", "malformed", "binding", "malformed_config", "disabled"])
def test_parent_artifact_and_declaration_fail_closed(connectors, variant):
    if variant == "missing":
        connectors.path.unlink()
    elif variant == "malformed":
        connectors.path.write_text("{}", encoding="utf-8")
    elif variant == "malformed_config":
        (connectors.path.parent / "config.yaml").write_text("mcp_servers: [", encoding="utf-8")
    else:
        if variant == "expired":
            connectors.value["expires_at"] = "2020-01-01T00:00:00Z"
        elif variant == "binding":
            connectors.value["servers"]["mixpanel"]["binding"]["project_id"] = 1
        else:
            connectors.raw["mcp_servers"]["tableau"]["enabled"] = False
        connectors.save()
    with pytest.raises(Exception):
        cu.policy()
    for server in connectors.servers.values():
        server.session.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_admin_interactions_denied_even_if_client_context_off(native):
    from plugins.platforms.slack.client_context import interaction_is_legacy
    adapter, _, _ = native
    adapter.client_context_enabled = False
    for user in cu.USERS | {"UOTHER"}:
        assert await interaction_is_legacy(adapter, {"user": {"id": user}, "channel": {"id": cu.CHANNEL}}) is False
    for user in cu.EXTRA_USERS:
        assert await interaction_is_legacy(adapter, {"user": {"id": user}, "channel": {"id": "DOTHER"}}) is False


@pytest.mark.asyncio
async def test_rhiz_ordinary_admission_and_toolset_resolver_remain_legacy(native, runner, monkeypatch):
    from gateway import client_context as cc
    runner.config.client_context = {"enabled": False}
    source = SessionSource(Platform.SLACK, "COTHER", scope_id=cu.WORKSPACE, user_id=lane.USER, chat_type="group")
    assert await cc.admission(runner, source) == "legacy"
    config = {"gateway": {"restricted_users": {"slack": {lane.USER: {
        "enabled_toolsets": ["terminal", "file", "linear"]}}}},
        "platform_toolsets": {"slack": ["memory", "terminal", "file", "linear", "delegation"]}}
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: config)
    from hermes_cli.tools_config import _get_platform_tools
    expected = sorted(_get_platform_tools({"platform_toolsets": {"slack": ["terminal", "file", "linear"]}}, "slack"))
    assert runner._resolve_enabled_toolsets_for_source(config, source, "slack") == expected
    source.user_id = "UOTHER"
    assert "memory" in runner._resolve_enabled_toolsets_for_source(config, source, "slack")


def test_dedicated_configs_disable_callbacks_without_mutating_shared(monkeypatch):
    from contextlib import nullcontext
    shared = {name: {"enabled": True, "url": f"https://{name}.synthetic.invalid/mcp",
                    "sampling": {"enabled": True}, "elicitation": {"enabled": True}}
              for name in cu.BINDINGS}
    before = copy.deepcopy(shared)
    monkeypatch.setattr("tools.mcp_tool_config._load_mcp_config", lambda: shared)
    monkeypatch.setattr("tools.mcp_tool_discovery._owner_secret_scope", nullcontext)
    configs = cu.resolved_connector_configs()
    assert shared == before
    assert set(configs) == set(cu.BINDINGS)
    for config in configs.values():
        assert config["sampling"] == {"enabled": False}
        assert config["elicitation"] == {"enabled": False}


def test_no_adoption_of_shared_servers(connectors, monkeypatch):
    monkeypatch.setattr(cu, "_scoped_servers", {})
    with pytest.raises(ContextDenied):
        cu.bound("mixpanel", cu.grant())


def test_cold_start_requires_pinned_sanitized_configuration(connectors, monkeypatch):
    monkeypatch.setattr(cu, "_scoped_servers", {})
    configs = copy.deepcopy(connectors.raw["mcp_servers"])
    monkeypatch.setattr(cu, "resolved_connector_configs", lambda: configs)
    def start(actual):
        assert actual == configs
        cu._scoped_servers.update(connectors.servers)
    starter = Mock(side_effect=start)
    monkeypatch.setattr(cu, "start_scoped_connectors", starter)
    cu.ensure_ready()
    starter.assert_called_once_with(configs)


def test_cold_start_refuses_changed_declaration_before_credentials(connectors, monkeypatch):
    monkeypatch.setattr(cu, "_scoped_servers", {})
    connectors.raw["mcp_servers"]["mixpanel"]["url"] = "https://other.invalid"
    connectors.save()
    credentials = Mock(side_effect=forbidden)
    monkeypatch.setattr(cu, "resolved_connector_configs", credentials)
    with pytest.raises(ContextDenied):
        cu.ensure_ready()
    credentials.assert_not_called()


def test_permanent_grant_remains_explicitly_revocable(connectors):
    connectors.value["expires_at"] = None
    connectors.save()
    assert cu.grant()["expires_at"] is None
    connectors.value["enabled"] = False
    connectors.save()
    with pytest.raises(ContextDenied):
        cu.grant()


def test_strict_provider_optional_empty_inputs(connectors):
    args = cu.validate("cookunity_mixpanel_query", {"event": "$all_events", "weeks": 1,
                                                   "math": "total", "breakdown": ""})
    assert args == {"event": "$all_events", "weeks": 1, "math": "total"}
    assert cu.validate("cookunity_mixpanel_events", {"query": None}) == {}
    for value in (None, "", 3):
        with pytest.raises(ContextDenied):
            cu.validate("cookunity_tableau_data", {"viewId": value})
    with pytest.raises(ContextDenied):
        cu.validate("cookunity_mixpanel_query", {"event": "Purchase", "weeks": 1, "breakdown": " "})


def test_new_mcp_sdk_schema_and_error_aliases(connectors):
    schema = {"type": "object", "properties": {}}
    assert cu.tool_schema(SimpleNamespace(input_schema=schema)) is schema
    assert cu.tool_schema(SimpleNamespace(inputSchema=schema)) is schema
    connectors.servers["mixpanel"].session.call_tool.return_value = SimpleNamespace(
        is_error=True, content=[SimpleNamespace(type="text", text="must not reach model")])
    with pytest.raises(ContextDenied):
        cu.execute("cookunity_mixpanel_query", {"event": "$all_events", "weeks": 1}, lambda: cu.policy())
