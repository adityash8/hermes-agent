"""Fixed Codewords reads, independent of the general tool/grant registries."""

from __future__ import annotations

import json
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from gateway import client_context_analytics as ga
from gateway import client_context_codewords_oauth as oauth
from gateway import client_context_codewords_dedicated as dedicated
from gateway import client_context_reads as transport
from gateway.client_context_policy import (
    ContextDenied, _reject_json_constant, _unique_object, fields, items, require,
)

MAX_OUTPUT = 8192
PREFIX = "codewords_"
NAMES = tuple(PREFIX + name + "_read" for name in (
    "posthog", "gsc", "ga4", "gtm", "google_ads", "meta_ads", "postgres", "stripe",
    "linear", "customerio"))
OPERATIONS = {
    NAMES[0]: ("pageviews", "event_trends", "unique_users", "cohort_counts"),
    NAMES[1]: ("daily", "queries", "pages"),
    NAMES[2]: ("daily", "acquisition", "device"),
    NAMES[3]: ("inventory", "configuration"),
    NAMES[4]: ("daily", "campaign_reports", "campaign_config"),
    NAMES[5]: ("daily", "campaign_reports", "adset_reports", "campaign_config", "adset_config"),
    NAMES[6]: ("inventory",),
    NAMES[7]: ("subscriptions",),
    **dedicated.OPS,
}
UNDATED = (NAMES[3], NAMES[6], NAMES[8], NAMES[9])
STRIPE_ACCOUNT = "acct_1Oyuv1Ka5VSTfRCx"
STRIPE_ENV_FILE = Path("/Users/adityasheth/.hermes/secrets/codewords-stripe.env")
STRIPE_PAGE_SIZE = 100  # Stripe-only transport caps raw pages at 1 MiB.
STRIPE_MAX_PAGES = 100
STRIPE_SCAN_SECONDS = 60.0
STRIPE_STATUSES = ("active", "trialing", "past_due", "unpaid", "paused",
                   "incomplete", "incomplete_expired", "canceled")


class ScanDeadline(Exception):
    """A bounded scan can report incomplete without releasing partial totals."""


class ReadReceipt(str):
    """Carry a credential-only recheck until the channel answer is delivered."""

    def __new__(cls, value, validate_credentials):
        result = super().__new__(cls, value)
        result.validate_credentials = validate_credentials
        return result


@dataclass(frozen=True)
class Binding:
    """Operator-owned identities and secret references; never model arguments."""

    name: str
    resources: tuple[tuple[str, str], ...] = ()
    credentials: tuple[tuple[str, str], ...] = ()
    time_zone: str = "UTC"
    blocker: str = "missing_verified_resource_and_credential_binding"


# These are the exact identities from integration-probes.json, not discovery hints.
# Unknown resource IDs stay absent. Editing this tuple invalidates in-flight reads.
_BINDINGS = (
    Binding(NAMES[0], (("project_id", "23659"),
                     ("organization_id", "018fbef7-f7af-0000-41ce-60cd82be5848"),
                     ("origin", "https://eu.posthog.com")),
            (("env_file", "/Users/adityasheth/.hermes/secrets/codewords-posthog.env"),
             ("access_token_key", "POSTHOG_PERSONAL_API_KEY")), blocker=""),
    Binding(NAMES[1], (("account_id", "sc-domain:codewords.ai"),
                     ("resource_id", "sc-domain:codewords.ai")),
            (("connection", "composio_gsc_ez_money"),),
            time_zone="America/Los_Angeles", blocker=""),
    Binding(NAMES[2], (("account_id", "351626649"), ("property_id", "511306748"), ("currency", "GBP")),
            (("oauth_file", "/Users/adityasheth/.hermes/secrets/google_marketing_oauth_token.json"),),
            time_zone="Etc/GMT", blocker=""),
    Binding(NAMES[3], (("account_id", "6234246789"), ("web_container", "186967704"),
                     ("web_public_id", "GTM-KFMB8N6L"), ("server_container", "233991397"),
                     ("server_public_id", "GTM-T6KD93DR")),
            (("oauth_file", "/Users/adityasheth/.hermes/secrets/gtm_oauth_token.json"),), blocker=""),
    Binding(NAMES[4], (("account_id", "9855813343"), ("currency", "GBP"), ("api_version", "v24")),
            (("oauth_file", "/Users/adityasheth/.hermes/secrets/google_marketing_oauth_token.json"),
             ("developer_token_file", "/Users/adityasheth/.google-ads.yaml")),
            time_zone="America/Los_Angeles", blocker=""),
    Binding(NAMES[5], (("account_id", "1143741027937235"), ("resource_id", "2028463037640474"),
                     ("currency", "GBP")),
            (("connection", "pipeboard_meta_ads"),), time_zone="Europe/London", blocker=""),
    Binding(NAMES[7], (("account_id", STRIPE_ACCOUNT), ("livemode", "true")),
            (("env_file", str(STRIPE_ENV_FILE)),
             ("access_token_key", "CODEWORDS_STRIPE_RESTRICTED_KEY")), blocker=""),
    Binding(NAMES[6], credentials=(("keychain_service", "jarvis.codewords.postgres.readonly"),
                                  ("keychain_account", "codewords_workflow_readonly")),
            blocker="database_tls_identity_and_privilege_binding_unverified"),
    Binding(NAMES[8], dedicated.RESOURCES[NAMES[8]], dedicated.CREDENTIALS[NAMES[8]], blocker=""),
    Binding(NAMES[9], dedicated.RESOURCES[NAMES[9]], dedicated.CREDENTIALS[NAMES[9]], blocker=""),
)


def bindings():
    return _BINDINGS


def schemas():
    descriptions = (
        "Read fixed Codewords PostHog pageviews, all-event trends, unique users or current cached cohort counts. No raw events or people.",
        "Read Codewords finalized Search Console daily, query or page metrics with explicit coverage limits.",
        "Read fixed Codewords GA4 daily, acquisition or device reports; no custom query or dimensions.",
        "Read counts or sanitized tag configuration in fixed Codewords production GTM containers; no names, parameters, HTML or secrets.",
        "Read fixed Codewords Google Ads daily totals, campaign reports or sanitized campaign configuration.",
        "Read fixed Codewords Meta Ads daily totals, campaign/adset reports or sanitized configuration.",
        "Codewords PostgreSQL is unavailable until identity, TLS, readonly privileges and fixed business aggregates are verified. No SQL or row data.",
        "Read Codewords Stripe subscription creations, classified canceled endings and current status counts. "
        "Optionally compare an earlier equal-length UTC window using both comparison_start_date and comparison_end_date. "
        "Returns computed count deltas and percent changes, null for a zero baseline. "
        f"Bounded full scan up to {STRIPE_PAGE_SIZE * STRIPE_MAX_PAGES} subscriptions and a {STRIPE_SCAN_SECONDS:g}-second scan budget; incomplete scans withhold totals. "
        "Current objects are not historical MRR or a customer churn denominator; no churn rate.",
        "Read verified Codewords Linear team inventory or bounded Stripe-titled issue summaries. Safe titles, dates and links only; no descriptions, comments or users. No changes or drafts.",
        "Read Codewords Customer.io campaigns, segments or workspace summaries. Both verified workspaces are in scope; default workspace attribution is unverified. No people, message content, actions or changes.",
    )
    result = []
    for name, description in zip(NAMES, descriptions):
        properties = {"operation": {"type": "string", "enum": list(OPERATIONS[name])}}
        required = ["operation"]
        if name not in UNDATED:
            properties.update({key: {"type": "string", "format": "date"} for key in ("start_date", "end_date")})
            if name == NAMES[7]:
                properties.update({key: {"type": "string", "format": "date"}
                                   for key in ("comparison_start_date", "comparison_end_date")})
            required = ["start_date", "end_date"]
            description += " At most 31 completed days. Missing bindings return unavailable."
            description += " Default operation: " + OPERATIONS[name][0] + "."
        result.append({"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}})
    return result


def validate_call(name, arguments):
    """Pure validation is usable for an entire provider batch before any I/O."""
    try:
        require(type(name) is str and name in NAMES)
        require(type(arguments) is str and len(arguments.encode()) <= 1024)
        args = json.loads(arguments, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
        if name in UNDATED:
            fields(args, {"operation"})
        else:
            comparison_keys = {"comparison_start_date", "comparison_end_date"} if name == NAMES[7] else set()
            require(type(args) is dict and {"start_date", "end_date"} <= set(args)
                    and set(args) <= {"start_date", "end_date", "operation"} | comparison_keys)
            start, end = ga.calendar_date(args["start_date"]), ga.calendar_date(args["end_date"])
            binding = next(b for b in bindings() if b.name == name)
            require(0 <= (end - start).days < 31
                    and end < datetime.now(ZoneInfo(binding.time_zone)).date())
            if comparison_keys & set(args):
                require(comparison_keys <= set(args))
                before_start = ga.calendar_date(args["comparison_start_date"])
                before_end = ga.calendar_date(args["comparison_end_date"])
                require(before_end - before_start == end - start and before_end < start)
        require(type(args.get("operation", OPERATIONS[name][0])) is str
                and args.get("operation", OPERATIONS[name][0]) in OPERATIONS[name])
        return args
    except Exception:
        raise ContextDenied("invalid Codewords read request") from None


def _json_file(path):
    # Credential file names come only from an immutable server-owned binding.
    with Path(path).open("rb") as stream:
        value = stream.read(65537)
    require(len(value) <= 65536)
    result = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
    require(type(result) is dict)
    return result


def _token(value):
    require(type(value) is str and 0 < len(value) <= 8192
            and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", value))
    return value


def _secret(binding, suffix, guard):
    refs = dict(binding.credentials)
    guard()
    if binding.name == NAMES[4] and suffix == "DEVELOPER_TOKEN":
        from gateway.client_context_codewords_ads import DEVELOPER_TOKEN_FILE, developer_token

        require(refs.get("developer_token_file") == str(DEVELOPER_TOKEN_FILE))
        value = developer_token(guard)
    elif "env_file" in refs:
        from dotenv import dotenv_values

        # Disable dotenv variable interpolation: no default-profile secret fallback.
        with Path(refs["env_file"]).open("r", encoding="utf-8") as stream:
            import io

            content = stream.read(65537)
            require(len(content) <= 65536)
            values = dotenv_values(stream=io.StringIO(content), interpolate=False)
        value = values.get(refs[suffix.lower() + "_key"])
    elif "json_file" in refs:
        require(binding.name == NAMES[9] and suffix == "ACCESS_TOKEN")
        raw = _json_file(refs["json_file"])
        require(raw.get("region") == "eu" and raw.get("base_url") == dedicated.CUSTOMERIO_ORIGIN)
        value = raw.get(refs["access_token_key"])
    elif "oauth_file" in refs:
        require(suffix == "ACCESS_TOKEN")
        raw = _json_file(refs["oauth_file"])
        value = oauth.access_token(raw, guard)
    else:
        raise ContextDenied("dedicated credential reference required")
    guard()
    return _token(value)


def _credential_snapshot(binding):
    """Seal only fixed credential files, never the ambient profile or environment."""
    result = []
    try:
        if binding.credentials == (("connection", "composio_gsc_ez_money"),):
            from gateway.client_context_codewords_gsc import _credential

            return _credential()[1]
        if binding.credentials == (("connection", "pipeboard_meta_ads"),):
            from gateway.client_context_codewords_meta import _credential

            return _credential()[1]
        for key, path in binding.credentials:
            if key in {"env_file", "oauth_file", "developer_token_file", "json_file"}:
                with Path(path).open("rb") as stream:
                    content = stream.read(65537)
                require(len(content) <= 65536)
                result.append((key, hashlib.sha256(content).digest()))
        require(bool(result))
        return tuple(result)
    except Exception:
        raise ContextDenied("Codewords credential unavailable or revoked") from None


def _posthog(binding, start, end, request, secret):
    r = dict(binding.resources)
    require(r["origin"] in {"https://eu.posthog.com", "https://us.posthog.com"})
    require(re.fullmatch(r"[1-9][0-9]{0,19}", r["project_id"]))
    url = r["origin"] + "/api/projects/" + r["project_id"] + "/"
    headers = {"Authorization": "Bearer " + secret()}

    def identity():
        raw = request("GET", url, headers=headers)
        require(type(raw.get("id")) is int and str(raw["id"]) == r["project_id"])
        require(raw.get("organization") == r["organization_id"]
                and raw.get("timezone") == binding.time_zone
                and raw.get("is_pending_deletion", False) is False)

    identity()
    raw = request("POST", url + "query/", headers=headers, json={"query": {
        "kind": "TrendsQuery", "series": [{"kind": "EventsNode", "event": "$pageview", "math": "total"}],
        "interval": "day", "dateRange": {"date_from": start.isoformat(), "date_to": end.isoformat(),
                                             "explicitDate": True},
        "properties": [], "filterTestAccounts": False,
    }})
    require(not raw.get("error") and not raw.get("hasMore", False))
    results = items(raw.get("results"), 1, empty=False)
    require(len(results) == 1 and type(results[0]) is dict)
    days, data = items(results[0].get("days"), 31), items(results[0].get("data"), 31)
    require(len(days) == len(data) == (end - start).days + 1)
    rows, seen = [], set()
    for day, value in zip(days, data):
        parsed = ga.calendar_date(day)
        require(start <= parsed <= end and day not in seen)
        require(type(value) in (int, float) and 0 <= value <= 10**15 and value == int(value))
        seen.add(day)
        rows.append({"date": day, "pageviews": int(value)})
    identity()
    return {"project_id": r["project_id"], "rows": sorted(rows, key=lambda row: row["date"]),
            "pagination_complete": True, "incomplete_reason": None}


def _ga4(binding, start, end, request, secret):
    r = dict(binding.resources)
    require(all(re.fullmatch(r"[1-9][0-9]{0,19}", r[k]) for k in ("account_id", "property_id")))
    grant = SimpleNamespace(property_id=r["property_id"], account_id=r["account_id"], time_zone=binding.time_zone)
    headers = {"Authorization": "Bearer " + secret()}
    url = "https://analyticsadmin.googleapis.com/v1beta/properties/" + grant.property_id
    def identity():
        metadata = request("GET", url, headers=headers)
        ga._property(metadata, grant)
        require(metadata.get("currencyCode") == r["currency"])

    identity()
    raw = request("POST", "https://analyticsdata.googleapis.com/v1beta/properties/" + grant.property_id + ":runReport",
        headers=headers, json={"dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "dimensions": [{"name": "date"}], "metrics": [{"name": metric} for metric in ga.METRICS],
            "limit": "31", "keepEmptyRows": False})
    require(raw.get("metadata", {}).get("currencyCode") == r["currency"])
    rows = ga._report(raw, grant, start, end)
    identity()
    return {"rows": rows, "pagination_complete": True, "incomplete_reason": None}


def _gtm(binding, _start, _end, request, secret):
    r = dict(binding.resources)
    require(re.fullmatch(r"[0-9]{1,20}", r["account_id"]))
    headers = {"Authorization": "Bearer " + secret()}
    # Count identities only. Never download tag HTML, names, parameters or links.
    mask = ("accountId,containerId,containerVersionId,path,deleted,"
            "container(accountId,containerId,publicId),"
            "tag(accountId,containerId),trigger(accountId,containerId),variable(accountId,containerId)")
    result = []
    for kind in ("web", "server"):
        container = r[kind + "_container"]
        require(re.fullmatch(r"[0-9]{1,20}", container))
        path = "accounts/" + r["account_id"] + "/containers/" + container
        url = "https://tagmanager.googleapis.com/tagmanager/v2/" + path + "/versions:live"

        def inventory():
            raw = request("GET", url, headers=headers, params={"fields": mask})
            require(raw.get("accountId") == r["account_id"] and raw.get("containerId") == container
                    and raw.get("deleted", False) is False)
            identity = raw.get("container", {})
            require(identity.get("accountId") == r["account_id"] and identity.get("containerId") == container
                    and identity.get("publicId") == r[kind + "_public_id"])
            version = raw.get("containerVersionId")
            require(type(version) is str and re.fullmatch(r"[0-9]{1,20}", version))
            require(raw.get("path") == path + "/versions/" + version)
            counts = {}
            for key in ("tag", "trigger", "variable"):
                values = items(raw.get(key, []), 500)
                require(all(type(value) is dict and value.get("accountId") == r["account_id"]
                            and value.get("containerId") == container for value in values))
                counts[key + "_count"] = len(values)
            return {"container": kind, "version": version, **counts}

        before = inventory()
        require(inventory() == before)
        result.append(before)
    return {"containers": result, "pagination_complete": True, "incomplete_reason": None}


def _ads(binding, start, end, request, secret, operation="daily"):
    from gateway import client_context_codewords_ads as ads

    require(dict(binding.resources) == {"account_id": ads.CUSTOMER, "currency": ads.CURRENCY, "api_version": "v24"}
            and binding.time_zone == ads.TIME_ZONE)
    return ads.report(start, end, request, secret, operation=operation)


def _stripe_ending_counts(reasons):
    return {
        "total": sum(reasons.values()),
        "voluntary": reasons["cancellation_requested"],
        "involuntary": reasons["payment_failed"],
        "other_or_unknown": reasons["payment_disputed"] + reasons["other_or_unknown"],
    }


def _stripe(binding, start, end, request, secret, *, comparison=None):
    resources = dict(binding.resources)
    require(resources == {"account_id": STRIPE_ACCOUNT, "livemode": "true"}
            and binding.time_zone == "UTC"
            and binding.credentials == (("env_file", str(STRIPE_ENV_FILE)),
                                        ("access_token_key", "CODEWORDS_STRIPE_RESTRICTED_KEY")))
    # This route never accepts a general/default-profile key, even from its file.
    key = secret()
    require(re.fullmatch(r"rk_live_[A-Za-z0-9]+", key))
    # The restricted key can read subscriptions without account-read permission.
    # Stripe enforces this operator-verified account on every request, including pagination.
    headers = {"Authorization": "Bearer " + key, "Stripe-Version": "2024-06-20",
               "Stripe-Account": STRIPE_ACCOUNT}
    origin = "https://api.stripe.com/v1/"
    windows = [(start, end)] + ([comparison] if comparison is not None else [])
    bounds = [(int(datetime.combine(first, datetime.min.time(), timezone.utc).timestamp()),
               int(datetime.combine(last + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()))
              for first, last in windows]
    reason_counts = [{reason: 0 for reason in ("cancellation_requested", "payment_failed",
                                              "payment_disputed", "other_or_unknown")} for _ in windows]
    lower, upper = bounds[0]
    observed_at = int(datetime.now(timezone.utc).timestamp())
    deadline = time.monotonic() + STRIPE_SCAN_SECONDS
    counts = {status: 0 for status in STRIPE_STATUSES}
    created_count = expired_count = scheduled_count = 0
    seen, cursor = set(), None
    complete = False
    incomplete_reason = "page_limit_reached"
    pages_read = 0
    for page in range(1, STRIPE_MAX_PAGES + 1):
        # Leave room for the next bounded HTTP request and final model response.
        # All pages share one scan cutoff; only a fully traversed list gets totals.
        if time.monotonic() >= deadline:
            incomplete_reason = "scan_time_budget_reached"
            break
        # Do not filter created by the requested range: older subscriptions can end in it.
        params = {"status": "all", "limit": STRIPE_PAGE_SIZE, "created[lte]": observed_at}
        if cursor is not None:
            params["starting_after"] = cursor
        try:
            raw = request("GET", origin + "subscriptions", headers=headers, params=params, deadline=deadline)
        except ScanDeadline:
            incomplete_reason = "scan_time_budget_reached"
            break
        pages_read += 1
        require(type(raw) is dict and raw.get("object") == "list"
                and raw.get("url") == "/v1/subscriptions" and type(raw.get("has_more")) is bool)
        rows = items(raw.get("data"), STRIPE_PAGE_SIZE)
        require(rows or raw["has_more"] is False)
        for row in rows:
            require(type(row) is dict and row.get("object") == "subscription"
                    and row.get("livemode") is True and row.get("test_clock") is None)
            sub_id, status = row.get("id"), row.get("status")
            require(type(sub_id) is str and re.fullmatch(r"sub_[A-Za-z0-9]{1,128}", sub_id)
                    and sub_id not in seen and type(status) is str and status in counts)
            created, ended = row.get("created"), row.get("ended_at")
            require(type(created) is int and 0 <= created <= observed_at and "ended_at" in row)
            terminal = status in {"canceled", "incomplete_expired"}
            require((terminal and type(ended) is int and created <= ended <= observed_at)
                    or (not terminal and ended is None))
            require(type(row.get("cancel_at_period_end")) is bool)
            seen.add(sub_id)
            cursor = sub_id
            counts[status] += 1
            created_count += int(lower <= created < upper)
            if status == "canceled":
                # Provider comments and unfamiliar reasons never enter the model context.
                details = row.get("cancellation_details")
                reason = details.get("reason") if type(details) is dict else None
                if type(reason) is not str or reason not in reason_counts[0]:
                    reason = "other_or_unknown"
                for (first, last), reasons in zip(bounds, reason_counts):
                    if first <= ended < last:
                        reasons[reason] += 1
            expired_count += int(status == "incomplete_expired" and lower <= ended < upper)
            scheduled_count += int(not terminal and row["cancel_at_period_end"])
        if time.monotonic() >= deadline:
            incomplete_reason = "scan_time_budget_reached"
            break
        if raw["has_more"] is False:
            complete = True
            break
    current = _stripe_ending_counts(reason_counts[0])
    comparison_result = None
    if complete and comparison is not None:
        before = _stripe_ending_counts(reason_counts[1])
        comparison_result = {
            "baseline": {"start_date": comparison[0].isoformat(), "end_date": comparison[1].isoformat(),
                         "cancellation_endings": before, "cancellation_reason_counts": reason_counts[1]},
            "current": {"start_date": start.isoformat(), "end_date": end.isoformat(),
                        "cancellation_endings": current},
            "delta": {key: current[key] - before[key] for key in current},
            "percent_change": {key: (current[key] - before[key]) / before[key] * 100
                               if before[key] else None for key in current},
            "percent_change_definition": "(current - baseline) / baseline * 100; null when baseline is zero. "
                                         "These are count changes, not customer churn rates or evidence of causality.",
        }
    return {
        "pagination_complete": complete,
        "incomplete_reason": None if complete else incomplete_reason,
        "pages_read": pages_read, "subscriptions_scanned": len(seen),
        "max_pages": STRIPE_MAX_PAGES, "page_size": STRIPE_PAGE_SIZE,
        "scan_time_budget_seconds": STRIPE_SCAN_SECONDS,
        "metrics": {
            "created_in_range": created_count,
            "canceled_subscriptions_ended_in_range": current["total"],
            "cancellation_endings": current,
            "cancellation_reason_counts": reason_counts[0],
            "incomplete_expired_subscriptions_ended_in_range": expired_count,
            "current_status_counts": counts,
            "current_cancel_at_period_end_count": scheduled_count,
        } if complete else None,
        "comparison": comparison_result,
        "classification": "Only status=canceled endings: cancellation_requested is voluntary; payment_failed is involuntary. "
                          "Payment disputes and missing or unrecognized reasons are other_or_unknown. "
                          "Incomplete_expired subscriptions are excluded from cancellation totals.",
        "coverage": "Live subscription objects created by scan start; dates include both UTC days. "
                    "Endings use ended_at; canceled_at may record an earlier cancellation request. "
                    "Status counts are current, not as of the requested dates.",
        "limitations": "Pagination completeness is not an atomic snapshot or an event history. "
                       "Current subscription data is not historical MRR or a customer churn denominator. "
                       "Subscription endings include trials and do not establish customer churn. No revenue or churn rate is computed.",
    }


_HANDLERS = {NAMES[0]: _posthog, NAMES[2]: _ga4,
             NAMES[3]: _gtm, NAMES[4]: _ads, NAMES[7]: _stripe}


def execute(name, arguments, check):
    args = validate_call(name, arguments)
    snapshot = bindings()
    binding = next(b for b in snapshot if b.name == name)
    operation = args.get("operation", OPERATIONS[name][0])
    credential_snapshot = None

    def validate_credentials():
        # Do not call the turn validator here: it retains this receipt and would recurse.
        if credential_snapshot is not None:
            require(_credential_snapshot(binding) == credential_snapshot)

    def guard():
        require(bindings() == snapshot)
        check()
        require(bindings() == snapshot)
        validate_credentials()

    guard()
    result = {"operation": name, "read_operation": operation, "status": "unavailable"}
    if binding.blocker:
        result["reason"] = binding.blocker
    else:
        try:
            start = ga.calendar_date(args["start_date"]) if "start_date" in args else None
            end = ga.calendar_date(args["end_date"]) if "end_date" in args else None
            if name == NAMES[6]:
                from gateway.client_context_codewords_db import inventory

                data = inventory(binding, guard)
            elif name == NAMES[1]:
                from gateway.client_context_codewords_gsc import SITE, report

                require(dict(binding.resources) == {"account_id": SITE, "resource_id": SITE}
                        and binding.credentials == (("connection", "composio_gsc_ez_money"),))
                credential_snapshot = _credential_snapshot(binding)
                data = (report(start, end, guard) if operation == "daily"
                        else report(start, end, guard, operation=operation))
            elif name == NAMES[5]:
                from gateway import client_context_codewords_meta as meta

                require(dict(binding.resources) == {"account_id": meta.ACCOUNT,
                        "resource_id": meta.BUSINESS, "currency": meta.CURRENCY}
                        and binding.time_zone == meta.TIME_ZONE
                        and binding.credentials == (("connection", "pipeboard_meta_ads"),))
                credential_snapshot = _credential_snapshot(binding)
                data = (meta.report(start, end, guard) if operation == "daily"
                        else meta.report(start, end, guard, operation=operation))
            else:
                with transport._http_client() as client:
                    def request(method, url, *, deadline=None, **kwargs):
                        def request_guard():
                            guard()
                            if deadline is not None and time.monotonic() >= deadline:
                                raise ScanDeadline()

                        if name == "codewords_stripe_read":
                            from gateway.client_context_codewords_stripe_http import request as stripe_request
                            return stripe_request(client, request_guard, method, url, **kwargs)
                        return transport._request(client, request_guard, method, url, **kwargs)

                    def secret(suffix="ACCESS_TOKEN"):
                        nonlocal credential_snapshot
                        if credential_snapshot is None:
                            credential_snapshot = _credential_snapshot(binding)
                        return _secret(binding, suffix, guard)

                    from gateway.client_context_codewords_analytics import OPERATIONS as expanded, execute as analytics

                    if name in dedicated.OPS:
                        data = dedicated.execute(binding, args, request, secret)
                    elif operation in expanded.get(name, ()):
                        data = analytics(binding, operation, start, end, request, secret)
                    elif name == NAMES[4]:
                        data = _ads(binding, start, end, request, secret, operation=operation)
                    else:
                        options = {}
                        if name == NAMES[7] and "comparison_start_date" in args:
                            options["comparison"] = (ga.calendar_date(args["comparison_start_date"]),
                                                     ga.calendar_date(args["comparison_end_date"]))
                        data = _HANDLERS[name](binding, start, end, request, secret, **options)
            guard()
            transport._normalized(data)
            result = {"operation": name, "read_operation": operation, "status": "observation", "result": data}
            if start is not None and end is not None:
                result.update(start_date=start.isoformat(), end_date=end.isoformat(), time_zone=binding.time_zone)
            result["retrieved_at"] = datetime.now(timezone.utc).isoformat()
        except oauth.OAuthRefreshError as error:
            guard()
            logging.getLogger(__name__).warning(
                "Codewords OAuth refresh unavailable: operation=%s code=%s status=%s",
                name, error.code, error.status)
            result = {"operation": name, "read_operation": operation, "status": "unavailable", "reason": "oauth_refresh_failed"}
        except Exception:
            # Revocation/cancellation must escape; provider/credential errors never do.
            guard()
            result = {"operation": name, "read_operation": operation, "status": "unavailable", "reason": "read_failed_or_identity_unverified"}
    guard()
    output = json.dumps(result, sort_keys=True, allow_nan=False)
    require(len(output.encode()) <= MAX_OUTPUT)
    guard()
    return ReadReceipt(output, validate_credentials)
