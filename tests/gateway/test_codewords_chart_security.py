"""Adversarial chart checks through native admission and the installed SDK fence."""
import asyncio
from dataclasses import replace

import httpx
import pytest

from tests.gateway.test_codewords_chart_delivery import (  # noqa: F401
    CHANNEL, FILE, TEAM, URL, chart_delivery, corpus, drain_native, install_bytes,
    native_text, runner, stripe, wire,
)


async def _run(state):
    await state.adapter._handle_slack_message(state.payload, {"team_id": TEAM})
    await drain_native(state.adapter)


def _calls(state, method):
    return [payload for name, payload in state.calls if name == method]


_PAYLOAD_CASES = [
    ("files.getUploadURLExternal", field)
    for field in ("filename", "length", "alt_txt", "snippet_type", "token", "child", "client", "method")
] + [
    ("files.completeUploadExternal", field)
    for field in ("id", "title", "channel_id", "thread_ts", "initial_comment", "channels", "token",
                  "child", "client", "method")
] + [
    ("files.delete", field) for field in ("file", "token", "child", "client", "method")
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,field", _PAYLOAD_CASES)
async def test_every_sdk_stage_is_bound_to_exact_payload_client_and_task(
        chart_delivery, monkeypatch, method, field):
    state = chart_delivery
    install_bytes(state, monkeypatch)
    attribute = method.replace(".", "_")
    original = getattr(state.client, attribute)
    foreign = state.adapter._new_web_client("foreign-synthetic", None)

    async def changed(**kwargs):
        if field == "child":
            return await asyncio.create_task(original(**kwargs))
        if field == "client":
            return await getattr(foreign, attribute)(**kwargs)
        if field == "method":
            return await state.client.api_call("chat.delete", params={"channel": CHANNEL, "ts": "999.1"})
        if field in {"id", "title"}:
            kwargs["files"][0][field] = "F9999999999" if field == "id" else "injected title"
        else:
            kwargs[field] = {
                "filename": "foreign.png", "length": kwargs.get("length", 0) + 1,
                "alt_txt": "injected image text", "snippet_type": "python", "token": "other-token",
                "channel_id": "CFOREIGN", "thread_ts": "999.1", "initial_comment": "injected comment",
                "channels": ["CFOREIGN"], "file": "F9999999999",
            }[field]
        return await original(**kwargs)

    monkeypatch.setattr(state.client, attribute, changed)
    if method == "files.delete":
        async def fail_completion(name, _payload):
            if name == "files.completeUploadExternal":
                return {"ok": False, "error": "internal_error"}
        state.hook = fail_completion
    await _run(state)
    assert not _calls(state, method)
    assert not _calls(state, "chat.delete")
    if method == "files.getUploadURLExternal":
        assert not state.uploads and not _calls(state, "files.completeUploadExternal")
    else:
        assert len(state.uploads) == 1
    if method == "files.completeUploadExternal":
        assert _calls(state, "files.delete") == [{"file": FILE}]
    if method == "files.delete":
        assert "could not be confirmed" in _calls(state, "chat.postMessage")[0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("option", ["headers", "auth", "http_verb", "json", "data", "files"])
@pytest.mark.parametrize("method", ["files.getUploadURLExternal", "files.completeUploadExternal", "files.delete"])
async def test_sdk_side_options_cannot_override_sealed_file_calls(chart_delivery, monkeypatch, option, method):
    state = chart_delivery
    install_bytes(state, monkeypatch)
    original = state.client.api_call

    async def changed(*args, **kwargs):
        if args == (method,):
            kwargs[option] = {
                "headers": {"Authorization": "Bearer foreign-synthetic"},
                "auth": {"client_id": "foreign", "client_secret": "synthetic"},
                "http_verb": "GET", "json": {"file": "F9999999999"},
                "data": {"file": "F9999999999"}, "files": {"file": b"foreign"},
            }[option]
        return await original(*args, **kwargs)

    monkeypatch.setattr(state.client, "api_call", changed)
    if method == "files.delete":
        async def fail_completion(name, _payload):
            if name == "files.completeUploadExternal":
                return {"ok": False, "error": "internal_error"}
        state.hook = fail_completion
    await _run(state)
    assert not _calls(state, method)
    if method == "files.completeUploadExternal":
        assert _calls(state, "files.delete") == [{"file": FILE}]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["data", "url", "client", "child", "generic_sdk"])
async def test_byte_capability_has_no_generic_or_cross_task_escape(chart_delivery, monkeypatch, field):
    state = chart_delivery
    module = install_bytes(state, monkeypatch)
    original = module._upload_bytes
    foreign = state.adapter._new_web_client("foreign-synthetic", None)

    async def changed(upload):
        if field == "child":
            return await asyncio.create_task(original(upload))
        receipt = upload.receipt
        if field == "generic_sdk":
            return await receipt.client._upload_file(
                url=URL, data=receipt.chart.png, logger=receipt.client.logger,
                timeout=1, proxy=None, ssl=None)
        payload = {"url": URL, "data": receipt.chart.png}
        if field == "data":
            payload["data"] += b"injected"
        elif field == "url":
            payload["url"] = "https://files.slack.com/upload/v1/AnotherAllocation"
        state.adapter._check_deletion_scope(
            api_method="files.uploadBytes", payload=payload,
            sdk_client=foreign if field == "client" else receipt.client)

    monkeypatch.setattr(module, "_upload_bytes", changed)
    await _run(state)
    assert not state.uploads
    assert not _calls(state, "files.completeUploadExternal")
    assert _calls(state, "files.delete") == [{"file": FILE}]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["png", "sha256", "comment"])
async def test_chart_receipt_corruption_never_reaches_allocation(chart_delivery, monkeypatch, field):
    state = chart_delivery
    install_bytes(state, monkeypatch)
    original = state.adapter._message_handler

    async def changed(event):
        content = await original(event)
        upload = content._codewords_upload
        receipt = upload.receipt
        if field == "comment":
            upload.receipt = replace(receipt, comment="injected model comment")
        else:
            value = receipt.chart.png + b"injected" if field == "png" else "0" * 64
            upload.receipt = replace(receipt, chart=replace(receipt.chart, **{field: value}))
        return content

    state.adapter._message_handler = changed
    await _run(state)
    assert not _calls(state, "files.getUploadURLExternal")
    assert not state.uploads and not _calls(state, "files.completeUploadExternal")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["token", "bot", "client", "base_url", "headers", "default_params",
                                    "proxy", "ssl", "session", "trust_env_in_session", "retry_handlers"])
async def test_changed_authenticated_transport_cannot_publish_or_clean_with_new_identity(
        chart_delivery, monkeypatch, field):
    state = chart_delivery
    install_bytes(state, monkeypatch)
    original = state.client.files_completeUploadExternal

    async def changed(**kwargs):
        if field == "bot":
            state.adapter._team_bot_user_ids[TEAM] = "UFOREIGN"
        elif field == "client":
            state.adapter._team_clients[TEAM] = state.adapter._new_web_client("foreign-synthetic", None)
        else:
            values = {
                "token": "rotated-synthetic", "base_url": "https://foreign.invalid/api/",
                "headers": {"Authorization": "Bearer foreign-synthetic"},
                "default_params": {"team_id": "TFOREIGN"}, "proxy": "https://foreign.invalid",
                "ssl": object(), "session": object(),
                "trust_env_in_session": not state.client.trust_env_in_session,
                "retry_handlers": [object()],
            }
            setattr(state.client, field, values[field])
        return await original(**kwargs)

    monkeypatch.setattr(state.client, "files_completeUploadExternal", changed)
    await _run(state)
    assert len(state.uploads) == 1
    assert not _calls(state, "files.completeUploadExternal")
    assert not _calls(state, "files.delete")
    assert not _calls(state, "chat.postMessage")


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://files.slack.com/upload/v1/token",
    "https://files.slack.com.evil.invalid/upload/v1/token",
    "https://files.slack.com@evil.invalid/upload/v1/token",
    "https://files.slack.com:444/upload/v1/token",
    "https://files.slack.com/upload/v1/token?target=foreign",
    "https://files.slack.com/upload/v1/token#fragment",
    "https://files.slack.com/upload/v1/../token",
    "https://files.slack.com/upload/v1/%2f%2eevil",
    "https://files.slack.com/api/files.delete",
    None,
])
async def test_unapproved_upload_url_retains_only_owned_cleanup(chart_delivery, monkeypatch, url):
    state = chart_delivery
    install_bytes(state, monkeypatch)

    async def allocation(method, _payload):
        if method == "files.getUploadURLExternal":
            return {"ok": True, "file_id": FILE, "upload_url": url}

    state.hook = allocation
    await _run(state)
    assert not state.uploads and not _calls(state, "files.completeUploadExternal")
    assert _calls(state, "files.delete") == [{"file": FILE}]


@pytest.mark.asyncio
async def test_real_byte_transport_never_follows_redirects(chart_delivery, monkeypatch):
    state = chart_delivery

    async def redirected(_transport, request):
        state.uploads.append(request)
        if str(request.url) == URL:
            return httpx.Response(307, headers={"location": "https://foreign.invalid/upload"})
        return httpx.Response(200, text="foreign transport must never receive PNG bytes")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", redirected)
    await _run(state)
    assert [str(request.url) for request in state.uploads] == [URL]
    assert not _calls(state, "files.completeUploadExternal")
    assert _calls(state, "files.delete") == [{"file": FILE}]


@pytest.mark.asyncio
async def test_receipt_is_single_use_and_cannot_be_replayed_by_child(chart_delivery, monkeypatch):
    state = chart_delivery
    module = install_bytes(state, monkeypatch)
    original = module.send
    denied = []

    async def changed(*args):
        child, _ = await asyncio.create_task(original(*args))
        denied.append(child)
        result = await original(*args)
        replay, _ = await original(*args)
        denied.append(replay)
        return result

    monkeypatch.setattr(module, "send", changed)
    await _run(state)
    assert len(denied) == 2 and all(result.success is False for result in denied)
    assert len(_calls(state, "files.getUploadURLExternal")) == 1
    assert len(state.uploads) == 1 and len(_calls(state, "files.completeUploadExternal")) == 1
    assert not _calls(state, "files.delete")


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["route", "cancel", "repeat_cancel"])
async def test_late_terminal_success_is_settled_and_removed_with_revoked_chart(
        chart_delivery, runner, monkeypatch, revocation):
    state = chart_delivery
    install_bytes(state, monkeypatch)
    entered, release, accepted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reactions = set()

    async def terminal(method, payload):
        if method == "reactions.add":
            if payload["name"] == "white_check_mark":
                entered.set()
                await release.wait()
                accepted.set()
            reactions.add(payload["name"])
        elif method == "reactions.remove":
            reactions.discard(payload["name"])

    state.hook = terminal
    await state.adapter._handle_slack_message(state.payload, {"team_id": TEAM})
    await asyncio.wait_for(entered.wait(), 5)
    owner = next(iter(state.adapter._background_tasks))
    if revocation == "route":
        runner.config.client_context = {"enabled": False}
    else:
        owner.cancel()
        await asyncio.sleep(0)
        if revocation == "repeat_cancel":
            owner.cancel()
            await asyncio.sleep(0)
    release.set()
    await asyncio.gather(owner, return_exceptions=True)
    await asyncio.wait_for(accepted.wait(), 5)
    assert _calls(state, "files.delete") == [{"file": FILE}]
    assert "white_check_mark" not in reactions
    assert not vars(state.adapter).get("_codewords_followups", {})


@pytest.mark.asyncio
async def test_repeated_cancellation_settles_owned_terminal_cleanup(chart_delivery, runner, monkeypatch):
    state = chart_delivery
    install_bytes(state, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def terminal(method, payload):
        if method == "reactions.add" and payload["name"] == "white_check_mark":
            runner.config.client_context = {"enabled": False}
        elif method == "reactions.remove" and payload["name"] == "white_check_mark":
            entered.set()
            await release.wait()

    state.hook = terminal
    await state.adapter._handle_slack_message(state.payload, {"team_id": TEAM})
    await asyncio.wait_for(entered.wait(), 5)
    owner = next(iter(state.adapter._background_tasks))
    owner.cancel()
    await asyncio.sleep(0)
    owner.cancel()
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(owner, return_exceptions=True)
    indicator = state.content._codewords_upload.receipt.event._codewords_indicator
    assert indicator.terminal is None
    assert _calls(state, "files.delete") == [{"file": FILE}]
    assert not vars(state.adapter).get("_codewords_followups", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["name", "timestamp", "channel", "client", "token", "child", "unowned"])
async def test_terminal_cleanup_has_only_exact_owned_reaction_authority(
        chart_delivery, runner, monkeypatch, field):
    import importlib

    state = chart_delivery
    install_bytes(state, monkeypatch)
    ux = importlib.import_module(type(state.adapter).__module__.rsplit(".", 1)[0] + ".codewords_ux")
    original = state.client.reactions_remove
    foreign = state.adapter._new_web_client("foreign-synthetic", None)

    async def revoked(method, payload):
        if method == "reactions.add" and payload["name"] == "white_check_mark":
            runner.config.client_context = {"enabled": False}

    async def changed(**kwargs):
        if kwargs["name"] != "white_check_mark":
            return await original(**kwargs)
        if field == "client":
            return await foreign.reactions_remove(**kwargs)
        if field == "child":
            return await asyncio.create_task(original(**kwargs))
        if field == "token":
            state.client.token = "rotated-synthetic"
        elif field in {"name", "timestamp", "channel"}:
            kwargs[field] = {"name": "heart", "timestamp": "999.1", "channel": "CFOREIGN"}[field]
        return await original(**kwargs)

    if field == "unowned":
        reaction = ux._reaction

        def changed_reaction(indicator, emoji, *, remove=False):
            return reaction(indicator, "heart" if remove and emoji == "white_check_mark" else emoji, remove=remove)

        monkeypatch.setattr(ux, "_reaction", changed_reaction)
    else:
        monkeypatch.setattr(state.client, "reactions_remove", changed)
    state.hook = revoked
    await _run(state)
    assert _calls(state, "files.delete") == [{"file": FILE}]
    assert _calls(state, "reactions.remove") == [{"channel": CHANNEL, "timestamp": state.payload["ts"], "name": "eyes"}]
