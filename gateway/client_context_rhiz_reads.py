"""SPI aggregate reads for the trusted, stateless restricted Slack runtime."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from agent.secret_scope import get_secret
from gateway.client_context_analytics import calendar_date
from gateway.client_context_codewords_gsc import _json, _rpc
from gateway.client_context_policy import fields, items, require
from gateway.client_context_reads import _http_client

_GA4 = "GOOGLE_ANALYTICS_RUN_REPORT"
_GSC = "GOOGLE_SEARCH_CONSOLE_SEARCH_ANALYTICS_QUERY"
_BINDINGS = {
    "spi_ga4_report": (_GA4, "google_analytics_lench-tenter"),
    "spi_gsc_report": (_GSC, "google_search_console_beck-fanner"),
}
_UNAVAILABLE = '{"error":"SPI report unavailable or request denied"}'


def schemas():
    """No tool discovery, generic proxy, or resource overrides enter this surface."""
    descriptions = {
        "spi_ga4_report": "SPI GA4 aggregate sessions for properties/549700824.",
        "spi_gsc_report": "SPI GSC aggregate clicks, impressions, CTR and position for screenprintindia.com.",
    }
    return [{"type": "function", "function": {
        "name": name, "description": description + " Dates are inclusive, at most 31 completed days within the past 366 days.",
        "parameters": {"type": "object", "properties": {
            "start_date": {"type": "string", "format": "date"},
            "end_date": {"type": "string", "format": "date"}},
            "required": ["start_date", "end_date"], "additionalProperties": False},
    }} for name, description in descriptions.items()]


def _credential():
    key = get_secret("COMPOSIO_CONSUMER_API_KEY")
    require(type(key) is str and 0 < len(key) <= 8192
            and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", key))
    return key


def _payload(name, start, end):
    if name == "spi_ga4_report":
        return {"property": "properties/549700824",
                "dateRanges": [{"startDate": start, "endDate": end}],
                "metrics": [{"name": "sessions"}], "limit": 1}
    return {"site_url": "https://www.screenprintindia.com/", "start_date": start,
            "end_date": end, "dimensions": [], "search_type": "web", "data_state": "final",
            "aggregation_type": "byProperty", "row_limit": 1, "start_row": 0}


def _report_data(result, slug):
    require(type(result) is dict and result.get("isError", False) is False)
    content = items(result.get("content"), 1, empty=False)
    require(type(content[0]) is dict and content[0].get("type") == "text")
    envelope = _json(content[0]["text"])
    require(type(envelope) is dict and envelope.get("successful") is True and not envelope.get("error"))
    data = envelope["data"]
    require(type(data) is dict and data.get("success", True) is True and not data.get("error"))
    results = items(data.get("results"), 1, empty=False)
    require(type(results[0]) is dict and results[0].get("tool_slug") == slug)
    response = results[0]["response"]
    require(type(response) is dict and response.get("successful") is True and not response.get("error"))
    require(type(response["data"]) is dict)
    return response["data"]


def _number(value, maximum=10**15, *, integer=False):
    require(type(value) in {int, float, str})
    if type(value) is str:
        require(len(value) <= 32 and re.fullmatch(r"\d+(?:\.\d+)?", value))
    number = float(value)
    require(math.isfinite(number) and 0 <= number <= maximum)
    if integer:
        require(number.is_integer())
        return int(number)
    return number


def _normalize(name, raw):
    if name == "spi_ga4_report":
        headers = items(raw.get("metricHeaders"), 1, empty=False)
        require(type(headers[0]) is dict and headers[0].get("name") == "sessions")
        require(raw.get("dimensionHeaders", []) == [])
        rows = items(raw.get("rows", []), 1)
        require(type(raw.get("rowCount", len(rows))) is int
                and raw.get("rowCount", len(rows)) == len(rows))
        if not rows:
            return {"sessions": 0}
        require(type(rows[0]) is dict and rows[0].get("dimensionValues", []) == [])
        values = items(rows[0].get("metricValues"), 1, empty=False)
        fields(values[0], {"value"})
        return {"sessions": _number(values[0]["value"], integer=True)}
    require(raw.get("responseAggregationType") == "byProperty")
    rows = items(raw.get("rows", []), 1)
    if not rows:
        return {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0}
    row = rows[0]
    require(type(row) is dict and row.get("keys", []) == [])
    require(set(row) <= {"keys", "clicks", "impressions", "ctr", "position"})
    clicks = _number(row["clicks"], integer=True)
    impressions = _number(row["impressions"], integer=True)
    require(clicks <= impressions)
    return {"clicks": clicks, "impressions": impressions,
            "ctr": _number(row["ctr"], maximum=1), "position": _number(row["position"])}


def validate(name, arguments) -> tuple[date, date]:
    """Validate without credentials or I/O so callers can reject a whole call batch."""
    require(type(name) is str and name in _BINDINGS)
    require(type(arguments) is str and len(arguments.encode("utf-8")) <= 1024)
    args = fields(_json(arguments), {"start_date", "end_date"})
    start, end = calendar_date(args["start_date"]), calendar_date(args["end_date"])
    # Pacific is conservative for completed Google Search reporting days.
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    require(0 <= (end - start).days < 31 and end < today and (today - start).days <= 366)
    return start, end


def execute(name, arguments, guard):
    """The caller must provide a trusted source/config guard, never a model argument."""
    try:
        require(guard() is not False)
        start, end = validate(name, arguments)
        key = _credential()

        def check():
            require(guard() is not False)
            require(_credential() == key)

        slug, account = _BINDINGS[name]
        with _http_client() as client:
            initialized, session = _rpc(client, check, key, {
                "jsonrpc": "2.0", "id": "initialize", "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "spi-fixed-read", "version": "1"}}})
            require(type(initialized) is dict and initialized.get("protocolVersion") == "2025-03-26")
            _rpc(client, check, key, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
            result, _ = _rpc(client, check, key, {
                "jsonrpc": "2.0", "id": "spi-report", "method": "tools/call",
                "params": {"name": "COMPOSIO_MULTI_EXECUTE_TOOL", "arguments": {
                    "sync_response_to_workbench": False, "tools": [{"tool_slug": slug,
                    "account": account, "arguments": _payload(name, start.isoformat(), end.isoformat())}]}}}, session)
        normalized = _normalize(name, _report_data(result, slug))
        check()
        return json.dumps({"start_date": start.isoformat(), "end_date": end.isoformat(), **normalized}, allow_nan=False)
    except Exception:
        return _UNAVAILABLE
