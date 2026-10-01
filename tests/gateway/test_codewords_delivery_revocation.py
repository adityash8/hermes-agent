"""Credential revocation survives the real tool loop through final answer release."""

import json
from dataclasses import replace

import httpx
import pytest

from gateway import client_context as cc
from gateway import client_context_codewords as route
from gateway import client_context_codewords_reads as reads
from gateway.client_context_policy import ContextDenied
from tests.gateway.test_client_context import corpus, runner, wire  # noqa: F401
from tests.gateway.test_codewords_meta import wire as meta_wire  # noqa: F401
from tests.gateway.test_slack_channel_isolation import sealed


@pytest.fixture
def posthog(monkeypatch, tmp_path):
    path = tmp_path / "posthog.env"
    path.write_text("POSTHOG_PERSONAL_API_KEY=SyntheticCredential\n")
    original = next(binding for binding in reads.bindings() if binding.name == "codewords_posthog_read")
    binding = replace(original, credentials=(("env_file", str(path)),
                                           ("access_token_key", "POSTHOG_PERSONAL_API_KEY")))
    monkeypatch.setattr(reads, "_BINDINGS", tuple(
        binding if item.name == binding.name else item for item in reads.bindings()))
    requests = []

    def send(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer SyntheticCredential"
        assert request.url.host == "eu.posthog.com"
        if request.method == "GET":
            assert request.url.path == "/api/projects/23659/"
            payload = {"id": 23659, "organization": "018fbef7-f7af-0000-41ce-60cd82be5848",
                       "timezone": "UTC", "is_pending_deletion": False}
        else:
            assert request.method == "POST" and request.url.path == "/api/projects/23659/query/"
            query = json.loads(request.content)["query"]
            assert query["kind"] == "TrendsQuery"
            assert query["series"] == [{"kind": "EventsNode", "event": None, "math": "total"}]
            payload = {"results": [{"days": ["2020-01-01"], "data": [7]}], "hasMore": False}
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(reads.transport, "_http_client", lambda: httpx.Client(
        transport=httpx.MockTransport(send), trust_env=False, follow_redirects=False))
    return path, requests


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("change", [None, "delete", "rotate"])
@pytest.mark.parametrize("moment", ["completion", "delivery"])
async def test_credentials_rechecked_through_final_release(runner, wire, posthog, admin, change, moment):
    path, requests = posthog
    trigger = sealed(runner)
    runner._is_user_authorized_for_source.return_value = admin
    trigger.source.user_id = "U07MJJMMK9T" if admin else "U092L0QKYMB"
    trigger.source._codewords_native_identity = route.native_identity(trigger.source)
    trigger.source._codewords_policy = route.route(runner.config, trigger.source)
    trigger._codewords_request = route.seal_event(trigger)

    def revoke():
        if change == "delete":
            path.unlink()
        elif change == "rotate":
            path.write_text("POSTHOG_PERSONAL_API_KEY=ReplacementCredential\n")

    def next_response(body):
        if len(wire.captured) == 1:
            wire.state.tool_calls = [{"id": "fixed-read", "type": "function", "function": {
                "name": "codewords_posthog_read", "arguments": json.dumps({
                    "operation": "event_trends", "start_date": "2020-01-01", "end_date": "2020-01-01"})}}]
            wire.state.text = None
            wire.state.finish = "tool_calls"
        else:
            assert len(wire.captured) == 2
            receipt = json.loads(next(item["content"] for item in body["messages"] if item["role"] == "tool"))
            assert receipt["status"] == "observation"
            assert receipt["result"]["rows"] == [{"date": "2020-01-01", "events": 7}]
            wire.state.tool_calls = []
            wire.state.text = "There were 7 events."
            wire.state.finish = "stop"
            if moment == "completion":
                revoke()

    wire.state.callback = next_response
    reply = await runner._handle_message(trigger)
    assert len(wire.captured) == 2 and len(requests) == 3
    assert "SyntheticCredential" not in json.dumps(wire.captured)
    runner._is_user_authorized_for_source.assert_not_called()
    runner.session_store.assert_not_called()
    if moment == "completion" and change:
        assert reply is None
        return
    assert isinstance(reply, cc.ScopedReply) and reply == "There were 7 events."
    reply._codewords_validate()
    if moment == "delivery":
        revoke()
    if change:
        with pytest.raises(ContextDenied):
            reply._codewords_validate()
    else:
        # Repeated delivery checks prove the retained callbacks do not recurse.
        reply._codewords_validate()
        reply._codewords_validate()


@pytest.mark.parametrize("change", ["delete", "rotate"])
def test_meta_connection_receipt_rechecks_its_own_store(meta_wire, change):
    output = reads.execute("codewords_meta_ads_read",
        '{"start_date":"2026-09-10","end_date":"2026-09-10"}', lambda: None)
    assert isinstance(output, reads.ReadReceipt) and json.loads(output)["status"] == "observation"
    output.validate_credentials()
    if change == "delete":
        meta_wire.env_path.unlink()
    else:
        meta_wire.env_path.write_text("PIPEBOARD_READ_TOKEN=ReplacementCredential\n")
    with pytest.raises(ContextDenied):
        output.validate_credentials()
