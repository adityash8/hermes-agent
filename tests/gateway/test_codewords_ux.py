"""Native processing UI and exact Slack presentation, with no external transport."""
import asyncio
import threading

import pytest

from tests.gateway.test_client_context import corpus, runner, wire  # noqa: F401
from tests.gateway.test_slack_channel_isolation import (  # noqa: F401
    CHANNEL, drain_native, native_text,
)

TEAM = "T07MMEX5658"


@pytest.fixture
def ux(native_text):
    adapter, client, payload = native_text
    client.reactions_add.return_value = {"ok": True}
    client.reactions_remove.return_value = {"ok": True}
    return adapter, client, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_eyes_cover_native_work_and_actual_outcome(ux, wire, failure):
    adapter, client, payload = ux
    wire.state.error = failure
    handler = adapter._message_handler

    async def work(event):
        client.reactions_add.assert_awaited_once_with(
            channel=CHANNEL, timestamp=payload["ts"], name="eyes")
        return await handler(event)

    adapter._message_handler = work
    await adapter._handle_slack_message(payload, {"team_id": TEAM})
    await drain_native(adapter)
    client.reactions_remove.assert_awaited_once_with(
        channel=CHANNEL, timestamp=payload["ts"], name="eyes")
    assert [c.kwargs["name"] for c in client.reactions_add.await_args_list] == [
        "eyes", "x" if failure else "white_check_mark"]
    assert client.chat_postMessage.await_count == int(not failure)


@pytest.mark.asyncio
async def test_model_timeout_clears_eyes_without_claiming_success(ux, wire, monkeypatch):
    from gateway import client_context_codewords as codewords

    adapter, client, payload = ux
    release = threading.Event()
    wire.state.callback = lambda _body: release.wait(10)
    monkeypatch.setattr(codewords, "TURN_TIMEOUT", 2.0)
    try:
        await adapter._handle_slack_message(payload, {"team_id": TEAM})
        await drain_native(adapter)
    finally:
        release.set()
    client.reactions_remove.assert_awaited_once_with(
        channel=CHANNEL, timestamp=payload["ts"], name="eyes")
    assert [c.kwargs["name"] for c in client.reactions_add.await_args_list] == ["eyes", "x"]
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("in_flight", [False, True])
async def test_cancel_after_revoke_can_only_clean_own_eyes(ux, runner, in_flight):
    adapter, client, payload = ux
    entered, release = asyncio.Event(), asyncio.Event()

    async def add(**kwargs):
        entered.set()
        await release.wait()
        return {"ok": True}

    async def work(event):
        entered.set()
        await release.wait()

    if in_flight:
        client.reactions_add.side_effect = add
    else:
        adapter._message_handler = work
    await adapter._handle_slack_message(payload, {"team_id": TEAM})
    await asyncio.wait_for(entered.wait(), 5)
    runner.config.client_context = {"enabled": False}
    task = next(iter(adapter._background_tasks))
    task.cancel()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    client.reactions_remove.assert_awaited_once_with(
        channel=CHANNEL, timestamp=payload["ts"], name="eyes")
    assert [c.kwargs["name"] for c in client.reactions_add.await_args_list] == ["eyes"]
    client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_present_eyes_are_not_owned_for_cleanup(ux):
    adapter, client, payload = ux
    client.reactions_add.return_value = {"ok": False, "error": "already_reacted"}
    await adapter._handle_slack_message(payload, {"team_id": TEAM})
    await drain_native(adapter)
    client.reactions_remove.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["allowed", "ignored", "edit"])
async def test_current_channel_or_edited_evidence_revokes_work_and_terminal_add(ux, wire, change):
    adapter, client, payload = ux
    handler = adapter._message_handler

    async def revoked(event):
        if change == "allowed":
            adapter.config.extra["allowed_channels"] = ["COTHER"]
        elif change == "ignored":
            adapter.config.extra["ignored_channels"] = [CHANNEL]
        else:
            # The native edit route invalidates evidence after the provider has
            # received it, while the current human request still has its seal.
            def edited(_body):
                adapter._codewords_history_revision += 1
            adapter._codewords_history_revision = 0
            wire.state.callback = edited
        return await handler(event)

    adapter._message_handler = revoked
    await adapter._handle_slack_message(payload, {"team_id": TEAM})
    await drain_native(adapter)
    assert [call.kwargs["name"] for call in client.reactions_add.await_args_list] == ["eyes"]
    client.reactions_remove.assert_awaited_once_with(
        channel=CHANNEL, timestamp=payload["ts"], name="eyes")
    client.chat_postMessage.assert_not_awaited()
    if change != "edit":
        client.conversations_replies.assert_not_awaited()
        assert not wire.captured


@pytest.mark.asyncio
async def test_real_sdk_keeps_accepted_add_receipt_across_cancel_and_revoke(
        ux, runner, wire, monkeypatch):
    adapter, _, payload = ux
    calls, entered, release = [], asyncio.Event(), asyncio.Event()

    async def transport(*, api_url, **kwargs):
        method = api_url.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "users.info":
            data = {"ok": True, "user": {"id": payload["user"], "is_bot": False}}
        else:
            if method == "reactions.add":
                entered.set()
                await release.wait()
            data = {"ok": True}
        return {"status_code": 200, "headers": {}, "data": data}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    client = adapter._new_web_client("synthetic", None)
    adapter._team_clients[TEAM] = adapter._app.client = client
    await adapter._handle_slack_message(payload, {"team_id": TEAM})
    await asyncio.wait_for(entered.wait(), 5)
    runner.config.client_context = {"enabled": False}
    task = next(iter(adapter._background_tasks))
    task.cancel()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert calls == ["users.info", "reactions.add", "reactions.remove"]
    assert not wire.captured


@pytest.mark.asyncio
async def test_table_reply_becomes_bullets_and_safe_native_bold(ux, wire):
    adapter, client, payload = ux
    wire.state.text = ("**endings fell.**\n\n| kind | before | after |\n|---|---:|---:|\n"
                       "| voluntary | 10 | 6 |\n| involuntary | 5 | 2 |\n\n"
                       "source: <@UOTHER> <https://invalid.test|do not link> @here")
    await adapter._handle_slack_message(payload, {"team_id": TEAM})
    await drain_native(adapter)
    sent = client.chat_postMessage.call_args.kwargs
    assert sent["text"].startswith("*endings fell.*")
    assert "• *voluntary*: before: 10; after: 6" in sent["text"]
    assert "• *involuntary*: before: 5; after: 2" in sent["text"]
    assert "| kind |" not in sent["text"] and "|---" not in sent["text"]
    assert "<@" not in sent["text"] and "<https:" not in sent["text"]
    assert "@here" not in sent["text"]
    assert sent == {"channel": CHANNEL, "thread_ts": payload["thread_ts"],
                    "text": sent["text"], "mrkdwn": True, "parse": "none", "link_names": False,
                    "unfurl_links": False, "unfurl_media": False}


@pytest.mark.asyncio
async def test_two_column_table_without_edge_pipes_is_also_slack_bullets(ux, wire):
    adapter, client, payload = ux
    wire.state.text = "endings fell.\n\nkind | value\n--- | ---\nvoluntary | 10\ninvoluntary | 5"
    await adapter._handle_slack_message(payload, {"team_id": TEAM})
    await drain_native(adapter)
    sent = client.chat_postMessage.call_args.kwargs["text"]
    assert "|" not in sent
    assert "• *voluntary*: value: 10" in sent and "• *involuntary*: value: 5" in sent


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", [False, True])
@pytest.mark.parametrize("tamper", [None, "name", "timestamp", "channel", "method", "child", "client", "token"])
async def test_real_sdk_ui_capability_cannot_expand_even_for_revoked_cleanup(
        ux, runner, wire, monkeypatch, cleanup, tamper):
    adapter, _, payload = ux
    calls, entered, release = [], asyncio.Event(), asyncio.Event()

    async def transport(*, api_url, **kwargs):
        method = api_url.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "users.info":
            data = {"ok": True, "user": {"id": payload["user"], "is_bot": False}}
        elif method == "conversations.replies":
            data = {"ok": True, "messages": [], "has_more": False}
        else:
            data = {"ok": True, "ts": "200.1"}
        return {"status_code": 200, "headers": {}, "data": data}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    client = adapter._new_web_client("synthetic", None)
    adapter._team_clients[TEAM] = adapter._app.client = client
    foreign = adapter._new_web_client("foreign-synthetic", None)
    method = "reactions_remove" if cleanup else "reactions_add"
    original = getattr(client, method)

    async def changed(**kwargs):
        if tamper == "child":
            return await asyncio.create_task(original(**kwargs))
        if tamper == "client":
            return await getattr(foreign, method)(**kwargs)
        if tamper == "method":
            return await client.api_call("chat.postMessage", json=kwargs)
        if tamper == "token":
            client.token = "rotated-synthetic"
        elif tamper:
            kwargs[tamper] = {"name": "heart", "timestamp": "999.1", "channel": "CFOREIGN"}[tamper]
        return await original(**kwargs)

    monkeypatch.setattr(client, method, changed)
    if cleanup:
        async def blocked(event):
            entered.set()
            await release.wait()
        adapter._message_handler = blocked
    await adapter._handle_slack_message(payload, {"team_id": TEAM})
    if cleanup:
        await asyncio.wait_for(entered.wait(), 5)
        runner.config.client_context = {"enabled": False}
        task = next(iter(adapter._background_tasks))
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert calls.count("reactions.add") == 1
        assert calls.count("reactions.remove") == int(tamper is None)
        assert "chat.postMessage" not in calls
    else:
        await drain_native(adapter)
        assert calls.count("reactions.add") == (2 if tamper is None else 0)
        assert calls.count("reactions.remove") == int(tamper is None)
    assert not any(method.startswith("files.") for method in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [None, "parse", "mrkdwn", "link_names", "unfurl_links",
                                      "unfurl_media", "reply_broadcast", "child", "token"])
async def test_real_sdk_post_payload_is_exactly_sealed(ux, wire, monkeypatch, tamper):
    adapter, _, payload = ux
    sent = []

    async def transport(*, api_url, **kwargs):
        method = api_url.rsplit("/", 1)[-1]
        if method == "users.info":
            data = {"ok": True, "user": {"id": payload["user"], "is_bot": False}}
        elif method == "conversations.replies":
            data = {"ok": True, "messages": [], "has_more": False}
        else:
            if method == "chat.postMessage":
                sent.append(kwargs)
            data = {"ok": True, "ts": "200.1"}
        return {"status_code": 200, "headers": {}, "data": data}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    client = adapter._new_web_client("synthetic", None)
    adapter._team_clients[TEAM] = adapter._app.client = client
    post = client.chat_postMessage

    async def changed(**kwargs):
        if tamper == "child":
            return await asyncio.create_task(post(**kwargs))
        if tamper == "token":
            client.token = "rotated-synthetic"
        elif tamper:
            kwargs[tamper] = "full" if tamper == "parse" else not kwargs.get(tamper, False)
        return await post(**kwargs)

    monkeypatch.setattr(client, "chat_postMessage", changed)
    await adapter._handle_slack_message(payload, {"team_id": TEAM})
    await drain_native(adapter)
    assert len(wire.captured) == 1
    assert len(sent) == int(tamper is None)
