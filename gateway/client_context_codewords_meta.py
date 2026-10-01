"""Fixed Codewords Meta reads through the operator-owned Pipeboard connection."""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from gateway import client_context_reads as transport
from gateway.client_context_analytics import calendar_date
from gateway.client_context_policy import (
    ContextDenied, _reject_json_constant, _unique_object, fields, items, require,
)

ENDPOINT = "https://mcp.pipeboard.co/meta-ads-mcp"
ENV_PATH = Path("/Users/adityasheth/.config/pipeboard/env")
ACCOUNT = "1143741027937235"
OBJECT = "act_" + ACCOUNT
BUSINESS = "2028463037640474"
CURRENCY = "GBP"
TIME_ZONE = "Europe/London"
PROTOCOL = "2025-03-26"
MAX_RESPONSE = 65536
IDENTITY_FIELDS = "id,account_id,name,business,currency,timezone_name"
OPERATIONS = {"daily": ("account", "report"),
              "campaign_reports": ("campaign", "report"),
              "adset_reports": ("adset", "report"),
              "campaign_config": ("campaign", "config"),
              "adset_config": ("adset", "config")}
ROW_LIMIT = 20
_STATUSES = frozenset({"ACTIVE", "PAUSED", "DELETED", "ARCHIVED", "IN_PROCESS", "WITH_ISSUES",
                       "CAMPAIGN_PAUSED", "ADSET_PAUSED", "PENDING_REVIEW", "DISAPPROVED",
                       "PREAPPROVED", "PENDING_BILLING_INFO"})
_OBJECTIVES = frozenset({"APP_INSTALLS", "BRAND_AWARENESS", "CONVERSIONS", "EVENT_RESPONSES",
    "LEAD_GENERATION", "LINK_CLICKS", "LOCAL_AWARENESS", "MESSAGES", "OFFER_CLAIMS", "PAGE_LIKES",
    "POST_ENGAGEMENT", "PRODUCT_CATALOG_SALES", "REACH", "STORE_VISITS", "VIDEO_VIEWS",
    "OUTCOME_AWARENESS", "OUTCOME_TRAFFIC", "OUTCOME_ENGAGEMENT", "OUTCOME_LEADS", "OUTCOME_SALES",
    "OUTCOME_APP_PROMOTION"})


def _json(value):
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)


def _credential():
    with ENV_PATH.open("rb") as stream:
        raw = stream.read(MAX_RESPONSE + 1)
    require(len(raw) <= MAX_RESPONSE)
    values = dotenv_values(stream=io.StringIO(raw.decode("utf-8")), interpolate=False)
    key = values.get("PIPEBOARD_READ_TOKEN")
    require(type(key) is str and 0 < len(key) <= 8192
            and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", key))
    return key, hashlib.sha256(raw).digest()


def _rpc(client, check, key, body, session=None):
    headers = {"Authorization": "Bearer " + key,
               "Accept": "application/json, text/event-stream", "Accept-Encoding": "identity",
               "MCP-Protocol-Version": PROTOCOL}
    if session is not None:
        headers["Mcp-Session-Id"] = session
    check()
    deadline = time.monotonic() + 5
    with client.stream("POST", ENDPOINT, headers=headers, json=body,
                       timeout=5.0, follow_redirects=False) as response:
        check()
        require(response.status_code in ({200, 202, 204} if "id" not in body else {200}))
        require(response.headers.get("content-encoding", "identity").lower() == "identity")
        session_id = response.headers.get("mcp-session-id", session)
        require(session_id is None or (0 < len(session_id) <= 256
                and re.fullmatch(r"[A-Za-z0-9._~-]+", session_id)))
        require(session is None or session_id == session)
        kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
        data = bytearray()
        complete_event = False
        for chunk in response.iter_bytes():
            check()
            require(time.monotonic() < deadline and len(data) + len(chunk) <= MAX_RESPONSE)
            data.extend(chunk)
            check()
            if "id" in body and kind == "text/event-stream":
                complete, _, pending = bytes(data).replace(b"\r\n", b"\n").rpartition(b"\n\n")
                if any(line.startswith(b"data:") for line in complete.splitlines()):
                    require(not pending.strip())
                    complete_event = True
                    # Streamable HTTP may keep SSE open after the response. Close
                    # this POST once its one complete envelope has arrived.
                    break
    check()
    require(time.monotonic() < deadline)
    if "id" not in body:
        # MCP notifications have no JSON-RPC response. Some servers send an HTTP
        # acknowledgement body; bound and discard it, never interpret it as a tool result.
        return None, session_id
    if kind == "text/event-stream":
        require(complete_event)
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


def _tool_data(result):
    require(type(result) is dict and set(result) <= {"content", "isError", "structuredContent", "_meta"}
            and result.get("isError", False) is False)
    content = items(result.get("content"), 1, empty=False)
    require(len(content) == 1 and content[0].get("type") == "text")
    raw = _json(content[0]["text"])
    require(type(raw) is dict and "error" not in raw)
    if "structuredContent" in result:
        structured = result["structuredContent"]
        require(type(structured) is dict)
        if "result" in structured:
            fields(structured, {"result"})
            require(type(structured["result"]) is str)
            structured = _json(structured["result"])
        # Python equality conflates JSON booleans and numbers (True == 1).
        require(json.dumps(structured, sort_keys=True, allow_nan=False)
                == json.dumps(raw, sort_keys=True, allow_nan=False))
    return raw


def _identity(raw):
    fields(raw, {"id", "account_id", "name", "business", "currency", "timezone_name"})
    require(raw["id"] == OBJECT and raw["account_id"] == ACCOUNT
            and raw["currency"] == CURRENCY and raw["timezone_name"] == TIME_ZONE)
    business = raw["business"]
    require(type(business) is dict and set(business) <= {"id", "name"}
            and business.get("id") == BUSINESS)


def _metric(value, *, integer=False):
    require(type(value) in (str, int, float))
    text = str(value)
    require(len(text) <= 32 and re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", text))
    number = Decimal(text)
    require(0 <= number <= 10**15 and (not integer or number == number.to_integral_value()))
    return int(number) if integer else format(number, "f")


def _normalize(raw, start, end):
    fields(raw, {"object_id", "level", "time_range", "time_breakdown", "total_segments",
                 "aggregates", "segmented_metrics", "execution_time_ms", "meta_api_calls"})
    require(raw["object_id"] == OBJECT and raw["level"] == "account"
            and raw["time_range"] == {"since": start.isoformat(), "until": end.isoformat()}
            and raw["time_breakdown"] == "day" and type(raw["aggregates"]) is dict)
    segments = items(raw["segmented_metrics"], 31)
    require(type(raw["total_segments"]) is int and raw["total_segments"] == len(segments)
            and len(segments) == (end - start).days + 1)
    rows, seen = [], set()
    for segment in segments:
        fields(segment, {"period", "period_start", "period_end", "metrics"})
        day = calendar_date(segment["period"])
        require(start <= day <= end and day not in seen
                and segment["period_start"] == segment["period_end"] == day.isoformat())
        seen.add(day)
        metrics = segment["metrics"]
        require(type(metrics) is dict and set(metrics) <= {"data", "paging"})
        if "paging" in metrics:
            paging = metrics["paging"]
            require(type(paging) is dict and set(paging) <= {"cursors"})
            if "cursors" in paging:
                require(type(paging["cursors"]) is dict
                        and set(paging["cursors"]) <= {"before", "after"})
        for row in items(metrics["data"], 1):
            required = {"account_id", "date_start", "date_stop", "impressions", "clicks", "spend"}
            # Hosted insights has a fixed vendor field set, without a fields
            # argument. Discard its extra aggregate metrics and account label.
            optional = {"account_currency", "account_name", "cpc", "cpm", "ctr", "reach", "frequency",
                        "actions", "action_values", "conversions", "unique_clicks", "cost_per_action_type"}
            require(type(row) is dict and required <= set(row) <= required | optional)
            require(row["account_id"] == ACCOUNT and row.get("account_currency", CURRENCY) == CURRENCY
                    and row["date_start"] == row["date_stop"] == day.isoformat())
            rows.append({"date": day.isoformat(), "impressions": _metric(row["impressions"], integer=True),
                         "clicks": _metric(row["clicks"], integer=True), "spend": _metric(row["spend"])})
    return {"rows": sorted(rows, key=lambda row: row["date"]), "currency": CURRENCY,
            "pagination_complete": True, "incomplete_reason": None,
            "coverage": "account_daily; omitted_dates_have_no_returned_data"}


def report(start, end, guard, operation="daily"):
    """No caller-selected resources, fields, tool names, endpoints or credential paths."""
    try:
        require(type(operation) is str and operation in OPERATIONS)
        require(type(start) is date and type(end) is date)
        require(0 <= (end - start).days < 31 and end < datetime.now(ZoneInfo(TIME_ZONE)).date())
        guard()
        key, fingerprint = _credential()
        deadline = time.monotonic() + 120

        def check():
            guard()
            require(time.monotonic() < deadline and _credential()[1] == fingerprint)
            guard()

        with transport._http_client() as client:
            initialized, session = _rpc(client, check, key, {
                "jsonrpc": "2.0", "id": "initialize", "method": "initialize",
                "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                           "clientInfo": {"name": "codewords-fixed-read", "version": "1"}}})
            require(type(initialized) is dict and initialized.get("protocolVersion") == PROTOCOL)
            _rpc(client, check, key, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)

            def identity(request_id):
                result, _ = _rpc(client, check, key, {
                    "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                    "params": {"name": "get_account_info", "arguments": {"account_id": OBJECT,
                               "fields": "id,account_id,name,business,currency,timezone_name"}}}, session)
                _identity(_tool_data(result))

            identity("meta-before")
            if operation == "daily":
                result, _ = _rpc(client, check, key, {
                    "jsonrpc": "2.0", "id": "meta-report", "method": "tools/call",
                    "params": {"name": "get_insights", "arguments": {"object_id": OBJECT,
                        "time_range": {"since": start.isoformat(), "until": end.isoformat()},
                        "time_breakdown": "day", "level": "account", "limit": 31, "after": "",
                        "breakdown": "", "action_breakdowns": []}}}, session)
                normalized = _normalize(_tool_data(result), start, end)
            else:
                result, _ = _rpc(client, check, key, {
                    "jsonrpc": "2.0", "id": "meta-expanded", "method": "tools/call",
                    "params": {"name": "get_account_info", "arguments": {
                        "account_id": OBJECT, "fields": _expansion(start, end, operation)}}}, session)
                normalized = _expanded(_tool_data(result), start, end, operation)
            identity("meta-after")
        check()
        return normalized
    except Exception:
        raise ContextDenied("Codewords Meta fixed read unavailable") from None


def _expansion(start, end, operation):
    level, kind = OPERATIONS[operation]
    if kind == "report":
        period = json.dumps({"since": start.isoformat(), "until": end.isoformat()}, separators=(",", ":"))
        ids = "campaign_id" + (",adset_id" if level == "adset" else "")
        edge = (f"insights.level({level}).time_range({period}).limit({ROW_LIMIT})"
                + "{account_id,account_currency,date_start,date_stop," + ids + ",impressions,clicks,spend}")
    else:
        selected = "id,account_id,status,daily_budget,lifetime_budget"
        selected += ",objective" if level == "campaign" else ",campaign{id,account_id},bid_amount"
        edge = f"{level}s.limit({ROW_LIMIT})" + "{" + selected + "}"
    return IDENTITY_FIELDS + "," + edge


def _resource_id(value):
    require(type(value) is str and re.fullmatch(r"[1-9][0-9]{0,31}", value))
    return value


def _expanded(raw, start, end, operation):
    level, kind = OPERATIONS[operation]
    edge = "insights" if kind == "report" else level + "s"
    fields(raw, set(IDENTITY_FIELDS.split(",")) | {edge})
    _identity({key: raw[key] for key in IDENTITY_FIELDS.split(",")})
    collection = raw[edge]
    require(type(collection) is dict and set(collection) <= {"data", "paging"})
    paging = collection.get("paging", {})
    require(type(paging) is dict and set(paging) <= {"cursors", "next", "previous"})
    # Pagination URLs can carry access tokens. Never return, parse or follow them.
    has_next = "next" in paging
    if has_next:
        require(type(paging["next"]) is str and 0 < len(paging["next"]) <= 8192)
    rows, seen = [], set()
    for row in items(collection.get("data"), ROW_LIMIT):
        require(type(row) is dict and row.get("account_id") == ACCOUNT)
        if kind == "report":
            expected = {"account_id", "account_currency", "date_start", "date_stop", "campaign_id",
                        "impressions", "clicks", "spend"} | ({"adset_id"} if level == "adset" else set())
            fields(row, expected)
            require(row["account_currency"] == CURRENCY and row["date_start"] == start.isoformat()
                    and row["date_stop"] == end.isoformat())
            normalized = {"campaign_id": _resource_id(row["campaign_id"]),
                          "impressions": _metric(row["impressions"], integer=True),
                          "clicks": _metric(row["clicks"], integer=True), "spend": _metric(row["spend"])}
            if level == "adset":
                normalized["adset_id"] = _resource_id(row["adset_id"])
        else:
            required = {"id", "account_id", "status"} | ({"campaign"} if level == "adset" else {"objective"})
            optional = {"daily_budget", "lifetime_budget"} | ({"bid_amount"} if level == "adset" else set())
            require(required <= set(row) <= required | optional)
            require(type(row["status"]) is str and row["status"] in _STATUSES)
            normalized = {level + "_id": _resource_id(row["id"]), "status": row["status"]}
            if level == "campaign":
                require(type(row["objective"]) is str and row["objective"] in _OBJECTIVES)
                normalized["objective"] = row["objective"]
            else:
                campaign = fields(row["campaign"], {"id", "account_id"})
                require(campaign["account_id"] == ACCOUNT)
                normalized["campaign_id"] = _resource_id(campaign["id"])
            for field in optional & set(row):
                normalized[field + "_minor_units"] = _metric(row[field], integer=True)
        identity = normalized[level + "_id"]
        require(identity not in seen)
        seen.add(identity)
        rows.append(normalized)
    complete = not has_next and len(rows) < ROW_LIMIT
    return {"rows": rows, "currency": CURRENCY, "pagination_complete": complete,
            "row_limit": ROW_LIMIT, "rows_returned": len(rows),
            "incomplete_reason": None if complete else "row_limit_reached_or_more_rows_possible",
            "coverage": operation + "; reports aggregate requested dates; configuration is current; "
                        "no account totals; budget and bid fields are minor currency units"}
