"""One fixed GSC report through the existing operator Composio connection."""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from dotenv import dotenv_values

import hermes_yaml as yaml

from gateway import client_context_google_stripe as google
from gateway import client_context_reads as transport
from gateway.client_context_analytics import calendar_date
from gateway.client_context_policy import (
    ContextDenied, _reject_json_constant, _unique_object, fields, items, require,
)

CONFIG_PATH = Path("/Users/adityasheth/.hermes/config.yaml")
ENV_PATH = Path("/Users/adityasheth/.hermes/.env")
ENDPOINT = "https://connect.composio.dev/mcp"
SLUG = "GOOGLE_SEARCH_CONSOLE_SEARCH_ANALYTICS_QUERY"
SITE = "sc-domain:codewords.ai"
MAX_RESPONSE = 65536
OPERATIONS = {"daily": ("date", "byProperty", 31),
              "queries": ("query", "byProperty", 20),
              "pages": ("page", "auto", 20)}


def _json(value):
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)


def _credential():
    with CONFIG_PATH.open("rb") as stream:
        raw = stream.read(1048577)
    require(len(raw) <= 1048576)
    config = yaml.safe_load(raw)
    server = config["mcp_servers"]["composio"]
    require(server.get("enabled") is True)
    # This parser cannot be turned into a configurable MCP/API proxy.
    require(server.get("command") == "npx" and ENDPOINT in server.get("args", []))
    key = server["env"]["COMPOSIO_KEY_RUNTIME"]
    env_fingerprint = None
    if key == "${COMPOSIO_CONSUMER_API_KEY}":
        with ENV_PATH.open("rb") as stream:
            env_raw = stream.read(65537)
        require(len(env_raw) <= 65536)
        values = dotenv_values(stream=io.StringIO(env_raw.decode("utf-8")), interpolate=False)
        key = values.get("COMPOSIO_CONSUMER_API_KEY")
        env_fingerprint = hashlib.sha256(env_raw).digest()
    require(type(key) is str and 0 < len(key) <= 8192
            and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", key))
    return key, (hashlib.sha256(raw).digest(), env_fingerprint)


def _rpc(client, check, key, body, session=None):
    headers = {"x-consumer-api-key": key, "Accept": "application/json, text/event-stream",
               "Accept-Encoding": "identity", "MCP-Protocol-Version": "2025-03-26"}
    if session:
        headers["Mcp-Session-Id"] = session
    check()
    deadline = time.monotonic() + 5
    with client.stream("POST", ENDPOINT, headers=headers, json=body, timeout=5.0) as response:
        check()
        require(response.status_code in ({200, 202, 204} if "id" not in body else {200}))
        require(response.headers.get("content-encoding", "identity") == "identity")
        session_id = response.headers.get("mcp-session-id", session)
        require(session_id is None or (0 < len(session_id) <= 256
                and re.fullmatch(r"[A-Za-z0-9._~-]+", session_id)))
        data = bytearray()
        for chunk in response.iter_bytes():
            check()
            require(time.monotonic() < deadline and len(data) + len(chunk) <= MAX_RESPONSE)
            data.extend(chunk)
        kind = response.headers.get("content-type", "").split(";")[0].strip()
    check()
    if "id" not in body:
        require(not data)
        return None, session_id
    if kind == "text/event-stream":
        events = []
        for event in data.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
            lines = [line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:")]
            if lines:
                events.append(_json("\n".join(lines)))
        require(len(events) == 1)
        result = events[0]
    else:
        require(kind == "application/json")
        result = _json(data)
    fields(result, {"jsonrpc", "id", "result"})
    require(result["jsonrpc"] == "2.0" and result["id"] == body["id"])
    return result["result"], session_id


def _report_data(result):
    require(type(result) is dict and result.get("isError", False) is False)
    content = items(result.get("content"), 1, empty=False)
    require(len(content) == 1 and content[0].get("type") == "text")
    envelope = _json(content[0]["text"])
    require(envelope.get("successful") is True and not envelope.get("error"))
    data = envelope["data"]
    require(data.get("success", True) is True and not data.get("error"))
    results = items(data.get("results"), 1, empty=False)
    require(len(results) == 1 and results[0].get("tool_slug") == SLUG)
    response = results[0]["response"]
    require(response.get("successful") is True and not response.get("error"))
    return response["data"]


def _normalize(raw, start, end, operation="daily"):
    if operation != "daily":
        return _dimensions(raw, operation)
    require(type(raw) is dict and set(raw) <= {"rows", "responseAggregationType"}
            and raw.get("responseAggregationType") == "byProperty")
    seen, rows = set(), []
    for row in items(raw.get("rows", []), 31):
        fields(row, {"keys", "clicks", "impressions", "ctr", "position"})
        keys = items(row["keys"], 1, empty=False)
        day = calendar_date(keys[0])
        require(start <= day <= end and day not in seen)
        seen.add(day)
        clicks, impressions = google._number(row["clicks"]), google._number(row["impressions"])
        require(clicks == int(clicks) and impressions == int(impressions) and clicks <= impressions)
        rows.append({"date": day.isoformat(), "clicks": int(clicks), "impressions": int(impressions),
                     "ctr": google._number(row["ctr"], maximum=1),
                     "position": google._number(row["position"])})
    return {"rows": sorted(rows, key=lambda row: row["date"]),
            "pagination_complete": True, "incomplete_reason": None,
            "coverage": "finalized_web_search_by_property; omitted_dates_have_no_returned_data"}


def report(start, end, guard, operation="daily"):
    """Accept dates only; connection, property, query, and transport stay server-owned."""
    try:
        require(type(operation) is str and operation in OPERATIONS)
        require(type(start) is date and type(end) is date)
        dimension, aggregation, limit = OPERATIONS[operation]
        require(0 <= (end - start).days < 31
                and end < datetime.now(ZoneInfo("America/Los_Angeles")).date())
        guard()
        key, fingerprint = _credential()

        def check():
            guard()
            require(_credential()[1] == fingerprint)

        with transport._http_client() as client:
            initialized, session = _rpc(client, check, key, {
                "jsonrpc": "2.0", "id": "initialize", "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "codewords-fixed-read", "version": "1"}}})
            require(type(initialized) is dict and initialized.get("protocolVersion") == "2025-03-26")
            _rpc(client, check, key, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
            result, _ = _rpc(client, check, key, {
                "jsonrpc": "2.0", "id": "gsc-report", "method": "tools/call",
                "params": {"name": "COMPOSIO_MULTI_EXECUTE_TOOL", "arguments": {
                    "sync_response_to_workbench": False, "tools": [{"tool_slug": SLUG,
                    "account": "ez money", "arguments": {"site_url": SITE,
                    "start_date": start.isoformat(), "end_date": end.isoformat(),
                    "dimensions": [dimension], "search_type": "web", "data_state": "final",
                    "aggregation_type": aggregation, "row_limit": limit, "start_row": 0}}]}}}, session)
        normalized = _normalize(_report_data(result), start, end, operation)
        check()
        return normalized
    except Exception:
        raise ContextDenied("Codewords GSC fixed read unavailable") from None


def _dimensions(raw, operation):
    """Dimension labels are pseudonymized; queries and URL paths can contain PII."""
    require(type(raw) is dict and set(raw) <= {"rows", "responseAggregationType"}
            and raw.get("responseAggregationType") == ("byPage" if operation == "pages" else "byProperty"))
    rows, seen = [], set()
    for row in items(raw.get("rows", []), 20):
        fields(row, {"keys", "clicks", "impressions", "ctr", "position"})
        keys = items(row["keys"], 1, empty=False)
        key = keys[0]
        require(type(key) is str and 0 < len(key.encode()) <= 4096 and key not in seen)
        if operation == "pages":
            page = urlsplit(key)
            require(page.scheme in {"https", "http"} and page.hostname is not None
                    and (page.hostname == "codewords.ai" or page.hostname.endswith(".codewords.ai"))
                    and page.username is None and page.password is None and page.port in {None, 80, 443})
        seen.add(key)
        clicks, impressions = google._number(row["clicks"]), google._number(row["impressions"])
        require(clicks == int(clicks) and impressions == int(impressions) and clicks <= impressions)
        rows.append({"dimension_hash": hashlib.sha256(key.encode()).hexdigest(),
                     "clicks": int(clicks), "impressions": int(impressions),
                     "ctr": google._number(row["ctr"], maximum=1),
                     "position": google._number(row["position"])})
    complete = len(rows) < 20
    return {"rows": rows, "dimension": OPERATIONS[operation][0], "pagination_complete": complete,
            "row_limit": 20, "rows_returned": len(rows),
            "incomplete_reason": None if complete else "row_limit_reached_or_more_rows_possible",
            "coverage": "finalized_web_search_top_rows; API omits anonymized queries and can omit rows; "
                        "pagination_complete describes returned API rows, not all search traffic; "
                        "dimension labels are SHA256 hashes to suppress PII and instructions; no totals"}
