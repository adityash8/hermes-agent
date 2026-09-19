"""Fixed Meta account insights and Mixpanel event-count reads."""

from __future__ import annotations

import base64
import json
import re
from datetime import timedelta

from gateway.client_context_analytics import calendar_date
from gateway.client_context_policy import fields, items, require


META = "scoped_meta_daily_insights"
MIXPANEL = "scoped_mixpanel_daily_event_count"
OPERATIONS = {
    META: "Read daily Meta account impressions, clicks and spend in its approved currency.",
    MIXPANEL: "Read daily total counts of one approved Mixpanel event, without filters or breakdowns.",
}
_MIXPANEL_ORIGINS = {
    "us": "https://mixpanel.com", "eu": "https://eu.mixpanel.com", "in": "https://in.mixpanel.com",
}
_META_FIELDS = "account_id,account_currency,date_start,date_stop,impressions,clicks,spend"


def validate_parameters(entry):
    for key in ("account_id", "resource_id"):
        require(isinstance(entry[key], str) and re.fullmatch(r"[1-9][0-9]{0,19}", entry[key]))
    params = entry["parameters"]
    if entry["operation"] == META:
        fields(params, {"api_version", "currency"})
        require(params["api_version"] == "v26.0")
        require(isinstance(params["currency"], str) and re.fullmatch(r"[A-Z]{3}", params["currency"]))
    else:
        require(entry["operation"] == MIXPANEL)
        fields(params, {"region", "event"})
        require(params["region"] in _MIXPANEL_ORIGINS)
        # A literal approved event only: no property expressions, JSON, URLs or code.
        require(isinstance(params["event"], str)
                and re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$ .-]{0,79}", params["event"]))


def _count(value):
    require(isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,14}", value))
    return int(value)


def _meta_account(raw, grant):
    require(raw.get("id") == f"act_{grant.account_id}" and raw.get("account_id") == grant.account_id)
    require(isinstance(raw.get("business"), dict) and raw["business"].get("id") == grant.resource_id)
    require(raw.get("timezone_name") == grant.time_zone)
    require(raw.get("currency") == grant.parameters["currency"])


def _meta(grant, start, end, request, secret):
    token = secret()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.facebook.com/v26.0/act_{grant.account_id}"
    metadata_params = {"fields": "id,account_id,business{id},timezone_name,currency"}
    _meta_account(request("GET", url, headers=headers, params=metadata_params), grant)
    raw = request("GET", url + "/insights", headers=headers, params={
        "fields": _META_FIELDS, "level": "account", "time_increment": "1", "limit": "31",
        "time_range": json.dumps({"since": start.isoformat(), "until": end.isoformat()}, separators=(",", ":")),
    })
    require(set(raw) <= {"data", "paging"})
    paging = raw.get("paging", {})
    require(isinstance(paging, dict) and set(paging) <= {"cursors"})
    # Never follow a returned pagination URL. Account/day output is bounded to 31 rows.
    rows, seen = [], set()
    for row in items(raw.get("data"), 31):
        fields(row, set(_META_FIELDS.split(",")))
        require(row["account_id"] == grant.account_id)
        require(row["account_currency"] == grant.parameters["currency"])
        day = calendar_date(row["date_start"])
        require(row["date_start"] == row["date_stop"] and start <= day <= end and day not in seen)
        seen.add(day)
        spend = row["spend"]
        require(isinstance(spend, str) and re.fullmatch(r"(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,6})?", spend))
        rows.append({"date": day.isoformat(), "impressions": _count(row["impressions"]),
                     "clicks": _count(row["clicks"]), "spend": spend})
    _meta_account(request("GET", url, headers=headers, params=metadata_params), grant)
    expected = {start + timedelta(days=i) for i in range((end - start).days + 1)}
    missing = sorted(day.isoformat() for day in expected - seen)
    return {"currency": grant.parameters["currency"], "rows": sorted(rows, key=lambda row: row["date"]),
            "complete": not missing, "missing_dates": missing}


def _mixpanel(grant, start, end, request, secret):
    username, password = secret("USERNAME"), secret("PASSWORD")
    require(":" not in username)
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    params = grant.parameters
    raw = request("GET", _MIXPANEL_ORIGINS[params["region"]] + "/api/query/segmentation",
                  headers={"Authorization": f"Basic {credentials}"}, params={
                      "project_id": grant.account_id, "workspace_id": grant.resource_id,
                      "event": params["event"], "from_date": start.isoformat(), "to_date": end.isoformat(),
                      "unit": "day", "type": "general",
                  })
    fields(raw, {"data", "legend_size"})
    require(type(raw["legend_size"]) is int and raw["legend_size"] == 1)
    data = fields(raw["data"], {"series", "values"})
    dates = items(data["series"], 31)
    require(all(isinstance(day, str) for day in dates))
    expected = {(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)}
    require(len(dates) == len(set(dates)) and set(dates) == expected)
    values = fields(data["values"], {params["event"]})[params["event"]]
    fields(values, expected)
    rows = []
    for day in sorted(expected):
        count = values[day]
        require(type(count) is int and 0 <= count <= 999999999999999)
        rows.append({"date": day, "event_count": count})
    # The series name is used only for validation, never forwarded as vendor text.
    return {"rows": rows}


def execute(grant, start, end, request, secret):
    validate_parameters({"operation": grant.operation, "account_id": grant.account_id,
                         "resource_id": grant.resource_id, "parameters": grant.parameters})
    require(0 <= (end - start).days < 31)
    return {META: _meta, MIXPANEL: _mixpanel}[grant.operation](grant, start, end, request, secret)
