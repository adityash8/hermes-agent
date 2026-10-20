"""Native owner exemption and same-thread private-session isolation."""
import copy
from unittest.mock import AsyncMock

import pytest

from gateway import client_context as cc
from gateway import client_context_codewords as route
from gateway import client_context_codewords_owner as owner
from gateway.session import build_session_key, is_shared_multi_user_session
from plugins.platforms.slack.client_context import admit_event, interaction_is_legacy
from tests.gateway.test_client_context import corpus, runner, wire, event  # noqa: F401
from tests.gateway.test_slack_channel_isolation import native_text, drain_native  # noqa: F401

OWNER = "U07MJJMMK9T"
TEAM = route.WORKSPACE


def opt_in(runner):
    setting = runner.config.client_context["codewords_read_channels"][route.CHANNEL]
    setting.update(operator_user_id=OWNER, read_tools=[])
    return setting


def owner_payload(client, payload):
    payload["user"] = OWNER
    client.users_info.return_value["user"].update(id=OWNER, team_id=TEAM)


@pytest.mark.asyncio
async def test_owner_ordinary_ingress_and_turn_teammate_bounded(native_text, runner):
    adapter, client, payload = native_text
    opt_in(runner)
    owner_payload(client, payload)
    mode, source = await admit_event(adapter, payload, {"team_id": TEAM})
    assert mode == "legacy" and owner.authorized(runner.config, source)
    trigger = event(chat_id=route.CHANNEL, scope_id=TEAM, user_id=OWNER)
    trigger.source = source
    trigger.message_id = payload["ts"]
    assert await cc.handle_ingress(runner, trigger) == (False, None)
    assert await cc.handle_turn(runner, trigger, source, build_session_key(source), 1) == (False, None)
    payload["user"] = "U092L0QKYMB"
    client.users_info.return_value["user"].update(id=payload["user"], team_id="Tforeign")
    mode, teammate = await admit_event(adapter, payload, {"team_id": TEAM})
    assert mode == "scoped" and teammate._codewords_policy.resources == ()
    assert not owner.authorized(runner.config, teammate)
    assert build_session_key(source, False, False) != build_session_key(teammate, False, False)
    assert not is_shared_multi_user_session(source, group_sessions_per_user=False)
    old = f"agent:main:slack:group:{TEAM}:{route.CHANNEL}:{payload['thread_ts']}"
    assert build_session_key(source, False, False) != old


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["default", "foreign", "missing", "bot", "member-bot", "deleted", "wrong-member", "member-team", "nested", "bad-policy", "claimed-admin"])
async def test_owner_authority_is_only_native_identity(native_text, runner, variant):
    adapter, client, payload = native_text
    setting = opt_in(runner)
    owner_payload(client, payload)
    body = {"team_id": TEAM}
    if variant == "default":
        setting.pop("operator_user_id")
    elif variant == "foreign":
        body["team_id"] = "Tforeign"
    elif variant == "missing":
        body = {}
    elif variant == "bot":
        payload["bot_id"] = "Bforeign"
    elif variant == "member-bot":
        client.users_info.return_value["user"]["is_bot"] = True
    elif variant == "deleted":
        client.users_info.return_value["user"]["deleted"] = True
    elif variant == "wrong-member":
        client.users_info.return_value["user"]["id"] = "Uother"
    elif variant == "member-team":
        client.users_info.return_value["user"]["team_id"] = "Tforeign"
    elif variant == "nested":
        payload.pop("user")
        payload["message"] = {"user": OWNER}
    elif variant == "bad-policy":
        setting["operator_user_id"] = "Uother"
    elif variant == "claimed-admin":
        payload["user"] = "Uother"
        client.users_info.return_value["user"].update(id="Uother", is_admin=True)
    mode, source = await admit_event(adapter, payload, body)
    assert mode == ("scoped" if variant in {"default", "claimed-admin"} else "deny")
    assert source is None or not owner.authorized(runner.config, source)


@pytest.mark.asyncio
async def test_revocation_and_identity_changes_never_restore_tools(native_text, runner):
    adapter, client, payload = native_text
    setting = opt_in(runner)
    owner_payload(client, payload)
    _, source = await admit_event(adapter, payload, {"team_id": TEAM})
    forged = copy.copy(source)
    forged.user_id = "Uother"
    assert await cc.admission(runner, forged) == "deny"
    forged = copy.copy(source)
    forged.delivered_via_upstream_relay = True
    assert await cc.admission(runner, forged) == "deny"
    setting.pop("operator_user_id")
    assert await cc.admission(runner, source) == "deny"
    setting["operator_user_id"] = OWNER
    adapter._team_bot_user_ids[TEAM] = "UBOT_REPLACED"
    assert await cc.admission(runner, source) == "deny"


@pytest.mark.asyncio
async def test_owner_interactions_freshly_verify_actor(native_text, runner):
    adapter, client, payload = native_text
    opt_in(runner)
    owner_payload(client, payload)
    body = {"type": "block_actions", "team": {"id": TEAM},
            "channel": {"id": route.CHANNEL}, "user": {"id": OWNER},
            "message": {"user": "UBOT", "thread_ts": "100.0", "ts": "200.0"}}
    admitted = []
    assert await interaction_is_legacy(adapter, body, admitted_sources=admitted)
    assert adapter._is_interactive_user_authorized(OWNER, source=admitted[0])
    assert client.users_info.await_count == 1
    body["user"]["id"] = "Uother"
    client.users_info.return_value["user"].update(id="Uother", is_admin=True)
    assert not await interaction_is_legacy(adapter, body)
    body["user"]["id"] = OWNER
    client.users_info.return_value["user"]["id"] = OWNER
    opt_in(runner).pop("operator_user_id")
    assert not await interaction_is_legacy(adapter, body)


@pytest.mark.asyncio
async def test_actual_adapter_owner_reaches_normal_handler(native_text, runner, monkeypatch):
    adapter, client, payload = native_text
    opt_in(runner)
    owner_payload(client, payload)
    adapter._hydrate_thread_context = AsyncMock(return_value=("", [], []))
    adapter._build_message_event = AsyncMock()
    trigger = event(chat_id=route.CHANNEL, scope_id=TEAM, user_id=OWNER)
    trigger.source.chat_type = "group"
    trigger.source.thread_id = payload["thread_ts"]
    trigger.message_id = payload["ts"]
    adapter._build_message_event.return_value = trigger
    adapter.handle_message = AsyncMock()
    await adapter._handle_slack_message(payload, {"team_id": TEAM})
    adapter.handle_message.assert_awaited_once()
    assert owner.event_authorized(runner.config, adapter.handle_message.call_args.args[0])


@pytest.mark.asyncio
async def test_owner_tool_and_delivery_guard_revokes(native_text, runner):
    import asyncio
    from plugins.platforms.slack.participation import _Participation, _bind_participation, _reset_participation
    from tools.interrupt import tool_execution_allowed
    adapter, client, payload = native_text
    setting = opt_in(runner)
    owner_payload(client, payload)
    _, source = await admit_event(adapter, payload, {"team_id": TEAM})
    key = (TEAM, route.CHANNEL, source.thread_id)
    grant = _Participation(adapter, key, OWNER, build_session_key(source),
                           adapter._deletion_generation(key), source=source, task=asyncio.current_task())
    tokens = _bind_participation(grant)
    try:
        assert tool_execution_allowed()
        assert not adapter._codewords_surface(key)
        adapter._check_deletion_scope(api_method="chat.postMessage", payload={
            "channel": route.CHANNEL, "thread_ts": source.thread_id, "text": "Codewords reply"}, sdk_client=client)
        setting.pop("operator_user_id")
        assert not tool_execution_allowed()
        assert not adapter._participation_valid(grant)
    finally:
        _reset_participation(tokens)


@pytest.mark.asyncio
async def test_owner_pending_prompt_requires_recorded_requester(native_text, runner):
    from plugins.platforms.slack.participation import _Participation
    adapter, client, payload = native_text
    opt_in(runner)
    owner_payload(client, payload)
    _, source = await admit_event(adapter, payload, {"team_id": TEAM})
    key = (TEAM, route.CHANNEL, source.thread_id)
    grant = _Participation(adapter, key, OWNER, build_session_key(source),
                           adapter._deletion_generation(key), source=source)
    adapter._slack_prompt_participation = {(TEAM, route.CHANNEL, "200.0"): (grant, "exact-action", "clarify", None)}
    for kind in ("approval", "clarify", "slash-confirm", "model-picker"):
        assert adapter._interaction_participation(TEAM, route.CHANNEL, "200.0", "Uother", kind, grant.session_key) is None
    assert adapter._interaction_participation(TEAM, route.CHANNEL, "200.0", OWNER, "approval", "forged-session") is None
    assert adapter._interaction_participation(TEAM, route.CHANNEL, "200.0", OWNER, "model-picker", "stage") is not None
    opt_in(runner).pop("operator_user_id")
    assert adapter._interaction_participation(TEAM, route.CHANNEL, "200.0", OWNER, "model-picker", "stage") is None


@pytest.mark.asyncio
async def test_owner_event_cannot_authorize_another_turn_source(native_text, runner, wire):
    adapter, client, payload = native_text
    opt_in(runner)
    owner_payload(client, payload)
    _, source = await admit_event(adapter, payload, {"team_id": TEAM})
    trigger = event(chat_id=route.CHANNEL, scope_id=TEAM, user_id=OWNER)
    trigger.source = source
    trigger.message_id = payload["ts"]
    different = copy.copy(source)
    different.user_id = "Uother"
    handled, _ = await cc.handle_turn(runner, trigger, different, build_session_key(different), 1)
    assert handled is True
    assert not wire.captured

