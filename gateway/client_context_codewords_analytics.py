"""Named aggregate reads for the fixed Codewords analytics bindings."""

from __future__ import annotations

import re
from types import SimpleNamespace

from gateway import client_context_analytics as ga
from gateway.client_context_policy import fields, items, require

OPERATIONS = {
    "codewords_posthog_read": ("event_trends", "unique_users", "cohort_counts"),
    "codewords_ga4_read": ("acquisition", "device"),
    "codewords_gtm_read": ("configuration",),
}
_TRENDS = {"event_trends": ("total", "events"), "unique_users": ("dau", "unique_users")}
_GA4_DIMENSIONS = {"acquisition": "sessionDefaultChannelGroup", "device": "deviceCategory"}
_GA4_VALUES = {
    "device": frozenset(("desktop", "mobile", "tablet", "smart tv", "(not set)")),
    "acquisition": frozenset(("Direct", "Organic Search", "Paid Search", "Organic Social",
        "Paid Social", "Email", "Affiliates", "Referral", "Audio", "Display", "Cross-network",
        "Paid Other", "Organic Video", "Paid Video", "Organic Shopping", "Paid Shopping",
        "SMS", "Mobile Push Notifications", "Unassigned", "(not set)")),
}
_TAG_TYPES = {"html": "custom_html", "img": "image", "gaawe": "google_analytics",
              "gaawc": "google_analytics", "googtag": "google_tag", "ua": "legacy_analytics",
              "awct": "google_ads", "flc": "floodlight", "fls": "floodlight"}
GA4_LIMIT = 32
COHORT_LIMIT = 10
GTM_TAG_LIMIT = 20


def _integer(value, maximum=10**15):
    require(type(value) is int and 0 <= value <= maximum)
    return value


def _numeric_id(value):
    require(type(value) is str and re.fullmatch(r"[0-9]{1,20}", value))
    return value


def _posthog(binding, operation, start, end, request, secret):
    resources = dict(binding.resources)
    require(resources["origin"] in {"https://eu.posthog.com", "https://us.posthog.com"})
    project = _numeric_id(resources["project_id"])
    url = resources["origin"] + "/api/projects/" + project + "/"
    headers = {"Authorization": "Bearer " + secret()}

    def identity():
        raw = request("GET", url, headers=headers)
        require(type(raw.get("id")) is int and str(raw["id"]) == project
                and raw.get("organization") == resources["organization_id"]
                and raw.get("timezone") == binding.time_zone
                and raw.get("is_pending_deletion", False) is False)

    identity()
    if operation == "cohort_counts":
        # A single project-scoped page: never follow the provider's next URL or
        # retrieve persons, filters, group membership, or recalculation endpoints.
        raw = request("GET", url + "cohorts/", headers=headers,
                      params={"limit": COHORT_LIMIT, "offset": 0})
        rows = items(raw.get("results"), COHORT_LIMIT)
        count = _integer(raw.get("count"))
        require(count >= len(rows) and "next" in raw
                and (raw["next"] is None or type(raw["next"]) is str))
        result, seen = [], set()
        for row in rows:
            require(type(row) is dict)
            cohort_id = _integer(row.get("id"))
            require(cohort_id > 0 and cohort_id not in seen and row.get("deleted") is False)
            seen.add(cohort_id)
            # Some API revisions include the team/project. Never ignore a
            # contradictory owner even though the URL is already project-scoped.
            for key in ("team_id", "project_id"):
                if key in row:
                    require(type(row[key]) is int and str(row[key]) == project)
            require(type(row.get("is_calculating")) is bool and type(row.get("is_static")) is bool)
            errors = _integer(row.get("errors_calculating"))
            available = not row["is_calculating"] and errors == 0 and row.get("count") is not None
            result.append({"cohort_id": cohort_id, "is_static": row["is_static"],
                           "count_available": available,
                           "cached_member_count": _integer(row["count"]) if available else None})
        complete = raw["next"] is None and count == len(rows)
        data = {"cohorts": result, "pagination_complete": complete,
                "rows_returned": len(result), "reported_cohorts": count,
                "incomplete_reason": None if complete else "bounded_first_page",
                "coverage": "Current cached cohort counts; requested dates do not filter cohorts. "
                            "Cohorts can overlap; counts must not be summed. Names and membership are withheld."}
    else:
        math, metric = _TRENDS[operation]
        raw = request("POST", url + "query/", headers=headers, json={"query": {
            "kind": "TrendsQuery", "series": [{"kind": "EventsNode", "event": None, "math": math}],
            "interval": "day", "dateRange": {"date_from": start.isoformat(), "date_to": end.isoformat(),
                                                  "explicitDate": True},
            "properties": [], "filterTestAccounts": False}})
        require(not raw.get("error") and raw.get("hasMore", False) is False)
        results = items(raw.get("results"), 1, empty=False)
        require(type(results[0]) is dict)
        days, values = items(results[0].get("days"), 31), items(results[0].get("data"), 31)
        require(len(days) == len(values) == (end - start).days + 1)
        rows, seen = [], set()
        for day, value in zip(days, values):
            parsed = ga.calendar_date(day)
            require(start <= parsed <= end and day not in seen)
            seen.add(day)
            require(type(value) in (int, float) and 0 <= value <= 10**15 and value == int(value))
            rows.append({"date": day, metric: int(value)})
        data = {"rows": sorted(rows, key=lambda row: row["date"]), "pagination_complete": True,
                "coverage": "All events, including test accounts. Unique users are daily, not additive across days."}
    identity()
    return {"project_id": project, **data}


def _ga4(binding, operation, start, end, request, secret):
    resources = dict(binding.resources)
    account = _numeric_id(resources["account_id"])
    property_id = _numeric_id(resources["property_id"])
    grant = SimpleNamespace(property_id=property_id, account_id=account, time_zone=binding.time_zone)
    headers = {"Authorization": "Bearer " + secret()}
    url = "https://analyticsadmin.googleapis.com/v1beta/properties/" + property_id

    def identity():
        raw = request("GET", url, headers=headers)
        ga._property(raw, grant)
        require(raw.get("currencyCode") == resources["currency"])

    identity()
    dimension = _GA4_DIMENSIONS[operation]
    raw = request("POST", "https://analyticsdata.googleapis.com/v1beta/properties/" + property_id + ":runReport",
        headers=headers, json={"dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "dimensions": [{"name": dimension}], "metrics": [{"name": metric} for metric in ga.METRICS],
            "orderBys": [{"dimension": {"dimensionName": dimension}}],
            "limit": str(GA4_LIMIT), "offset": "0", "keepEmptyRows": False})
    require(raw.get("kind") == "analyticsData#runReport"
            and raw.get("dimensionHeaders") == [{"name": dimension}]
            and raw.get("metricHeaders") == [{"name": metric, "type": "TYPE_INTEGER"} for metric in ga.METRICS])
    metadata = raw.get("metadata")
    require(type(metadata) is dict and metadata.get("timeZone") == binding.time_zone
            and metadata.get("currencyCode") == resources["currency"])
    for key in ("dataLossFromOtherRow", "subjectToThresholding"):
        require(type(metadata.get(key, False)) is bool)
    require(metadata.get("emptyReason", "") == ""
            and metadata.get("schemaRestrictionResponse", {}) in ({}, {"activeMetricRestrictions": []})
            and metadata.get("samplingMetadatas", []) == [])
    rows = items(raw.get("rows", []), GA4_LIMIT)
    count = _integer(raw.get("rowCount", 0))
    require(count >= len(rows))
    result, seen = [], set()
    for row in rows:
        fields(row, {"dimensionValues", "metricValues"})
        dims = items(row["dimensionValues"], 1, empty=False)
        value = fields(dims[0], {"value"})["value"]
        require(type(value) is str and value in _GA4_VALUES[operation] and value not in seen)
        seen.add(value)
        metrics = items(row["metricValues"], len(ga.METRICS), empty=False)
        require(len(metrics) == len(ga.METRICS))
        numbers = []
        for entry in metrics:
            number = fields(entry, {"value"})["value"]
            require(type(number) is str and re.fullmatch(r"0|[1-9][0-9]{0,14}", number))
            numbers.append(int(number))
        result.append({dimension: value, **dict(zip(ga.METRICS, numbers))})
    identity()
    return {"rows": result, "rows_returned": len(result), "reported_rows": count,
            "pagination_complete": count == len(result),
            "incomplete_reason": None if count == len(result) else "bounded_first_page",
            "data_loss_from_other_row": metadata.get("dataLossFromOtherRow", False),
            "subject_to_thresholding": metadata.get("subjectToThresholding", False),
            "coverage": "Reported dimension rows for the full requested period; no cross-row user totals are computed."}


def _gtm(binding, _operation, _start, _end, request, secret):
    resources = dict(binding.resources)
    account = _numeric_id(resources["account_id"])
    headers = {"Authorization": "Bearer " + secret()}
    mask = ("accountId,containerId,containerVersionId,path,deleted,"
            "container(accountId,containerId,publicId),"
            "tag(accountId,containerId,tagId,type,paused),"
            "trigger(accountId,containerId,triggerId),variable(accountId,containerId,variableId)")
    result = []
    for kind in ("web", "server"):
        container = _numeric_id(resources[kind + "_container"])
        path = "accounts/" + account + "/containers/" + container
        url = "https://tagmanager.googleapis.com/tagmanager/v2/" + path + "/versions:live"

        def configuration():
            raw = request("GET", url, headers=headers, params={"fields": mask})
            require(raw.get("accountId") == account and raw.get("containerId") == container
                    and raw.get("deleted", False) is False)
            owner = raw.get("container")
            require(type(owner) is dict and owner.get("accountId") == account
                    and owner.get("containerId") == container
                    and owner.get("publicId") == resources[kind + "_public_id"])
            version = _numeric_id(raw.get("containerVersionId"))
            require(raw.get("path") == path + "/versions/" + version)
            counts, tags = {}, []
            for key in ("tag", "trigger", "variable"):
                values = items(raw.get(key, []), 500)
                seen = set()
                for value in values:
                    require(type(value) is dict and value.get("accountId") == account
                            and value.get("containerId") == container)
                    resource_id = _numeric_id(value.get(key + "Id"))
                    require(resource_id not in seen)
                    seen.add(resource_id)
                    if key == "tag":
                        require(type(value.get("type")) is str
                                and type(value.get("paused", False)) is bool)
                        tags.append({"tag_id": resource_id, "category": _TAG_TYPES.get(value["type"], "other"),
                                     "paused": value.get("paused", False)})
                counts[key + "_count"] = len(values)
            tags.sort(key=lambda tag: int(tag["tag_id"]))
            complete = len(tags) <= GTM_TAG_LIMIT
            return {"container": kind, "version": version, **counts,
                    "tags": tags[:GTM_TAG_LIMIT], "pagination_complete": complete,
                    "incomplete_reason": None if complete else "tag_output_limit",
                    "tag_details_returned": min(len(tags), GTM_TAG_LIMIT)}

        before = configuration()
        require(configuration() == before)
        result.append(before)
    return {"containers": result, "coverage": "Live version counts and bounded tag categories/paused flags. "
            "Names, URLs, parameters, custom HTML, templates and variable values are withheld."}


_HANDLERS = {"codewords_posthog_read": _posthog, "codewords_ga4_read": _ga4,
             "codewords_gtm_read": _gtm}


def execute(binding, operation, start, end, request, secret):
    """The caller owns date validation, revocation checks and bounded transport."""
    require(binding.name in OPERATIONS and type(operation) is str and operation in OPERATIONS[binding.name])
    return _HANDLERS[binding.name](binding, operation, start, end, request, secret)
