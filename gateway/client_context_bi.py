"""Pinned PostHog saved trends and Tableau numeric snapshot reads."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal

from gateway.client_context_analytics import calendar_date
from gateway.client_context_policy import fields, items, require, timestamp

OPERATIONS = {
    "scoped_posthog_saved_insight": "Read one approved saved PostHog daily trend; cached numeric observations only.",
    "scoped_tableau_saved_view": "Read one approved Tableau Cloud date/value CSV snapshot; exact approved content only.",
}
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _matches(value, pattern):
    require(isinstance(value, str) and re.fullmatch(pattern, value) is not None)


def validate_parameters(entry):
    _matches(entry["account_id"], UUID)
    p = entry["parameters"]
    if entry["operation"] == "scoped_posthog_saved_insight":
        fields(p, {"region", "report_id", "definition_sha256"})
        require(p["region"] in {"us", "eu"})
        _matches(entry["resource_id"], r"[1-9][0-9]{0,19}")
        _matches(p["report_id"], r"[1-9][0-9]{0,19}")
        _matches(p["definition_sha256"], r"[0-9a-f]{64}")
    else:
        require(entry["operation"] == "scoped_tableau_saved_view")
        fields(p, {"pod", "workbook_id", "view_updated_at", "workbook_updated_at", "report_sha256"})
        _matches(entry["resource_id"], UUID)
        _matches(p["workbook_id"], UUID)
        _matches(p["pod"], r"[a-z0-9][a-z0-9-]{0,62}")
        _matches(p["report_sha256"], r"[0-9a-f]{64}")
        timestamp(p["view_updated_at"])
        timestamp(p["workbook_updated_at"])


def _count(value):
    require(type(value) in {int, float} and math.isfinite(value)
            and 0 <= value <= 10**15 and int(value) == value)
    return int(value)


def _provider_time(value):
    _matches(value, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    require(parsed <= datetime.now(timezone.utc))
    return parsed


def _posthog_query(raw, grant):
    # Hash the original definition, including harmless UI fields; any edit needs review.
    require(type(raw) is dict)
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    require(digest == grant.parameters["definition_sha256"])
    query = raw
    if query.get("kind") == "InsightVizNode":
        require(set(query) <= {"kind", "source", "version"})
        query = query.get("source")
    require(type(query) is dict)
    require(set(query) <= {"kind", "series", "dateRange", "interval", "properties",
                           "filterTestAccounts", "trendsFilter", "version"})
    require(query.get("kind") == "TrendsQuery" and query.get("interval") == "day")
    require(query.get("properties", []) == [])
    # Project-level test-account filters are mutable outside the saved definition.
    require(query.get("filterTestAccounts", False) is False)
    require(query.get("trendsFilter", {}) in ({}, {"display": "ActionsLineGraph"}))
    dates = query.get("dateRange")
    require(type(dates) is dict and set(dates) <= {"date_from", "date_to", "explicitDate"})
    require(dates.get("date_from") == grant.start_date and dates.get("date_to") == grant.end_date)
    require(dates.get("explicitDate", True) is True)
    series = items(query.get("series"), 1, empty=False)
    require(len(series) == 1)
    event = fields(series[0], {"kind", "event", "math"})
    require(event["kind"] == "EventsNode" and event["math"] in {"total", "dau"})
    _matches(event["event"], r"[A-Za-z0-9_$.:/-]{1,128}")
    return event["math"]


def _posthog(grant, start, end, request, secret):
    p = grant.parameters
    origin = {"us": "https://us.posthog.com", "eu": "https://eu.posthog.com"}[p["region"]]
    base = f"{origin}/api/projects/{grant.resource_id}"
    project_url = f"{origin}/api/organizations/{grant.account_id}/projects/{grant.resource_id}/"
    headers = {"Authorization": f"Bearer {secret()}"}

    def project():
        raw = request("GET", project_url, headers=headers)
        require(type(raw.get("id")) is int and str(raw["id"]) == grant.resource_id)
        require(raw.get("organization") == grant.account_id and raw.get("timezone") == grant.time_zone)
        require(raw.get("is_pending_deletion", False) is False)

    def report():
        raw = request("GET", f"{base}/insights/{p['report_id']}/", headers=headers,
                      params={"refresh": "force_cache"})
        require(type(raw.get("id")) is int and str(raw["id"]) == p["report_id"])
        require(raw.get("deleted") is False and raw.get("hasMore", False) is False)
        require(raw.get("timezone", grant.time_zone) == grant.time_zone)
        aggregation = _posthog_query(raw.get("query"), grant)
        _provider_time(raw.get("last_refresh"))
        return raw, aggregation

    project()
    raw, aggregation = report()
    results = items(raw.get("result"), 1, empty=False)
    require(len(results) == 1 and type(results[0]) is dict)
    data = items(results[0].get("data"), 366)
    days = items(results[0].get("days"), 366)
    require(len(data) == len(days))
    rows, seen = [], set()
    for day, value in zip(days, data):
        parsed = calendar_date(day)
        require(calendar_date(grant.start_date) <= parsed <= calendar_date(grant.end_date)
                and day not in seen)
        seen.add(day)
        count = _count(value)
        if start <= parsed <= end:
            rows.append({"date": day, "value": count})
    require(len(days) == (calendar_date(grant.end_date) - calendar_date(grant.start_date)).days + 1)
    after, _ = report()
    require(after.get("last_refresh") == raw.get("last_refresh")
            and after.get("result") == raw.get("result"))
    project()
    return {"aggregation": aggregation, "computed_at": _provider_time(raw["last_refresh"]).isoformat(),
            "freshness": "cached_saved_report; not_realtime", "rows": sorted(rows, key=lambda r: r["date"])}


def _tableau(grant, start, end, request, secret):
    p = grant.parameters
    base = f"https://{p['pod']}.online.tableau.com/api/3.27/sites/{grant.account_id}"
    headers = {"X-Tableau-Auth": secret()}

    def revision():
        view = request("GET", f"{base}/views/{grant.resource_id}", headers=headers).get("view")
        require(type(view) is dict and view.get("id") == grant.resource_id)
        require(type(view.get("workbook")) is dict and view["workbook"].get("id") == p["workbook_id"])
        require(view.get("updatedAt") == p["view_updated_at"])
        workbook = request("GET", f"{base}/workbooks/{p['workbook_id']}", headers=headers).get("workbook")
        require(type(workbook) is dict and workbook.get("id") == p["workbook_id"])
        require(workbook.get("updatedAt") == p["workbook_updated_at"])

    revision()
    text = request("GET", f"{base}/views/{grant.resource_id}/data", headers=headers,
                   params={"maxAge": "1"}, response_type="text")
    require(isinstance(text, str) and hashlib.sha256(text.encode("utf-8")).hexdigest() == p["report_sha256"])
    reader = csv.reader(io.StringIO(text), strict=True)
    require(next(reader, None) == ["date", "value"])
    rows, seen = [], set()
    for row in reader:
        require(len(row) == 2 and len(seen) < 366)
        day = calendar_date(row[0])
        require(calendar_date(grant.start_date) <= day <= calendar_date(grant.end_date) and row[0] not in seen)
        _matches(row[1], r"(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,6})?")
        seen.add(row[0])
        value = float(row[1])
        require(math.isfinite(value) and Decimal(str(value)) == Decimal(row[1]))
        if start <= day <= end:
            rows.append({"date": row[0], "value": value})
    require(len(seen) == (calendar_date(grant.end_date) - calendar_date(grant.start_date)).days + 1)
    revision()
    return {"freshness": "approved_content_snapshot; not_realtime", "rows": sorted(rows, key=lambda r: r["date"])}


def execute(grant, start, end, request, secret):
    return {"scoped_posthog_saved_insight": _posthog,
            "scoped_tableau_saved_view": _tableau}[grant.operation](grant, start, end, request, secret)
