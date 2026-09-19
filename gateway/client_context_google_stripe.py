"""Fixed Google/Stripe observations with explicit provider identity checks."""

from __future__ import annotations

import math
import re
from urllib.parse import quote, urlsplit

from gateway.client_context_analytics import calendar_date
from gateway.client_context_policy import fields, items, require

SEARCH_CONSOLE = "scoped_search_console_daily_report"
GOOGLE_ADS = "scoped_google_ads_daily_report"
STRIPE = "scoped_stripe_balance"
OPERATIONS = {
    SEARCH_CONSOLE: "Read finalized daily web search clicks, impressions, CTR and position for the approved site; no query or page text.",
    GOOGLE_ADS: "Read daily account impressions, clicks and cost in micros using a fixed Google Ads query; no arbitrary GAQL.",
    STRIPE: "Read the current available and pending Stripe balance in currency minor units; a current snapshot, not a date-range revenue or MRR report.",
}


def _domain(value):
    require(isinstance(value, str) and value == value.lower())
    encoded = value.encode("idna").decode("ascii")
    require(len(encoded) <= 253 and "." in encoded)
    require(all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                for part in encoded.split(".")))
    # Validate A-labels without changing the exact provider property identifier.
    encoded.encode("ascii").decode("idna")


def validate_parameters(entry):
    operation = entry["operation"]
    account, resource = entry["account_id"], entry["resource_id"]
    require(isinstance(account, str) and isinstance(resource, str))
    if operation == GOOGLE_ADS:
        require(re.fullmatch(r"[0-9]{10}", account) and re.fullmatch(r"[0-9]{10}", resource))
        fields(entry["parameters"], set())
    elif operation == STRIPE:
        require(account == resource and re.fullmatch(r"acct_[A-Za-z0-9]{1,64}", account))
        fields(entry["parameters"], {"livemode"})
        require(type(entry["parameters"]["livemode"]) is bool)
    elif operation == SEARCH_CONSOLE:
        require(account == resource and len(resource) <= 512)
        if resource.startswith("sc-domain:"):
            _domain(resource[10:])
        else:
            require(not any(ord(c) < 33 or ord(c) == 127 for c in resource))
            parsed = urlsplit(resource)
            require(parsed.scheme in {"http", "https"} and parsed.hostname is not None)
            _domain(parsed.hostname)
            require(parsed.netloc == parsed.hostname and not parsed.query and not parsed.fragment)
            require(re.fullmatch(r"/(?:[A-Za-z0-9._~!$&'()*+,;=:@/-]|%[0-9A-Fa-f]{2})*", parsed.path))
        require(entry["time_zone"] == "America/Los_Angeles")
        fields(entry["parameters"], set())
    else:
        require(False)


def _number(value, *, maximum=10**15, signed=False):
    require(type(value) in (int, float) and math.isfinite(value))
    require((-maximum if signed else 0) <= value <= maximum)
    return value


def _integer(value):
    # Google protobuf JSON encodes int64 values as decimal strings.
    require(isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,14}", value))
    return int(value)


def _search_console(grant, start, end, request, secret):
    headers = {"Authorization": f"Bearer {secret()}"}
    url = "https://www.googleapis.com/webmasters/v3/sites/" + quote(grant.resource_id, safe="")

    def identity():
        raw = request("GET", url, headers=headers)
        require(raw.get("siteUrl") == grant.resource_id)
        require(raw.get("permissionLevel") in {"siteOwner", "siteFullUser", "siteRestrictedUser"})

    identity()
    raw = request("POST", url + "/searchAnalytics/query", headers=headers, json={
        "startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["date"],
        "type": "web", "dataState": "final", "aggregationType": "byProperty", "rowLimit": 31,
    })
    require(isinstance(raw, dict) and set(raw) <= {"rows", "responseAggregationType"})
    require(raw.get("responseAggregationType") == "byProperty")
    seen, result = set(), []
    for row in items(raw.get("rows", []), 31):
        fields(row, {"keys", "clicks", "impressions", "ctr", "position"})
        keys = items(row["keys"], 1, empty=False)
        day = calendar_date(keys[0])
        require(start <= day <= end and day not in seen)
        seen.add(day)
        clicks, impressions = _number(row["clicks"]), _number(row["impressions"])
        require(clicks == int(clicks) and impressions == int(impressions) and clicks <= impressions)
        result.append({"date": day.isoformat(), "clicks": int(clicks), "impressions": int(impressions),
                       "ctr": _number(row["ctr"], maximum=1), "position": _number(row["position"])})
    identity()
    return {"rows": sorted(result, key=lambda row: row["date"]),
            "coverage": "finalized_web_search_by_property; omitted_dates_have_no_returned_data"}


def _google_ads(grant, start, end, request, secret):
    headers = {"Authorization": f"Bearer {secret()}", "developer-token": secret("DEVELOPER_TOKEN"),
               "login-customer-id": grant.resource_id}
    url = f"https://googleads.googleapis.com/v25/customers/{grant.account_id}/googleAds:search"

    def query(text):
        raw = request("POST", url, headers=headers, json={"query": text})
        require(isinstance(raw, dict) and set(raw) <= {"results", "fieldMask", "totalResultsCount", "queryResourceConsumption"})
        return items(raw.get("results", []), 31)

    def customer(raw):
        require(isinstance(raw, dict) and set(raw) <= {"resourceName", "id", "currencyCode", "timeZone"})
        require(raw.get("id") == grant.account_id and raw.get("resourceName") == f"customers/{grant.account_id}")
        require(raw.get("timeZone") == grant.time_zone)
        require(isinstance(raw.get("currencyCode"), str) and re.fullmatch(r"[A-Z]{3}", raw["currencyCode"]))
        return raw["currencyCode"]

    identity_query = "SELECT customer.id, customer.currency_code, customer.time_zone FROM customer LIMIT 1"

    def identity():
        rows = query(identity_query)
        require(len(rows) == 1)
        fields(rows[0], {"customer"})
        return customer(rows[0]["customer"])

    currency = identity()
    rows = query("SELECT customer.id, customer.currency_code, customer.time_zone, segments.date, "
                 "metrics.impressions, metrics.clicks, metrics.cost_micros FROM customer "
                 f"WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}' "
                 "ORDER BY segments.date LIMIT 31")
    seen, result = set(), []
    for row in rows:
        fields(row, {"customer", "segments", "metrics"})
        require(customer(row["customer"]) == currency)
        day = calendar_date(fields(row["segments"], {"date"})["date"])
        require(start <= day <= end and day not in seen)
        seen.add(day)
        metrics = row["metrics"]
        require(isinstance(metrics, dict) and set(metrics) <= {"impressions", "clicks", "costMicros"})
        result.append({"date": day.isoformat(), "impressions": _integer(metrics.get("impressions", "0")),
                       "clicks": _integer(metrics.get("clicks", "0")), "cost_micros": _integer(metrics.get("costMicros", "0"))})
    require(identity() == currency)
    return {"rows": sorted(result, key=lambda row: row["date"]), "currency": currency,
            "coverage": "daily_customer_totals; cost_in_micros; historical_metrics_can_be_revised"}


def _stripe(grant, start, end, request, secret):
    headers = {"Authorization": f"Bearer {secret()}", "Stripe-Account": grant.account_id,
               "Stripe-Version": "2024-06-20"}

    def identity():
        raw = request("GET", "https://api.stripe.com/v1/account", headers=headers)
        require(raw.get("id") == grant.account_id and raw.get("object") == "account")

    identity()
    raw = request("GET", "https://api.stripe.com/v1/balance", headers=headers)
    require(raw.get("object") == "balance" and type(raw.get("livemode")) is bool)
    require(raw["livemode"] == grant.parameters["livemode"])
    result = {}
    for name in ("available", "pending"):
        seen, values = set(), []
        for row in items(raw.get(name), 32):
            require(isinstance(row, dict))
            currency, amount = row.get("currency"), row.get("amount")
            require(isinstance(currency, str) and re.fullmatch(r"[a-z]{3}", currency) and currency not in seen)
            seen.add(currency)
            require(type(amount) is int)
            _number(amount, signed=True)
            values.append({"currency": currency, "amount_minor_units": amount})
        result[name] = sorted(values, key=lambda row: row["currency"])
    identity()
    return {**result, "livemode": raw["livemode"],
            "coverage": "current_balance_snapshot; dates_do_not_filter_balance; not_revenue_or_mrr"}


_HANDLERS = {SEARCH_CONSOLE: _search_console, GOOGLE_ADS: _google_ads, STRIPE: _stripe}


def execute(grant, start, end, request, secret):
    return _HANDLERS[grant.operation](grant, start, end, request, secret)
