"""In-memory refresh of an existing, server-owned Google OAuth grant."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from gateway import client_context_reads as transport
from gateway.client_context_policy import (
    ContextDenied, _reject_json_constant, _unique_object, require,
)

TOKEN_URL = "https://oauth2.googleapis.com/token"
MAX_RESPONSE = 65536
ERROR_CODES = frozenset({
    "invalid_request", "invalid_client", "invalid_grant", "unauthorized_client",
    "unsupported_grant_type", "invalid_scope", "access_denied", "temporarily_unavailable",
    "server_error", "rate_limit_exceeded",
})


class OAuthRefreshError(ContextDenied):
    """Only fixed codes and HTTP status may reach an operator; never vendor prose."""

    def __init__(self, code: str, status: int | None = None):
        allowed = ERROR_CODES | {"http_error", "transport_error", "invalid_response"}
        self.code = code if code in allowed else "invalid_response"
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        super().__init__(f"google_oauth_refresh:{self.code}:status={self.status}")


def _token(value: object) -> str:
    if not isinstance(value, str):
        raise ContextDenied("invalid OAuth token")
    require(type(value) is str and 0 < len(value) <= 8192
            and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", value))
    return value


def _refresh(raw: dict, guard: Callable[[], None]) -> str:
    form = {"grant_type": "refresh_token"}
    for key in ("client_id", "client_secret", "refresh_token"):
        value = raw.get(key)
        if not isinstance(value, str):
            raise ContextDenied("invalid OAuth credential")
        require(type(value) is str and 0 < len(value) <= 8192
                and all(32 <= ord(char) <= 126 for char in value))
        form[key] = value
    body = urlencode(form).encode("ascii")
    require(len(body) <= MAX_RESPONSE)
    deadline = time.monotonic() + 5.0

    def check():
        guard()
        require(time.monotonic() < deadline)

    check()
    try:
        with transport._http_client() as client:
            with client.stream("POST", TOKEN_URL, content=body, timeout=5.0,
                               follow_redirects=False, headers={
                                   "Content-Type": "application/x-www-form-urlencoded",
                                   "Accept": "application/json", "Accept-Encoding": "identity",
                               }) as response:
                check()
                status = response.status_code
                if 300 <= status < 400:
                    raise OAuthRefreshError("http_error", status)
                try:
                    require(response.headers.get("content-encoding", "identity") == "identity")
                    require(response.headers.get("content-type", "").split(";", 1)[0].strip()
                            == "application/json")
                    length = response.headers.get("content-length")
                    require(length is None or (length.isdecimal() and int(length) <= MAX_RESPONSE))
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        check()
                        require(len(data) + len(chunk) <= MAX_RESPONSE)
                        data.extend(chunk)
                    check()
                    result = json.loads(data, object_pairs_hook=_unique_object,
                                        parse_constant=_reject_json_constant)
                    require(type(result) is dict)
                except (ValueError, TypeError, ContextDenied):
                    # Recheck revocation separately: a stale route must not become
                    # an ordinary unavailable-provider response.
                    check()
                    raise OAuthRefreshError("invalid_response", status) from None
                if status != 200:
                    error = result.get("error")
                    code = error if type(error) is str and error in ERROR_CODES else "http_error"
                    raise OAuthRefreshError(code, status)
                try:
                    require(type(result.get("token_type")) is str
                            and result["token_type"].lower() == "bearer")
                    require(type(result.get("expires_in")) is int and 0 < result["expires_in"] <= 86400)
                    token = _token(result.get("access_token"))
                except ContextDenied:
                    raise OAuthRefreshError("invalid_response", status) from None
        check()
        return token
    except httpx.HTTPError:
        check()
        raise OAuthRefreshError("transport_error") from None


def access_token(raw: dict, guard: Callable[[], None]) -> str:
    """Accept only a pinned credential document loaded by the read executor."""
    guard()
    require(type(raw) is dict and raw.get("token_uri") == TOKEN_URL)
    expiry_raw = raw.get("expiry")
    if not isinstance(expiry_raw, str):
        raise ContextDenied("invalid OAuth credential expiry")
    require(type(expiry_raw) is str and len(expiry_raw) <= 64)
    try:
        expiry = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
    except ValueError:
        raise ContextDenied("invalid OAuth credential expiry") from None
    require(expiry.tzinfo is not None)
    token = (_token(raw.get("token")) if expiry > datetime.now(timezone.utc)
             else _refresh(raw, guard))
    guard()
    return token
