"""Verified Codewords customer totals through a fixed Google Ads direct route."""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from gateway.client_context_analytics import calendar_date
from gateway.client_context_google_stripe import _integer
from gateway.client_context_policy import ContextDenied, fields, items, require

CUSTOMER = "9855813343"
CURRENCY = "GBP"
TIME_ZONE = "America/Los_Angeles"
URL = "https://googleads.googleapis.com/v24/customers/9855813343/googleAds:search"
DEVELOPER_TOKEN_FILE = Path("/Users/adityasheth/.google-ads.yaml")
IDENTITY_FIELDS = "customer.id, customer.currency_code, customer.time_zone, customer.manager"


def developer_token(guard):
    """Read one credential scalar; never inherit the SDK's manager/config fields."""
    guard()
    with DEVELOPER_TOKEN_FILE.open("rb") as stream:
        content = stream.read(65537)
    guard()
    require(len(content) <= 65536)
    try:
        # Compose a safe syntax tree rather than constructing unrelated config.
        root = yaml.compose(content, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        raise ContextDenied("invalid Google Ads credential document") from None
    require(isinstance(root, yaml.MappingNode))
    tokens = [value for key, value in root.value
              if isinstance(key, yaml.ScalarNode) and key.tag == "tag:yaml.org,2002:str"
              and key.value == "developer_token"]
    require(len(tokens) == 1)
    token = tokens[0]
    require(isinstance(token, yaml.ScalarNode) and token.tag == "tag:yaml.org,2002:str")
    value = token.value
    require(type(value) is str and 0 < len(value) <= 8192
            and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", value))
    guard()
    return value


def _customer(raw):
    fields(raw, {"resourceName", "id", "currencyCode", "timeZone", "manager"})
    require(raw["id"] == CUSTOMER and raw["resourceName"] == "customers/" + CUSTOMER
            and raw["currencyCode"] == CURRENCY and raw["timeZone"] == TIME_ZONE
            and raw["manager"] is False)


def report(start, end, request, secret, operation="daily"):
    """Only dates vary; customer, endpoint, headers and GAQL remain server-owned."""
    require(type(operation) is str and operation in OPERATIONS)
    require(type(start) is date and type(end) is date)
    require(0 <= (end - start).days < 31 and end < datetime.now(ZoneInfo(TIME_ZONE)).date())
    headers = {"Authorization": "Bearer " + secret(), "developer-token": secret("DEVELOPER_TOKEN")}

    def query(text):
        raw = request("POST", URL, headers=headers, json={"query": text})
        require(type(raw) is dict and set(raw) <= {
            "results", "fieldMask", "totalResultsCount", "queryResourceConsumption"})
        rows = items(raw.get("results", []), 31)
        if "totalResultsCount" in raw:
            require(_integer(raw["totalResultsCount"]) == len(rows))
        return rows

    def identity():
        rows = query("SELECT " + IDENTITY_FIELDS + " FROM customer LIMIT 1")
        require(len(rows) == 1)
        _customer(fields(rows[0], {"customer"})["customer"])

    identity()
    if operation != "daily":
        result = _campaign_report(start, end, request, headers, operation)
        identity()
        return result
    rows = query("SELECT " + IDENTITY_FIELDS + ", segments.date, metrics.impressions, "
                 "metrics.clicks, metrics.cost_micros FROM customer "
                 f"WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}' "
                 "ORDER BY segments.date LIMIT 31")
    result, seen = [], set()
    for row in rows:
        fields(row, {"customer", "segments", "metrics"})
        _customer(row["customer"])
        day = calendar_date(fields(row["segments"], {"date"})["date"])
        require(start <= day <= end and day not in seen)
        seen.add(day)
        metrics = row["metrics"]
        require(type(metrics) is dict and set(metrics) <= {"impressions", "clicks", "costMicros"})
        result.append({"date": day.isoformat(), "impressions": _integer(metrics.get("impressions", "0")),
                       "clicks": _integer(metrics.get("clicks", "0")),
                       "cost_micros": _integer(metrics.get("costMicros", "0"))})
    identity()
    return {"rows": sorted(result, key=lambda row: row["date"]), "currency": CURRENCY,
            "pagination_complete": True, "incomplete_reason": None,
            "coverage": "daily_customer_totals; cost_in_micros; historical_metrics_can_be_revised"}


OPERATIONS = ("daily", "campaign_reports", "campaign_config")
CAMPAIGN_LIMIT = 20
_CAMPAIGN_FIELDS = {
    "campaign_reports": "campaign.id, metrics.impressions, metrics.clicks, metrics.cost_micros",
    "campaign_config": "campaign.id, campaign.status, campaign.advertising_channel_type",
}
_CHANNELS = frozenset({"UNSPECIFIED", "UNKNOWN", "SEARCH", "DISPLAY", "SHOPPING", "HOTEL",
                      "VIDEO", "MULTI_CHANNEL", "LOCAL", "SMART", "PERFORMANCE_MAX",
                      "LOCAL_SERVICES", "DISCOVERY", "TRAVEL", "DEMAND_GEN"})


def _campaign_report(start, end, request, headers, operation):
    suffix = (f" WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
              if operation == "campaign_reports" else "")
    query = ("SELECT " + IDENTITY_FIELDS + ", " + _CAMPAIGN_FIELDS[operation]
             + " FROM campaign" + suffix + " ORDER BY campaign.id LIMIT 21")
    raw = request("POST", URL, headers=headers, json={"query": query})
    require(type(raw) is dict and set(raw) <= {
        "results", "fieldMask", "totalResultsCount", "queryResourceConsumption"})
    rows = items(raw.get("results", []), CAMPAIGN_LIMIT + 1)
    total = _integer(raw["totalResultsCount"]) if "totalResultsCount" in raw else len(rows)
    # Google reports matching rows before GAQL LIMIT, when the count is requested.
    require(total >= len(rows))
    result, seen = [], set()
    for row in rows:
        expected = {"customer", "campaign"} | ({"metrics"} if operation == "campaign_reports" else set())
        fields(row, expected)
        _customer(row["customer"])
        campaign = row["campaign"]
        fields(campaign, {"resourceName", "id"} | (
            {"status", "advertisingChannelType"} if operation == "campaign_config" else set()))
        campaign_id = campaign["id"]
        require(type(campaign_id) is str and re.fullmatch(r"[1-9][0-9]{0,19}", campaign_id)
                and campaign_id not in seen
                and campaign["resourceName"] == f"customers/{CUSTOMER}/campaigns/{campaign_id}")
        seen.add(campaign_id)
        normalized = {"campaign_id": campaign_id}
        if operation == "campaign_reports":
            metrics = row["metrics"]
            require(type(metrics) is dict and set(metrics) <= {"impressions", "clicks", "costMicros"})
            normalized.update({"impressions": _integer(metrics.get("impressions", "0")),
                               "clicks": _integer(metrics.get("clicks", "0")),
                               "cost_micros": _integer(metrics.get("costMicros", "0"))})
        else:
            require(type(campaign["status"]) is str
                    and campaign["status"] in {"ENABLED", "PAUSED", "REMOVED", "UNKNOWN", "UNSPECIFIED"}
                    and type(campaign["advertisingChannelType"]) is str
                    and campaign["advertisingChannelType"] in _CHANNELS)
            normalized.update(status=campaign["status"], channel=campaign["advertisingChannelType"])
        result.append(normalized)
    complete = len(rows) <= CAMPAIGN_LIMIT and total == len(rows)
    return {"rows": result[:CAMPAIGN_LIMIT], "currency": CURRENCY,
            "pagination_complete": complete, "rows_returned": min(len(rows), CAMPAIGN_LIMIT),
            "row_limit": CAMPAIGN_LIMIT, "incomplete_reason": None if complete else "row_limit_reached",
            "coverage": operation + "; current configuration; reports aggregate requested dates; no account totals"}
