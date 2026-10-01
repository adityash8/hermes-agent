"""Exercise the dedicated transport with real HTTPX request/stream machinery."""

import json
import logging
import traceback

import httpx
import pytest

from gateway import client_context_codewords_stripe_http as stripe
from gateway import client_context_reads as shared
from gateway.client_context_policy import ContextDenied

KEY = "rk_live_PrivateCredentialCanary"
PRIVATE = "private-customer@example.test"
HEADERS = {"Authorization": "Bearer " + KEY, "Stripe-Account": stripe.ACCOUNT,
           "Stripe-Version": stripe.VERSION}
PARAMS = {"status": "all", "limit": 100, "created[lte]": 1700000000}


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks, step=lambda: None, close=lambda: None):
        self.chunks = chunks
        self.step = step
        self.on_close = close
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.step()
            yield chunk

    def close(self):
        self.closed = True
        self.on_close()


def invoke(client, check=lambda: None, **kwargs):
    return stripe.request(client, check, kwargs.pop("method", "GET"), kwargs.pop("url", stripe.URL),
                          headers=kwargs.pop("headers", dict(HEADERS)),
                          params=kwargs.pop("params", dict(PARAMS)), **kwargs)


def client_for(handler, **kwargs):
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False, **kwargs)


@pytest.mark.parametrize("size", [607274, 1024 * 1024])
def test_large_page_uses_local_budget_and_pinned_request(size):
    body = json.dumps({"data": ""}).encode()
    body = body[:-2] + b"x" * (size - len(body)) + body[-2:]
    stream = Stream([body[i:i+8192] for i in range(0, len(body), 8192)])

    def send(request):
        assert request.method == "GET" and str(request.url).startswith(stripe.URL + "?")
        assert dict(request.url.params) == {**{k: str(v) for k, v in PARAMS.items()}, "starting_after": "sub_Safe123"}
        assert request.headers["authorization"] == "Bearer " + KEY
        assert request.headers["stripe-account"] == stripe.ACCOUNT
        assert request.headers["stripe-version"] == stripe.VERSION
        assert request.headers["accept-encoding"] == "identity"
        assert "cookie" not in request.headers and "x-private" not in request.headers
        assert all(value == 5.0 for value in request.extensions["timeout"].values())
        return httpx.Response(200, headers={"Content-Type": "application/json; charset=utf-8"}, stream=stream)

    with client_for(send, headers={"X-Private": PRIVATE}, cookies={"private": PRIVATE},
                    params={"expand[]": "customer"}, auth=("private", KEY), timeout=90) as client:
        result = invoke(client, params={**PARAMS, "starting_after": "sub_Safe123"})
    assert len(result["data"]) == size - len(json.dumps({"data": ""}).encode())
    assert stream.closed
    # Prove the same large body still fails through the unchanged shared lane.
    with client_for(lambda _: httpx.Response(200, headers={"content-type": "application/json"}, content=body)) as client:
        with pytest.raises(ContextDenied):
            shared._request(client, lambda: None, "GET", stripe.URL)


@pytest.mark.parametrize("changes", [
    {"method": "POST"}, {"method": "get"}, {"method": "DELETE"},
    *({"url": url} for url in ["http://api.stripe.com/v1/subscriptions", stripe.URL + "/",
        stripe.URL + "?limit=100", stripe.URL + "#fragment", "https://api.stripe.com:443/v1/subscriptions",
        "https://api.stripe.com/v1/accounts", "https://api.stripe.com.evil.test/v1/subscriptions",
        "https://user@api.stripe.com/v1/subscriptions"]),
    {"headers": None}, {"headers": {**HEADERS, "Stripe-Account": "acct_Foreign"}},
    {"headers": {**HEADERS, "Stripe-Version": "2025-01-01"}},
    {"headers": {**HEADERS, "stripe-account": stripe.ACCOUNT}},
    {"headers": {**HEADERS, "Host": "evil.test"}},
    {"headers": {**HEADERS, "Accept-Encoding": "gzip"}},
    *({"headers": {**HEADERS, "Authorization": value}} for value in [
        "Bearer sk_live_Private", "Bearer rk_test_Private", "Bearer rk_live_", "Basic " + KEY,
        "Bearer rk_live_Private\r\nX-Evil: true", "Bearer ${STRIPE_SECRET_KEY}"]),
    {"params": None}, {"params": {}}, {"params": {**PARAMS, "status": "active"}},
    *({"params": {**PARAMS, "limit": value}} for value in [10, 101, "100", True]),
    *({"params": {**PARAMS, "created[lte]": value}} for value in [True, "1700000000", 1.2, -1]),
    {"params": {**PARAMS, "expand[]": "customer"}},
    {"params": {**PARAMS, "created[gte]": 1}},
    *({"params": {**PARAMS, "starting_after": value}} for value in [
        "", "cus_Private", "sub_x&expand[]=customer", "sub_" + "x" * 129, None]),
])
def test_unapproved_request_never_reaches_network(changes):
    with client_for(lambda _: pytest.fail("denied request reached HTTP")) as client:
        with pytest.raises(ContextDenied):
            invoke(client, **changes)


@pytest.mark.parametrize("options", [
    {"trust_env": True}, {"proxy": "http://127.0.0.1:9"},
    {"transport": httpx.HTTPTransport(proxy="http://127.0.0.1:9")},
    {"event_hooks": {"request": [lambda _: pytest.fail("request hook executed")]}},
])
def test_proxy_or_mutating_client_is_denied(options):
    options = dict(options)
    options.setdefault("trust_env", False)
    with httpx.Client(**options) as client:
        with pytest.raises(ContextDenied):
            invoke(client)


@pytest.mark.parametrize("chunks", [[b"x" * (1024 * 1024 + 1)], [b"x" * 600000, b"x" * 600000]])
def test_oversized_individual_or_accumulated_chunks_fail_closed(chunks):
    stream = Stream(chunks)
    with client_for(lambda _: httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)) as client:
        with pytest.raises(ContextDenied):
            invoke(client)
    assert stream.closed


@pytest.mark.parametrize("status,headers,body", [
    (302, {"location": "https://evil.test/" + KEY}, b""),
    (403, {}, (KEY + PRIVATE).encode()),
    (200, {"content-type": "text/html"}, b"{}"),
    (200, {"content-encoding": "identity"}, b"{}"),
    (200, {"content-encoding": "gzip"}, b"{}"),
    (200, {"content-length": str(1024 * 1024 + 1)}, b"{}"),
    (200, {}, b'{"x":1,"x":2}'), (200, {}, b'{"x":{"a":1,"a":2}}'),
    *[(200, {}, ('{"x":' + value + '}').encode()) for value in ["NaN", "Infinity", "-Infinity", "1e999"]],
    (200, {}, ('{"' + KEY + '":' + PRIVATE + '}').encode()),
    (200, {}, b'\xff'), (200, {}, b'[]'), (200, {}, b'null'),
])
def test_untrusted_response_denies_without_private_error_details(status, headers, body, caplog):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(status, headers={"content-type": "application/json", **headers}, stream=Stream([body]))

    with client_for(send, follow_redirects=True) as client:
        with pytest.raises(ContextDenied) as error:
            invoke(client)
    assert len(calls) == 1
    output = "".join(traceback.format_exception(error.value)) + caplog.text
    assert KEY not in output and PRIVATE not in output


@pytest.mark.parametrize("phase", ["send", "chunk", "close"])
def test_provider_exceptions_never_expose_credentials(phase, caplog):
    def fail():
        raise httpx.ReadTimeout(KEY + PRIVATE)

    stream = Stream([b"{}"], step=fail if phase == "chunk" else lambda: None,
                    close=fail if phase == "close" else lambda: None)

    def send(_):
        if phase == "send":
            fail()
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    with client_for(send) as client:
        with pytest.raises(ContextDenied) as error:
            invoke(client)
    output = "".join(traceback.format_exception(error.value)) + caplog.text
    assert KEY not in output and PRIVATE not in output


def test_http_error_does_not_log_cursor_or_private_reason_phrase(caplog):
    caplog.set_level(logging.INFO)
    cursor = "sub_PrivateCursorCanary"
    with client_for(lambda _: httpx.Response(
        403, extensions={"reason_phrase": (KEY + PRIVATE).encode()},
        stream=Stream([b"{}"]))) as client:
        with pytest.raises(ContextDenied) as error:
            invoke(client, params={**PARAMS, "starting_after": cursor})
    output = "".join(traceback.format_exception(error.value)) + caplog.text
    assert all(value not in output for value in (KEY, PRIVATE, cursor))


@pytest.mark.parametrize("phase", ["before", "headers", "chunk", "close", "parsed"])
@pytest.mark.parametrize("failure", [ContextDenied, TimeoutError])
def test_check_revocation_and_deadline_prevent_release(phase, failure, monkeypatch):
    revoked = phase == "before"
    calls = []

    def revoke():
        nonlocal revoked
        revoked = True

    def check():
        if revoked:
            raise failure()

    stream = Stream([b'{"data":', b'[]}'], step=revoke if phase == "chunk" else lambda: None,
                    close=revoke if phase == "close" else lambda: None)

    def send(request):
        calls.append(request)
        if phase == "headers":
            revoke()
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    original = stripe._unique_object

    def parse(pairs):
        result = original(pairs)
        if phase == "parsed":
            revoke()
        return result

    monkeypatch.setattr(stripe, "_unique_object", parse)
    with client_for(send) as client:
        with pytest.raises(failure):
            invoke(client, check)
    assert len(calls) == (0 if phase == "before" else 1)
    if calls:
        assert stream.closed


@pytest.mark.parametrize("phase", ["headers", "chunk", "close", "parsed"])
def test_page_deadline_is_total_not_only_socket_timeout(phase, monkeypatch):
    now = 0.0

    def expire():
        nonlocal now
        now = 5.0

    monkeypatch.setattr(stripe.time, "monotonic", lambda: now)
    stream = Stream([b"{}"], step=expire if phase == "chunk" else lambda: None,
                    close=expire if phase == "close" else lambda: None)
    original = stripe._unique_object

    def parse(pairs):
        result = original(pairs)
        if phase == "parsed":
            expire()
        return result

    monkeypatch.setattr(stripe, "_unique_object", parse)

    def send(_):
        if phase == "headers":
            expire()
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    with client_for(send) as client:
        with pytest.raises(ContextDenied):
            invoke(client)
    assert stream.closed
