"""Fixed-grant OAuth refresh, bounded transport, and revocation contracts."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from gateway import client_context_codewords_oauth as oauth
from gateway.client_context_policy import ContextDenied


def credential(expired=True):
    return {"token_uri": oauth.TOKEN_URL, "token": "old-token-canary",
            "expiry": (datetime.now(timezone.utc) + timedelta(days=-1 if expired else 1)).isoformat(),
            "client_id": "client-id", "client_secret": "client+secret&canary=1",
            "refresh_token": "refresh+token&canary=2", "scopes": ["must-not-send"],
            "arbitrary": "must-not-send"}


def client(monkeypatch, handler):
    calls = []

    def send(request):
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(oauth.transport, "_http_client", lambda: httpx.Client(
        transport=httpx.MockTransport(send), follow_redirects=False, trust_env=False))
    return calls


def success():
    return httpx.Response(200, json={"access_token": "new-token-canary", "expires_in": 3600,
                                     "token_type": "Bearer", "refresh_token": "ignored-rotation"})


def test_refresh_is_fixed_form_and_ephemeral(monkeypatch, caplog):
    raw = credential()
    original = deepcopy(raw)
    calls = client(monkeypatch, lambda request: success())
    checks = []
    assert oauth.access_token(raw, lambda: checks.append(True)) == "new-token-canary"
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "POST" and str(request.url) == "https://oauth2.googleapis.com/token"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.headers["accept-encoding"] == "identity"
    assert "authorization" not in request.headers
    assert all(value == 5.0 for value in request.extensions["timeout"].values())
    assert parse_qs(request.content.decode(), strict_parsing=True) == {
        "grant_type": ["refresh_token"], "client_id": [raw["client_id"]],
        "client_secret": [raw["client_secret"]], "refresh_token": [raw["refresh_token"]],
    }
    assert raw == original and len(checks) >= 5
    # A second invocation neither reuses a global cache nor writes into the input.
    assert oauth.access_token(raw, lambda: None) == "new-token-canary"
    assert len(calls) == 2 and raw == original
    assert oauth.access_token(credential(expired=False), lambda: None) == "old-token-canary"
    assert len(calls) == 2
    assert "canary" not in caplog.text


@pytest.mark.parametrize("patch", [
    {"token_uri": "https://attacker.test/token"}, {"token_uri": oauth.TOKEN_URL + "?scope=evil"},
    {"token_uri": "http://oauth2.googleapis.com/token"}, {"expiry": "bad-canary"},
    {"expiry": "2000-01-01T00:00:00"}, {"expiry": None},
    {"client_id": ""}, {"client_secret": "secret\r\ncanary"}, {"refresh_token": None},
])
def test_invalid_credentials_never_reach_transport(monkeypatch, patch):
    calls = client(monkeypatch, lambda request: pytest.fail("unexpected HTTP"))
    with pytest.raises(ContextDenied) as exc:
        oauth.access_token(credential() | patch, lambda: None)
    assert not calls and "canary" not in str(exc.value)


@pytest.mark.parametrize("status,payload,expected", [
    (400, {"error": "invalid_grant", "error_description": "revoked secret-canary"}, "invalid_grant"),
    (401, {"error": "invalid_client", "error_description": "auth secret-canary"}, "invalid_client"),
    (500, {"error": "server_error", "secret": "secret-canary"}, "server_error"),
    (403, {"error": "secret-canary", "error_description": "secret-canary"}, "http_error"),
    (400, {"error": {"message": "secret-canary"}}, "http_error"),
    (200, {"access_token": "secret-canary", "expires_in": 0, "token_type": "Bearer"}, "invalid_response"),
    (200, {"access_token": "secret-canary", "expires_in": True, "token_type": "Bearer"}, "invalid_response"),
    (200, {"access_token": "secret-canary", "expires_in": 3600, "token_type": "Basic"}, "invalid_response"),
])
def test_failed_refresh_reports_only_allowlisted_code_and_status(monkeypatch, caplog, status, payload, expected):
    client(monkeypatch, lambda request: httpx.Response(status, json=payload))
    with pytest.raises(oauth.OAuthRefreshError) as exc:
        oauth.access_token(credential(), lambda: None)
    assert exc.value.code == expected and exc.value.status == status
    assert "canary" not in str(exc.value) + caplog.text


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "https://attacker.test"}),
    httpx.Response(200, content=b'{"access_token":"a","access_token":"b"}', headers={"content-type": "application/json"}),
    httpx.Response(200, content=b'{"expires_in":NaN}', headers={"content-type": "application/json"}),
    httpx.Response(200, content=b"secret-canary", headers={"content-type": "text/html"}),
    httpx.Response(200, content=b"{}", headers={"content-type": "application/json", "content-length": "65537"}),
    httpx.Response(200, content=b" " * 65537, headers={"content-type": "application/json"}),
    httpx.Response(200, stream=httpx.ByteStream(b"{}"), headers={"content-type": "application/json", "content-encoding": "br"}),
])
def test_refresh_rejects_redirects_and_unbounded_or_ambiguous_payloads(monkeypatch, response):
    calls = client(monkeypatch, lambda request: response)
    with pytest.raises(oauth.OAuthRefreshError):
        oauth.access_token(credential(), lambda: None)
    assert len(calls) == 1


def test_chunked_refresh_cannot_evade_size_cap(monkeypatch):
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b" " * 32768
            yield b" " * 32769

    client(monkeypatch, lambda request: httpx.Response(
        200, stream=Stream(), headers={"content-type": "application/json"}))
    with pytest.raises(oauth.OAuthRefreshError, match="invalid_response"):
        oauth.access_token(credential(), lambda: None)


@pytest.mark.parametrize("stage", ["before", "headers", "chunk", "after"])
def test_revocation_survives_all_transport_boundaries(monkeypatch, stage):
    state = {"revoked": stage == "before"}

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            if stage == "chunk":
                state["revoked"] = True
            yield b'{"access_token":"new-token", "expires_in":3600,"token_type":"Bearer"}'

        def close(self):
            if stage == "after":
                state["revoked"] = True

    def send(request):
        if stage == "headers":
            state["revoked"] = True
        return httpx.Response(200, stream=Stream(), headers={"content-type": "application/json"})

    def guard():
        if state["revoked"]:
            raise ContextDenied("revoked")

    calls = client(monkeypatch, send)
    with pytest.raises(ContextDenied, match="revoked") as exc:
        oauth.access_token(credential(), guard)
    assert type(exc.value) is ContextDenied
    assert len(calls) == (0 if stage == "before" else 1)


def test_transport_failure_is_sanitized_and_deadline_is_checked(monkeypatch):
    def fail(request):
        raise httpx.ConnectError("private credential secret-canary")

    client(monkeypatch, fail)
    with pytest.raises(oauth.OAuthRefreshError, match="transport_error") as exc:
        oauth.access_token(credential(), lambda: None)
    assert "canary" not in str(exc.value)
    now = [0.0]
    monkeypatch.setattr(oauth.time, "monotonic", lambda: now[0])

    def expired(request):
        now[0] = 6.0
        return success()

    client(monkeypatch, expired)
    with pytest.raises(ContextDenied):
        oauth.access_token(credential(), lambda: None)
