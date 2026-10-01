"""Native chart admission, actual read loop and fenced SDK with fake HTTP only."""
import asyncio
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from tests.gateway.test_client_context import corpus, runner, wire  # noqa: F401
from tests.gateway.test_codewords_stripe import (  # noqa: F401
    LOWER, page, stripe, subscription,
)
from tests.gateway.test_slack_channel_isolation import (  # noqa: F401
    CHANNEL, drain_native, native_text,
)

TEAM = "T07MMEX5658"
URL = "https://files.slack.com/upload/v1/ApprovedSyntheticUpload"
FILE = "F0123456789"


@pytest.fixture
def chart_delivery(native_text, stripe, wire, monkeypatch):
    adapter, _, payload = native_text
    payload["text"] = "<@U0B2X1D8R17> show a chart of the Stripe ending comparison"
    _, _, install = stripe
    install(lambda _request: page([
        subscription("sub_Before", created=LOWER-86400, ended_at=LOWER-1,
                     status="canceled", cancellation_details={"reason": "payment_failed"}),
        subscription("sub_After", ended_at=LOWER, status="canceled",
                     cancellation_details={"reason": "cancellation_requested"}),
    ]))

    def model(body):
        assert all(tool["function"]["name"].startswith("codewords_") for tool in body.get("tools", []))
        wire.state.tool_calls = ([{"id": "stripe", "type": "function", "function": {
            "name": "codewords_stripe_read", "arguments": json.dumps({
                "start_date": "2020-01-02", "end_date": "2020-01-02",
                "comparison_start_date": "2020-01-01", "comparison_end_date": "2020-01-01"})}}]
            if not any(m["role"] == "tool" for m in body["messages"]) else None)
        wire.state.text = "invented 999999 ending count MEDIA:/private/evil.png <@UOTHER>"
        wire.state.finish = "tool_calls" if wire.state.tool_calls else "stop"

    wire.state.callback = model
    state = SimpleNamespace(calls=[], uploads=[], hook=None, byte_hook=None, content=None)

    async def transport(*, api_url, req_args, **kwargs):
        method = api_url.rsplit("/", 1)[-1]
        data = req_args.get("params") or req_args.get("json") or {}
        state.calls.append((method, dict(data)))
        if state.hook:
            result = await state.hook(method, data)
            if result is not None:
                return {"status_code": 200, "headers": {}, "data": result}
        if method == "users.info":
            result = {"ok": True, "user": {"id": payload["user"], "is_bot": False}}
        elif method == "conversations.replies":
            result = {"ok": True, "messages": [], "has_more": False}
        elif method == "files.getUploadURLExternal":
            result = {"ok": True, "upload_url": URL, "file_id": FILE}
        elif method == "files.completeUploadExternal":
            result = {"ok": True, "files": [{"id": FILE}]}
        else:
            result = {"ok": True, "ts": "200.1"}
        return {"status_code": 200, "headers": {}, "data": result}

    monkeypatch.setattr("slack_sdk.web.async_base_client._request_with_session", transport)
    client = adapter._new_web_client("synthetic", None)
    adapter._team_clients[TEAM] = adapter._app.client = client
    state.adapter, state.client, state.payload = adapter, client, payload
    original = adapter._message_handler
    prepare = adapter._prepare_codewords_chart

    def record_prepare(*args):
        try:
            return prepare(*args)
        except Exception:
            import traceback
            state.prepare_error = traceback.format_exc()
            raise

    adapter._prepare_codewords_chart = record_prepare

    async def record(event):
        result = await original(event)
        state.content = result
        return result

    adapter._message_handler = record
    return state


def install_bytes(state, monkeypatch):
    module = importlib.import_module(type(state.adapter).__module__.rsplit(".", 1)[0] + ".codewords_upload")

    async def transport(request):
        state.uploads.append(request)
        if state.byte_hook:
            return await state.byte_hook(request)
        return httpx.Response(200, text="OK")

    monkeypatch.setattr(module, "_byte_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(transport), follow_redirects=False, trust_env=False))
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "show a chart of the Stripe ending comparison",
    "design a visual", "design a visual of the Stripe comparison",
])
async def test_native_chart_uses_validated_bytes_and_exact_sdk_stages(chart_delivery, monkeypatch, question):
    state = chart_delivery
    state.payload["text"] = "<@U0B2X1D8R17> " + question
    install_bytes(state, monkeypatch)
    await state.adapter._handle_slack_message(state.payload, {"team_id": TEAM})
    await drain_native(state.adapter)
    assert not getattr(state, "prepare_error", None), getattr(state, "prepare_error", None)
    methods = [method for method, _ in state.calls]
    assert methods == ["users.info", "reactions.add", "conversations.replies",
                       "files.getUploadURLExternal", "files.completeUploadExternal",
                       "reactions.remove", "reactions.add"]
    assert len(state.uploads) == 1
    request = state.uploads[0]
    assert str(request.url) == URL and request.method == "POST"
    assert request.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert "authorization" not in request.headers
    allocation = dict(state.calls)["files.getUploadURLExternal"]
    assert allocation["length"] == len(request.content)
    completion = dict(state.calls)["files.completeUploadExternal"]
    assert completion["channel_id"] == CHANNEL
    assert completion["thread_ts"] == state.payload["thread_ts"]
    from gateway.client_context_codewords_chart import TITLE
    assert json.loads(completion["files"]) == [{"id": FILE, "title": TITLE}]
    assert "999999" not in completion["initial_comment"]
    assert "MEDIA:" not in completion["initial_comment"]
    assert "unverified" in completion["initial_comment"].lower()
    assert state.calls[-1][1]["name"] == "white_check_mark"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ordinary", "missing", "incomplete", "invalid", "renderer"])
async def test_chart_selection_and_honest_text_fallback(chart_delivery, wire, stripe, monkeypatch, mode):
    from gateway import client_context_codewords_chart as chart
    state = chart_delivery
    install_bytes(state, monkeypatch)
    model = wire.state.callback

    def answer(body):
        model(body)
        wire.state.text = "ordinary concise answer"
        if mode == "missing":
            wire.state.tool_calls = None
            wire.state.finish = "stop"

    wire.state.callback = answer
    if mode == "ordinary":
        state.payload["text"] = "<@U0B2X1D8R17> compare the Stripe subscription endings"
    elif mode == "incomplete":
        from gateway import client_context_codewords_reads as reads
        monkeypatch.setattr(reads, "STRIPE_MAX_PAGES", 1)
        stripe[2](lambda _request: page([subscription()], more=True))
    elif mode == "invalid":
        stripe[2](lambda _request: page([subscription(status="foreign")]))
    elif mode == "renderer":
        def broken(_comparison):
            raise ImportError("Pillow unavailable")
        monkeypatch.setattr(chart, "render", broken)
    await state.adapter._handle_slack_message(state.payload, {"team_id": TEAM})
    await drain_native(state.adapter)
    assert not state.uploads and not any(method.startswith("files.") for method, _ in state.calls)
    text = dict(state.calls)["chat.postMessage"]["text"]
    if mode == "ordinary":
        assert text == "ordinary concise answer"
    elif mode == "renderer":
        assert "voluntary: 0 → 1" in text and "could not be delivered" in text
    else:
        assert "complete Stripe comparison" in text and "counts are unavailable" in text
    assert state.calls[-1][1]["name"] == ("white_check_mark" if mode == "ordinary" else "x")


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["allocation", "bytes", "completion", "eyes"])
@pytest.mark.parametrize("change", ["route", "stop", "delete", "credential", "cancel", "repeat_cancel"])
async def test_revocation_boundaries_settle_only_owned_file(
        chart_delivery, runner, monkeypatch, boundary, change):
    state = chart_delivery
    install_bytes(state, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    target = {"allocation": "files.getUploadURLExternal", "completion": "files.completeUploadExternal",
              "eyes": "reactions.remove"}.get(boundary)

    async def sdk(method, _data):
        if method == target:
            entered.set()
            await release.wait()

    async def byte(_request):
        if boundary == "bytes":
            entered.set()
            await release.wait()
        return httpx.Response(200)

    state.hook, state.byte_hook = sdk, byte
    await state.adapter._handle_slack_message(state.payload, {"team_id": TEAM})
    await asyncio.wait_for(entered.wait(), 5)
    owner = next(iter(state.adapter._background_tasks))
    if change == "route":
        runner.config.client_context = {"enabled": False}
    elif change == "credential":
        state.client.token = "rotated"
    elif change == "stop":
        await state.adapter._handle_slack_message({**state.payload, "ts": "101.1", "text": "stop"},
                                                {"team_id": TEAM})
    elif change == "delete":
        await state.adapter._handle_slack_message({"type": "message", "subtype": "message_deleted",
            "channel": CHANNEL, "deleted_ts": state.payload["ts"], "event_ts": "101.1",
            "previous_message": dict(state.payload)}, {"team_id": TEAM})
    else:
        owner.cancel()
        await asyncio.sleep(0)
        if change == "repeat_cancel":
            owner.cancel()
            await asyncio.sleep(0)
    release.set()
    await asyncio.gather(owner, return_exceptions=True)
    methods = [method for method, _ in state.calls]
    assert "chat.postMessage" not in methods
    assert methods.count("files.completeUploadExternal") == int(boundary in {"completion", "eyes"})
    deletions = [payload for method, payload in state.calls if method == "files.delete"]
    assert deletions == ([] if change == "credential" else [{"file": FILE}])
    assert not any(method == "reactions.add" and p.get("name") == "white_check_mark"
                   for method, p in state.calls)
    upload = state.content._codewords_upload
    assert upload.closed and upload.active is None
    assert not vars(state.adapter).get("_codewords_followups", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["allocation", "bytes", "completion"])
@pytest.mark.parametrize("failure", ["rejected", "unknown", "timeout"])
async def test_upload_failure_never_claims_success_and_cleans_known_allocation(
        chart_delivery, monkeypatch, stage, failure):
    state = chart_delivery
    module = install_bytes(state, monkeypatch)
    monkeypatch.setattr(module, "_STAGE_TIMEOUT", 0.05)
    target = {"allocation": "files.getUploadURLExternal", "completion": "files.completeUploadExternal"}.get(stage)

    async def failed():
        if failure == "timeout":
            await asyncio.Event().wait()
        if failure == "unknown":
            raise TimeoutError("unknown acceptance")

    async def sdk(method, _data):
        if method == target:
            await failed()
            return {"ok": False, "error": "synthetic_denied"}

    async def byte(_request):
        if stage == "bytes":
            await failed()
            return httpx.Response(403)
        return httpx.Response(200)

    state.hook, state.byte_hook = sdk, byte
    await state.adapter._handle_slack_message(state.payload, {"team_id": TEAM})
    await drain_native(state.adapter)
    methods = [method for method, _ in state.calls]
    assert methods.count("files.completeUploadExternal") == int(stage == "completion")
    assert [p for m, p in state.calls if m == "files.delete"] == ([] if stage == "allocation" else [{"file": FILE}])
    text = dict(state.calls)["chat.postMessage"]["text"]
    assert "showing validated counts" in text and "voluntary: 0 → 1" in text
    assert state.calls[-1][1]["name"] == "x"
    assert not vars(state.adapter).get("_codewords_followups", {})


@pytest.mark.asyncio
async def test_unknown_completion_and_failed_cleanup_use_honest_fallback(chart_delivery, monkeypatch):
    state = chart_delivery
    install_bytes(state, monkeypatch)

    async def sdk(method, _data):
        if method == "files.completeUploadExternal":
            return {"ok": True, "files": [{"id": "FOTHER12345"}]}
        if method == "files.delete":
            return {"ok": False, "error": "synthetic_cleanup_failed"}

    state.hook = sdk
    await state.adapter._handle_slack_message(state.payload, {"team_id": TEAM})
    await drain_native(state.adapter)
    assert [p for m, p in state.calls if m == "files.delete"] == [{"file": FILE}]
    text = dict(state.calls)["chat.postMessage"]["text"]
    assert "delivery could not be confirmed" in text
    assert "not delivered" not in text and "FOTHER" not in text


@pytest.mark.asyncio
async def test_chart_followup_is_fresh_and_replay_or_other_requester_cannot_upload(chart_delivery, monkeypatch):
    state = chart_delivery
    install_bytes(state, monkeypatch)
    await state.adapter._handle_slack_message(state.payload, {"team_id": TEAM})
    await drain_native(state.adapter)
    first = state.content._codewords_upload
    first_bytes = len(state.uploads)
    await state.adapter._handle_slack_message(state.payload, {"team_id": TEAM})
    await drain_native(state.adapter)
    await state.adapter._handle_slack_message({**state.payload, "ts": "101.1", "user": "UFOREIGN",
                                             "text": "show a chart"}, {"team_id": TEAM})
    await drain_native(state.adapter)
    assert len(state.uploads) == first_bytes
    await state.adapter._handle_slack_message({**state.payload, "ts": "102.1", "text": "show that as a chart"},
                                            {"team_id": TEAM})
    await drain_native(state.adapter)
    assert len(state.uploads) == first_bytes + 1
    assert state.content._codewords_upload is not first
    assert first.closed and state.content._codewords_upload.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "Provide an explanation of this Stripe comparison chart.",
    "Give me the text of this chart.",
    "Show a chart, but do not create or send it.",
    "Show a PostHog users comparison chart.",
    "don't design a visual of the Stripe comparison",
    "Design a visual of the Stripe comparison. Do not design it.",
    'The user said "design a visual of the Stripe comparison". Explain the request.',
    "why don't you create a visual of the Stripe comparison?",
    "don't create a visual of the Stripe comparison",
])
async def test_actual_stripe_read_cannot_grant_unrequested_chart_authority(
        chart_delivery, wire, monkeypatch, question):
    state = chart_delivery
    install_bytes(state, monkeypatch)
    state.payload["text"] = "<@U0B2X1D8R17> " + question
    model = wire.state.callback

    def answer(body):
        model(body)
        wire.state.text = "a concise text answer"

    wire.state.callback = answer
    await state.adapter._handle_slack_message(state.payload, {"team_id": TEAM})
    await drain_native(state.adapter)
    assert len(wire.captured) == 2
    assert not state.uploads and not any(m.startswith("files.") for m, _ in state.calls)
    assert dict(state.calls)["chat.postMessage"]["text"] == "a concise text answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", ["bot", "workspace", "channel", "unverified"])
async def test_denied_native_chart_input_has_no_provider_or_write_authority(
        chart_delivery, wire, monkeypatch, denied):
    state = chart_delivery
    install_bytes(state, monkeypatch)
    body = {"team_id": TEAM}
    state.payload["text"] = "<@U0B2X1D8R17> design a visual of the Stripe comparison"
    if denied == "bot":
        state.payload["bot_id"] = "BFOREIGN"
    elif denied == "workspace":
        body["team_id"] = "TFOREIGN"
    elif denied == "channel":
        state.payload["channel"] = "CFOREIGN"
    else:
        async def unverified(method, _payload):
            if method == "users.info":
                return {"ok": False, "error": "synthetic_unverified"}
        state.hook = unverified
    await state.adapter._handle_slack_message(state.payload, body)
    await drain_native(state.adapter)
    assert not wire.captured and not state.uploads
    assert all(method in {"users.info"} for method, _ in state.calls)
