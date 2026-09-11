"""One account-bound GA4 read operation; no global registry, discovery or actions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from gateway.client_context_policy import (
    ContextDenied, _reject_json_constant, _unique_object, fields, identifier,
    items, require, timestamp,
)

TOOL = "scoped_ga4_daily_report"
METRICS = ("sessions", "activeUsers", "screenPageViews")
MAX_RESPONSE = 65536
GRANT_FIELDS = {
    "id", "scope_id", "chat_id", "client", "audience", "account_id", "property_id",
    "credential_slot", "time_zone", "start_date", "end_date", "expires_at", "operation",
    "status", "approval_evidence", "sharing_evidence",
}


def calendar_date(value):
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    return date.fromisoformat(value)


@dataclass(frozen=True)
class AnalyticsGrant:
    id: str
    scope_id: str
    chat_id: str
    client: str
    audience: str
    account_id: str
    property_id: str
    credential_slot: str
    time_zone: str
    start_date: str
    end_date: str
    expires_at: str
    operation: str
    status: str
    approval_evidence: str | None
    sharing_evidence: str | None

    @property
    def binding(self):
        return {k: getattr(self, k) for k in ("account_id", "property_id", "client", "audience")}

    @property
    def schema(self):
        props = {k: {"type": "string", "enum": [v]} for k, v in self.binding.items()}
        props["grant_id"] = {"type": "string", "enum": [self.id]}
        for key in ("start_date", "end_date"):
            props[key] = {"type": "string", "format": "date",
                          "description": f"Approved range: {self.start_date} through {self.end_date}, inclusive."}
        return {"type": "object", "properties": props, "required": list(props),
                "additionalProperties": False}


def parse_grants(raw, routes):
    """Validate all bindings before any source bytes or provider input is read."""
    from gateway import client_context_reads as reads

    entries = items(raw, 64)
    require(all(isinstance(e, dict) and isinstance(e.get("operation"), str) for e in entries))
    other = reads.parse_grants([e for e in entries if e["operation"] != TOOL], routes)
    grants, seen, properties, credentials = [], {g.id for g in other}, {}, {}
    for entry in (e for e in entries if e["operation"] == TOOL):
        fields(entry, GRANT_FIELDS)
        for key in ("id", "scope_id", "chat_id", "client"):
            identifier(entry[key])
        require(entry["id"] not in seen)
        seen.add(entry["id"])
        route = routes.get((entry["scope_id"], entry["chat_id"]))
        require(route is not None)
        require(entry["client"] == route["client"] and entry["audience"] == route["audience"])
        require(entry["operation"] == TOOL)
        for key in ("account_id", "property_id"):
            require(isinstance(entry[key], str) and re.fullmatch(r"[1-9][0-9]{0,19}", entry[key]))
        require(isinstance(entry["credential_slot"], str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", entry["credential_slot"]))
        require(isinstance(entry["time_zone"], str) and len(entry["time_zone"]) <= 64)
        ZoneInfo(entry["time_zone"])
        start, end = calendar_date(entry["start_date"]), calendar_date(entry["end_date"])
        require(0 <= (end - start).days < 366)
        require(timestamp(entry["expires_at"]) <= timestamp(route["expires_at"]))
        require(entry["status"] in {"proposed", "approved"})
        if entry["status"] == "approved":
            identifier(entry["approval_evidence"])
            if entry["audience"] == "shared":
                identifier(entry["sharing_evidence"])
            else:
                require(entry["sharing_evidence"] is None)
        else:
            require(entry["approval_evidence"] is None and entry["sharing_evidence"] is None)
        # A project or credential slot cannot silently cross client ownership.
        owner = (entry["client"], entry["account_id"])
        for mapping, key in ((properties, entry["property_id"]), (credentials, entry["credential_slot"])):
            require(key not in mapping or mapping[key] == owner)
            mapping[key] = owner
        grants.append(AnalyticsGrant(**entry))
    return tuple(grants) + other


def route_grants(registry, source, now=None):
    now = now or datetime.now(timezone.utc)
    selected = tuple(g for g in registry.analytics if (g.scope_id, g.chat_id)
                     == (source.scope_id, source.chat_id) and g.status == "approved")
    for grant in selected:
        require(now < timestamp(grant.expires_at))
    return selected


def schemas(grants):
    grants = tuple(g for g in grants if g.operation == TOOL)
    if not grants:
        return []
    # anyOf preserves grant/account correlations, unlike independently merged enums.
    parameters = grants[0].schema if len(grants) == 1 else {
        "type": "object", "anyOf": [g.schema for g in grants],
    }
    return [{"type": "function", "function": {
        "name": TOOL,
        "description": "Read GA4 daily sessions, activeUsers and screenPageViews. At most 31 completed days "
                       "inside the approved date window. Observations only, never decision approval.",
        "parameters": parameters,
    }}]


def _credential(grant):
    from agent.secret_scope import current_secret_scope, get_secret

    key = f"CLIENT_CONTEXT_GA4_{grant.credential_slot}_ACCESS_TOKEN"
    scope = current_secret_scope()
    token = scope.get(key) if scope is not None else get_secret(key)
    require(isinstance(token, str) and 0 < len(token) <= 8192
            and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token))
    return token


def _http_client():
    # Fixed Google origins only; no redirects, proxy env, cookies from other tools,
    # application retries, SDK discovery, or default account resolution.
    return httpx.Client(timeout=5.0, follow_redirects=False, trust_env=False)


def _request(client, method, url, token, check, body=None):
    check()
    with client.stream(method, url, headers={"Authorization": f"Bearer {token}",
                       "Accept": "application/json", "Accept-Encoding": "identity"}, json=body) as response:
        require(response.status_code == 200)
        require(response.headers.get("content-type", "").split(";")[0] == "application/json")
        require(response.headers.get("content-encoding", "identity") == "identity")
        data = bytearray()
        for chunk in response.iter_bytes():
            check()
            require(len(data) + len(chunk) <= MAX_RESPONSE)
            data.extend(chunk)
    check()
    result = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
    require(isinstance(result, dict))
    return result


def _property(raw, grant):
    require(raw.get("name") == f"properties/{grant.property_id}")
    require(raw.get("account") == raw.get("parent") == f"accounts/{grant.account_id}")
    require(raw.get("propertyType") == "PROPERTY_TYPE_ORDINARY")
    require(raw.get("timeZone") == grant.time_zone)
    for key in ("deleteTime", "expireTime"):
        if key in raw:
            require(raw[key] == "")
    # Discard display names and every other free-text field at the boundary.


def _report(raw, grant, start, end):
    require(set(raw) <= {"dimensionHeaders", "metricHeaders", "rows", "rowCount", "metadata", "kind"})
    require(raw.get("kind") == "analyticsData#runReport")
    require(raw.get("dimensionHeaders") == [{"name": "date"}])
    require(raw.get("metricHeaders") == [{"name": m, "type": "TYPE_INTEGER"} for m in METRICS])
    metadata = raw.get("metadata")
    require(isinstance(metadata, dict) and set(metadata) <= {
        "timeZone", "currencyCode", "dataLossFromOtherRow", "subjectToThresholding",
        "emptyReason", "schemaRestrictionResponse", "samplingMetadatas",
    })
    require(metadata.get("timeZone") == grant.time_zone)
    if "currencyCode" in metadata:
        require(isinstance(metadata["currencyCode"], str) and re.fullmatch(r"[A-Z]{3}", metadata["currencyCode"]))
    for key in ("dataLossFromOtherRow", "subjectToThresholding"):
        require(type(metadata.get(key, False)) is bool and not metadata.get(key, False))
    if "emptyReason" in metadata:
        require(metadata["emptyReason"] == "")
    if "schemaRestrictionResponse" in metadata:
        require(type(metadata["schemaRestrictionResponse"]) is dict
                and metadata["schemaRestrictionResponse"] in ({}, {"activeMetricRestrictions": []}))
    if "samplingMetadatas" in metadata:
        require(type(metadata["samplingMetadatas"]) is list and not metadata["samplingMetadatas"])
    rows = items(raw.get("rows", []), 31)
    count = raw.get("rowCount", 0)
    require(type(count) is int and count == len(rows) and count <= (end - start).days + 1)
    dates, result = set(), []
    for row in rows:
        fields(row, {"dimensionValues", "metricValues"})
        dimensions = items(row["dimensionValues"], 1, empty=False)
        value = fields(dimensions[0], {"value"})["value"]
        require(isinstance(value, str) and re.fullmatch(r"[0-9]{8}", value))
        day = datetime.strptime(value, "%Y%m%d").date()
        require(start <= day <= end and day not in dates)
        dates.add(day)
        values = items(row["metricValues"], len(METRICS), empty=False)
        require(len(values) == len(METRICS))
        metrics = []
        for entry in values:
            metric = fields(entry, {"value"})["value"]
            require(isinstance(metric, str) and re.fullmatch(r"0|[1-9][0-9]{0,14}", metric))
            metrics.append(int(metric))
        result.append({"date": day.isoformat(), **dict(zip(METRICS, metrics))})
    return sorted(result, key=lambda r: r["date"])


def execute(grants, arguments, check):
    """Only validated, normalized numeric observations may leave this executor."""
    try:
        require(isinstance(arguments, str) and len(arguments.encode()) <= 4096)
        args = json.loads(arguments, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
        fields(args, {"grant_id", "account_id", "property_id", "client", "audience", "start_date", "end_date"})
        grant = next((g for g in grants if g.id == args["grant_id"]), None)
        require(grant is not None and grant.status == "approved" and grant.operation == TOOL)
        require(all(args[k] == value for k, value in grant.binding.items()))
        start, end = calendar_date(args["start_date"]), calendar_date(args["end_date"])
        require(calendar_date(grant.start_date) <= start <= end <= calendar_date(grant.end_date))
        require((end - start).days < 31 and end < datetime.now(ZoneInfo(grant.time_zone)).date())

        def guarded():
            require(datetime.now(timezone.utc) < timestamp(grant.expires_at))
            check()

        guarded()
        token = _credential(grant)
        guarded()  # A delayed credential resolver cannot start I/O after revocation.
        with _http_client() as client:
            url = f"https://analyticsadmin.googleapis.com/v1beta/properties/{grant.property_id}"
            _property(_request(client, "GET", url, token, guarded), grant)
            body = {"dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
                    "dimensions": [{"name": "date"}], "metrics": [{"name": m} for m in METRICS],
                    "limit": "31", "keepEmptyRows": False}
            raw = _request(client, "POST",
                           f"https://analyticsdata.googleapis.com/v1beta/properties/{grant.property_id}:runReport",
                           token, guarded, body)
            rows = _report(raw, grant, start, end)
            _property(_request(client, "GET", url, token, guarded), grant)
        guarded()
        return json.dumps({"operation": TOOL, "grant_id": grant.id, **grant.binding,
            "start_date": start.isoformat(), "end_date": end.isoformat(), "time_zone": grant.time_zone,
            "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "observation", "approval": "not_a_decision_approval",
            "freshness": "queried_at_retrieved_at; historical_dates; not_realtime",
            "rows": rows}, sort_keys=True)
    except Exception:
        # Neither upstream error bodies nor credential-bearing request exceptions
        # may enter model history, gateway logs, or peer-facing errors. ContextChanged
        # subclasses ContextDenied and is caught here on purpose: a peer must not be able
        # to tell a mid-turn revocation apart from any other failure.
        raise ContextDenied("client context analytics unavailable") from None
