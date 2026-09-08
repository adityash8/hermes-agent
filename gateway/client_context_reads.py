"""Static vendor read grants and bounded executor transport, never registry dispatch."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from gateway.client_context_policy import (
    ContextDenied, _reject_json_constant, _unique_object, fields, identifier,
    items, require, timestamp,
)

# No runtime discovery, plugin lookup, caller-supplied callable or fallback.
VENDORS = MappingProxyType({
    "scoped_meta_daily_insights": "META",
    "scoped_mixpanel_daily_event_count": "MIXPANEL",
    "scoped_tableau_saved_view": "TABLEAU",
    "scoped_posthog_saved_insight": "POSTHOG",
    "scoped_stripe_balance": "STRIPE",
    "scoped_search_console_daily_report": "SEARCH_CONSOLE",
    "scoped_google_ads_daily_report": "GOOGLE_ADS",
})
GRANT_FIELDS = {
    "id", "scope_id", "chat_id", "client", "audience", "account_id", "resource_id",
    "credential_slot", "time_zone", "start_date", "end_date", "expires_at", "operation",
    "status", "approval_evidence", "sharing_evidence", "parameters",
}
MAX_RESPONSE = 65536


def _adapter(name):
    from gateway import client_context_bi, client_context_google_stripe, client_context_marketing

    return {
        "META": client_context_marketing, "MIXPANEL": client_context_marketing,
        "TABLEAU": client_context_bi, "POSTHOG": client_context_bi,
        "STRIPE": client_context_google_stripe, "SEARCH_CONSOLE": client_context_google_stripe,
        "GOOGLE_ADS": client_context_google_stripe,
    }[VENDORS[name]]


@dataclass(frozen=True)
class ReadGrant:
    id: str
    scope_id: str
    chat_id: str
    client: str
    audience: str
    account_id: str
    resource_id: str
    credential_slot: str
    time_zone: str
    start_date: str
    end_date: str
    expires_at: str
    operation: str
    status: str
    approval_evidence: str | None
    sharing_evidence: str | None
    parameters_json: str

    @property
    def parameters(self):
        # Never expose a mutable object that can alter a frozen execution binding.
        return json.loads(self.parameters_json)

    def to_manifest(self):
        raw = asdict(self)
        raw["parameters"] = json.loads(raw.pop("parameters_json"))
        return raw

    @property
    def binding(self):
        return {k: getattr(self, k) for k in
                ("scope_id", "chat_id", "account_id", "resource_id", "client", "audience")}

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
    from gateway.client_context_analytics import calendar_date

    result, seen, owners, credentials = [], set(), {}, {}
    for entry in items(raw, 64):
        fields(entry, GRANT_FIELDS)
        for key in ("id", "scope_id", "chat_id", "client"):
            identifier(entry[key])
        require(entry["id"] not in seen)
        seen.add(entry["id"])
        operation = entry["operation"]
        require(isinstance(operation, str) and operation in VENDORS)
        route = routes.get((entry["scope_id"], entry["chat_id"]))
        require(route is not None)
        require((entry["client"], entry["audience"]) == (route["client"], route["audience"]))
        for key in ("account_id", "resource_id"):
            require(isinstance(entry[key], str) and 0 < len(entry[key]) <= 512)
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
        require(isinstance(entry["parameters"], dict))
        _adapter(operation).validate_parameters(entry)
        vendor = VENDORS[operation]
        # Resource identifiers may be tenant-local; account ownership cannot be.
        for mapping, key, owner in (
            (owners, (vendor, entry["account_id"]), entry["client"]),
            (credentials, (vendor, entry["credential_slot"]), (entry["client"], entry["account_id"])),
        ):
            require(key not in mapping or mapping[key] == owner)
            mapping[key] = owner
        value = dict(entry)
        parameters = json.dumps(value.pop("parameters"), sort_keys=True, allow_nan=False)
        require(len(parameters.encode()) <= 8192)
        result.append(ReadGrant(**value, parameters_json=parameters))
    return tuple(result)


def schemas(grants, name):
    selected = tuple(g for g in grants if g.operation == name)
    if not selected:
        return []
    parameters = selected[0].schema if len(selected) == 1 else {
        "type": "object", "anyOf": [g.schema for g in selected],
    }
    return [{"type": "function", "function": {"name": name,
        "description": _adapter(name).OPERATIONS[name], "parameters": parameters}}]


def _credential(grant, suffix="ACCESS_TOKEN"):
    from agent.secret_scope import current_secret_scope, get_secret

    require(suffix in {"ACCESS_TOKEN", "DEVELOPER_TOKEN", "USERNAME", "PASSWORD"})
    key = f"CLIENT_CONTEXT_{VENDORS[grant.operation]}_{grant.credential_slot}_{suffix}"
    scope = current_secret_scope()
    value = scope.get(key) if scope is not None else get_secret(key)
    require(isinstance(value, str) and 0 < len(value) <= 8192
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))
    return value


def _http_client():
    return httpx.Client(timeout=5.0, follow_redirects=False, trust_env=False)


def _request(client, check, method, url, *, headers=None, json=None, params=None, response_type="json"):
    target = urlsplit(url)
    require(target.scheme == "https" and target.hostname and not target.username
            and not target.password and not target.fragment and target.port in (None, 443))
    require(method in {"GET", "POST"} and response_type in {"json", "text"})
    check()
    with client.stream(method, url, headers={"Accept": "application/json" if response_type == "json" else "text/csv",
                       "Accept-Encoding": "identity", **(headers or {})}, json=json, params=params) as response:
        require(response.status_code == 200)
        kind = response.headers.get("content-type", "").split(";")[0].strip()
        require(kind in ({"application/json"} if response_type == "json" else {"text/csv"}))
        require(response.headers.get("content-encoding", "identity") == "identity")
        data = bytearray()
        for chunk in response.iter_bytes():
            check()
            require(len(data) + len(chunk) <= MAX_RESPONSE)
            data.extend(chunk)
    check()
    text = data.decode("utf-8")
    if response_type == "text":
        return text
    import json as json_module

    result = json_module.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
    require(isinstance(result, (dict, list)))
    return result


def _normalized(value, depth=0):
    """Defense at the shared boundary: adapters must return small JSON data only."""
    require(depth <= 8)
    if isinstance(value, dict):
        require(len(value) <= 64 and all(isinstance(k, str) and len(k) <= 100 for k in value))
        for child in value.values():
            _normalized(child, depth + 1)
    elif isinstance(value, list):
        require(len(value) <= 1000)
        for child in value:
            _normalized(child, depth + 1)
    elif isinstance(value, str):
        require(len(value) <= 512 and not any(ord(c) < 32 for c in value))
    else:
        require(type(value) in {int, float, bool, type(None)})
        if type(value) in {int, float}:
            require(math.isfinite(value) and abs(value) <= 10**18)


def execute(grants, name, arguments, check):
    from gateway.client_context_analytics import calendar_date

    try:
        require(name in VENDORS)
        require(isinstance(arguments, str) and len(arguments.encode()) <= 4096)
        args = json.loads(arguments, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
        fields(args, {"grant_id", "scope_id", "chat_id", "account_id", "resource_id", "client",
                      "audience", "start_date", "end_date"})
        grant = next((g for g in grants if g.id == args["grant_id"]), None)
        require(isinstance(grant, ReadGrant) and grant.operation == name and grant.status == "approved")
        require(all(args[k] == v for k, v in grant.binding.items()))
        start, end = calendar_date(args["start_date"]), calendar_date(args["end_date"])
        require(calendar_date(grant.start_date) <= start <= end <= calendar_date(grant.end_date))
        require((end - start).days < 31 and end < datetime.now(ZoneInfo(grant.time_zone)).date())

        def guarded():
            require(datetime.now(timezone.utc) < timestamp(grant.expires_at))
            check()

        def secret(suffix="ACCESS_TOKEN"):
            guarded()
            value = _credential(grant, suffix)
            guarded()
            return value

        guarded()
        with _http_client() as client:
            def request(method, url, **kwargs):
                return _request(client, guarded, method, url, **kwargs)

            result = _adapter(name).execute(grant, start, end, request, secret)
        guarded()
        _normalized(result)
        output = json.dumps({"operation": name, "grant_id": grant.id, **grant.binding,
            "start_date": start.isoformat(), "end_date": end.isoformat(), "time_zone": grant.time_zone,
            "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "observation", "approval": "not_a_decision_approval", "result": result},
            sort_keys=True, allow_nan=False)
        require(len(output.encode()) <= MAX_RESPONSE)
        guarded()
        return output
    except Exception:
        raise ContextDenied("client context analytics unavailable") from None


def capability_report(registry, allowed=None, effective_toolsets=None):
    """Offline receipt: policy inputs are supplied assertions, never live config reads."""
    from gateway import client_context_analytics as ga
    from gateway.client_context_policy import snapshot
    from gateway.config import Platform
    from gateway.session import SessionSource

    supported = {ga.TOOL: "GA4 ordinary-property daily sessions, activeUsers, screenPageViews",
                 **{name: _adapter(name).OPERATIONS[name] for name in VENDORS}}
    require(allowed is None or (isinstance(allowed, list) and all(n in supported or n in {
        "scoped_source_read", "scoped_history_read"} for n in allowed)))
    policy_supplied = allowed is not None and effective_toolsets is not None
    routes = []
    for route in registry.routes.values():
        source = SessionSource(Platform.SLACK, route["chat_id"], scope_id=route["scope_id"],
                               user_id="operator", chat_type="channel")
        evidence = snapshot(registry, source, "offline capability check", include_history=True)
        current_ids = [r["id"] for r in json.loads(evidence.packet)["evidence"]]
        history = [{"id": r["id"], "lifecycle": r["lifecycle"]}
                   for r in json.loads(evidence.history_packet)]
        grants = [g for g in registry.analytics if (g.scope_id, g.chat_id) ==
                  (route["scope_id"], route["chat_id"])]
        approved = {g.operation for g in grants if g.status == "approved"}
        routes.append({"scope_id": route["scope_id"], "chat_id": route["chat_id"],
            "client": route["client"], "audience": route["audience"],
            "granted_source_ids": route["source_ids"],
            "current_source_ids": current_ids, "approved_history": history,
            "record_policy_permits": {name: (name in allowed and toolset in effective_toolsets)
                if policy_supplied else None for name, toolset in
                (("scoped_source_read", "file"), ("scoped_history_read", "session_search"))},
            "analytics": [{"operation": name,
                "approved_grant_ids": [g.id for g in grants if g.operation == name and g.status == "approved"],
                "proposed_grant_ids": [g.id for g in grants if g.operation == name and g.status == "proposed"],
                "policy_permits": (name in allowed and "web" in effective_toolsets) if policy_supplied else None,
                "manifest_ready": name in approved,
                "trust_blocker": None if name in approved else "missing_explicit_approved_resource_and_route_grant"}
                for name in supported]})
    return {"valid": True, "activation": "not_performed", "network_calls": 0,
            "credentials_checked": False, "live_effective_policy_checked": False,
            "policy_inputs_supplied": policy_supplied, "capabilities": supported, "routes": routes,
            "owner_private": [{**{k: owner[k] for k in ("scope_id", "chat_id", "user_id", "chat_type")},
                "unexpired": datetime.now(timezone.utc) < timestamp(owner["expires_at"])}
                for owner in registry.owners.values()],
            "source_coverage": "only_explicit_source_ids; no_discovery_or_ungranted_reports",
            "live_prerequisites": "review_source_coverage_resource_bindings_permissions_tokens_and_saved_report_pins"}
