"""Bounded transport for the single operator-pinned Codewords Stripe read."""

from __future__ import annotations

import json
import math
import re
import time
from contextlib import contextmanager

import httpcore
import httpx

from gateway.client_context_policy import (
    ContextDenied, _reject_json_constant, _unique_object, fields, require,
)

URL = "https://api.stripe.com/v1/subscriptions"
ACCOUNT = "acct_1Oyuv1Ka5VSTfRCx"
VERSION = "2024-06-20"
MAX_RESPONSE = 1024 * 1024
READ_SECONDS = 5.0


@contextmanager
def _private_errors():
    try:
        yield
    except Exception:
        # HTTP exceptions and JSON decoder errors can retain credentials or rows.
        raise ContextDenied("Stripe read unavailable") from None


def _finite_float(value):
    result = float(value)
    require(math.isfinite(result))
    return result


def request(client, check, method, url, *, headers=None, params=None):
    """Return one strict JSON object; caller owns row validation and scan bounds.

    The trusted check callback retains its exception semantics (including the
    caller's scan-deadline signal), and runs outside provider-error translation.
    """
    with _private_errors():
        require(type(method) is str and method == "GET")
        require(type(url) is str and url == URL)
        headers = fields(headers, {"Authorization", "Stripe-Account", "Stripe-Version"}).copy()
        require(headers["Stripe-Account"] == ACCOUNT and headers["Stripe-Version"] == VERSION)
        token = headers["Authorization"]
        require(type(token) is str and len(token) <= 8192
                and re.fullmatch(r"Bearer rk_live_[A-Za-z0-9]+", token))
        if type(params) is not dict:
            raise ContextDenied("Stripe read unavailable")
        params = dict(params)
        require({"status", "limit", "created[lte]"} <= set(params)
                <= {"status", "limit", "created[lte]", "starting_after"})
        require(params["status"] == "all" and type(params["limit"]) is int and params["limit"] == 100)
        require(type(params["created[lte]"]) is int and 0 <= params["created[lte]"] <= 2**63 - 1)
        if "starting_after" in params:
            require(type(params["starting_after"]) is str
                    and re.fullmatch(r"sub_[A-Za-z0-9]{1,128}", params["starting_after"]))
        # HTTPX exposes no per-request proxy override. Fail closed on mounted
        # transports, environment proxies, or an explicitly proxied HTTPTransport.
        require(isinstance(client, httpx.Client) and not client.trust_env and not client._mounts)
        if isinstance(client._transport, httpx.HTTPTransport):
            require(type(client._transport._pool) is httpcore.ConnectionPool)
        require(not any(client.event_hooks.values()))
        # Construct directly so client defaults cannot add cookies, headers or params.
        outgoing = httpx.Request("GET", URL, params=params, headers={
            "Accept": "application/json", "Accept-Encoding": "identity", **headers,
        }, extensions={"timeout": httpx.Timeout(READ_SECONDS).as_dict()})

    deadline = time.monotonic() + READ_SECONDS

    def guard():
        check()
        require(time.monotonic() < deadline)

    guard()
    with _private_errors():
        # Client.send logs the full cursor URL and untrusted reason phrase at INFO.
        # A direct transport call sends exactly one request, without that log,
        # redirect handling, client authentication, cookies, or response hooks.
        response = client._transport.handle_request(outgoing)
    try:
        guard()
        with _private_errors():
            require(response.status_code == 200)
            require(response.headers.get("content-type", "").split(";")[0].strip() == "application/json")
            require("content-encoding" not in response.headers)
            length = response.headers.get("content-length")
            if length is not None:
                require(re.fullmatch(r"[0-9]{1,10}", length) and int(length) <= MAX_RESPONSE)
            chunks = iter(response.iter_bytes())
        data = bytearray()
        while True:
            guard()
            with _private_errors():
                chunk = next(chunks, None)
            guard()
            if chunk is None:
                break
            require(len(data) + len(chunk) <= MAX_RESPONSE)
            data.extend(chunk)
    finally:
        with _private_errors():
            response.close()
    guard()
    with _private_errors():
        result = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                            parse_constant=_reject_json_constant, parse_float=_finite_float)
        require(type(result) is dict)
    guard()
    return result
