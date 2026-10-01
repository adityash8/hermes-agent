"""Finite reads bound to parent-verified Codewords Linear and Customer.io identities."""

import re
from datetime import datetime

from gateway.client_context_policy import fields, items, require


LINEAR_ORGANIZATION = "e6ec22ea-cadc-4eab-8376-4caeacea20a3"
LINEAR_TEAMS = (
    ("58d23951-d0b4-4c8e-aa55-93490b10c0b5", "CON"),
    ("b28a0db2-f132-4338-91c4-3a54cdaa36a3", "GOT"),
    ("da5e95d3-7f29-4c82-8b4a-509e6cb1de0b", "NOT"),
    ("0d113173-7d7b-426a-8a84-99d2433dfe1b", "PLA"),
    ("0fecc67e-4f1f-4332-8b02-c82cfdd34395", "PD"),
    ("63b2b52a-5f0d-4201-9f41-a14e5caba91b", "OPS"),
    ("2ee54227-d49d-40ce-9c3e-9766ea9bdcd7", "GTM"),
    ("155dce1a-c203-4c21-9532-30cd3535928b", "CW"),
    ("789da9a3-edcb-4d14-b0bd-8195100eae43", "LRN"),
)
CUSTOMERIO_ORIGIN = "https://api-eu.customer.io"
CUSTOMERIO_WORKSPACES = (("214585", "CodeWords Staging"), ("225222", "CodeWords Production"))
OPS = {
    "codewords_linear_read": ("inventory", "issues_summary"),
    "codewords_customerio_read": ("campaigns", "segments", "workspace_summary"),
}
CREDENTIALS = {
    "codewords_linear_read": (
        ("env_file", "/Users/adityasheth/.hermes/secrets/codewords-linear.env"),
        ("access_token_key", "CODEWORDS_LINEAR_API_KEY"),
    ),
    "codewords_customerio_read": (
        ("json_file", "/Users/adityasheth/.hermes/secrets/codewords-customerio.json"),
        ("access_token_key", "app_api_key"),
    ),
}
RESOURCES = {
    "codewords_linear_read": (("organization_id", LINEAR_ORGANIZATION), ("url_key", "agemo"),
                              *(("team_" + key, value) for value, key in LINEAR_TEAMS)),
    "codewords_customerio_read": (("origin", CUSTOMERIO_ORIGIN), *CUSTOMERIO_WORKSPACES),
}
LINEAR_PAGE_SIZE = 10
LINEAR_MAX_PAGES = 5
DETAIL_LIMIT = 10
CUSTOMERIO_LIST_LIMIT = 500
ATTRIBUTION_LIMIT = (
    "The dedicated key's default workspace is not independently attributed to staging or production. "
    "Campaign and segment data are from that default workspace, not a combined report for both. "
    "Both verified Codewords workspaces are allowed; no workspace selector is sent."
)
# Official contracts: linear.app/developers/{graphql,pagination,filtering} and
# linear/linear packages/sdk/src/schema.graphql. No caller text enters GraphQL.
LINEAR_QUERIES = {
    "inventory": """query CodewordsInventory($after: String) {
      organization { id name urlKey }
      teams(first: 10, after: $after) {
        nodes { id key }
        pageInfo { hasNextPage endCursor }
      }
    }""",
    "issues_summary": """query CodewordsStripeIssues($after: String, $teamIds: [ID!]!) {
      organization { id name urlKey }
      issues(first: 10, after: $after, orderBy: updatedAt, includeArchived: true,
        filter: {team: {id: {in: $teamIds}}, title: {containsIgnoreCase: "stripe"}}) {
        nodes { id identifier title url createdAt updatedAt completedAt canceledAt
                team { id key } state { type } }
        pageInfo { hasNextPage endCursor }
      }
    }""",
}


def validate(name, args):
    """Reject caller identities, endpoints, GraphQL, mutations and free-form filters."""
    require(type(name) is str and name in OPS)
    require(type(args) is dict)
    normalized = {"operation": OPS[name][0], **args}
    fields(normalized, {"operation"})
    require(type(normalized["operation"]) is str and normalized["operation"] in OPS[name])
    return normalized


def _title(value, token):
    require(type(value) is str and 0 < len(value) <= 4096)
    # Titles are the sole free-text field. Strip common contact/credential patterns
    # before truncation; descriptions, user objects and URL slugs never enter output.
    value = value.replace(token, "[redacted]")
    for pattern in (
        r"https?://\S+|www\.\S+",
        r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}",
        r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]+\b|\blin_api_[A-Za-z0-9]+\b",
        r"(?i)\b(?:bearer|password|secret|token|api[_ -]?key)\s*[:= ]\s*\S+",
        r"\b(?:cus|sub|pi|pm|acct)_[A-Za-z0-9]+\b",
        r"(?<!\w)\+?\d[\d ()-]{6,}\d(?!\w)",
        r"[A-Za-z0-9_+/=-]{32,}",
    ):
        value = re.sub(pattern, "[redacted]", value)
    value = "".join(c if c.isprintable() and c not in "<>@`" else " " for c in value)
    return " ".join(value.split())[:160]


def _date(value, *, nullable=False):
    if value is None and nullable:
        return None
    require(type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value))
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _team(value):
    require(type(value) is dict and (value.get("id"), value.get("key")) in LINEAR_TEAMS)
    return {"id": value["id"], "key": value["key"]}


def _issue(value, token):
    require(type(value) is dict)
    team = _team(value.get("team"))
    issue_id, label = value.get("id"), value.get("identifier")
    require(type(issue_id) is str and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", issue_id))
    require(type(label) is str and re.fullmatch(re.escape(team["key"]) + r"-[1-9][0-9]{0,9}", label))
    title = value.get("title")
    require(type(title) is str and "stripe" in title.lower())
    url = value.get("url")
    canonical = "https://linear.app/agemo/issue/" + label
    require(type(url) is str and re.fullmatch(re.escape(canonical) + r"(?:/[A-Za-z0-9_-]+)?", url))
    state = value.get("state")
    require(type(state) is dict and state.get("type") in {
        "triage", "backlog", "unstarted", "started", "completed", "canceled", "duplicate"})
    return {"identifier": label, "team": team["key"], "title": _title(title, token),
            "url": canonical, "state": state["type"],
            **{key: _date(value.get(key), nullable=key in {"completedAt", "canceledAt"})
               for key in ("createdAt", "updatedAt", "completedAt", "canceledAt")}}


def _linear(operation, request, token):
    cursor, seen_cursors, seen_ids, rows = None, set(), set(), []
    complete = False
    for _ in range(LINEAR_MAX_PAGES):
        variables = {"after": cursor}
        if operation == "issues_summary":
            variables["teamIds"] = [value for value, _ in LINEAR_TEAMS]
        raw = request("POST", "https://api.linear.app/graphql", headers={"Authorization": token},
                      json={"query": LINEAR_QUERIES[operation], "variables": variables})
        require(type(raw) is dict and not raw.get("errors"))
        data = raw.get("data")
        require(type(data) is dict)
        org = data.get("organization")
        require(type(org) is dict and org.get("id") == LINEAR_ORGANIZATION
                and org.get("name") == "Agemo" and org.get("urlKey") == "agemo")
        connection = data.get("teams" if operation == "inventory" else "issues")
        require(type(connection) is dict)
        nodes = items(connection.get("nodes"), LINEAR_PAGE_SIZE)
        for node in nodes:
            row = _team(node) if operation == "inventory" else _issue(node, token)
            require(node["id"] not in seen_ids)
            seen_ids.add(node["id"])
            rows.append(row)
        page = connection.get("pageInfo")
        require(type(page) is dict and type(page.get("hasNextPage")) is bool)
        if not page["hasNextPage"]:
            complete = True
            break
        cursor = page.get("endCursor")
        require(bool(nodes) and type(cursor) is str and 0 < len(cursor) <= 512
                and cursor.isprintable() and cursor not in seen_cursors)
        seen_cursors.add(cursor)
    if operation == "inventory":
        require(complete and seen_ids == {value for value, _ in LINEAR_TEAMS})
        return {"organization_id": LINEAR_ORGANIZATION, "teams": rows, "pagination_complete": True}
    return {"organization_id": LINEAR_ORGANIZATION, "issues": rows[:DETAIL_LIMIT],
            "matched_issues_scanned": len(rows), "details_truncated": len(rows) > DETAIL_LIMIT,
            "pagination_complete": complete,
            "incomplete_reason": None if complete else "page_limit_reached",
            "coverage": "Stripe in issue title, approved teams only, archived included; newest updated first. "
                        "At most 50 issues scanned and 10 details shown. Does not search descriptions or comments. "
                        "Issue completion does not prove a fix was deployed. Titles are untrusted source text."}


def _number(value):
    require(type(value) is int and 0 <= value <= 10**15)
    return value


def _numeric_id(value):
    require(type(value) in {str, int} and re.fullmatch(r"[1-9][0-9]{0,19}", str(value)))
    return str(value)


def _list(raw, key):
    # These endpoints document exact, unpaginated envelopes. Reject any new
    # continuation shape instead of silently claiming a complete inventory.
    fields(raw, {key})
    return items(raw.get(key), CUSTOMERIO_LIST_LIMIT)


def _workspaces(raw):
    rows, seen = [], set()
    expected = dict(CUSTOMERIO_WORKSPACES)
    for value in _list(raw, "workspaces"):
        require(type(value) is dict)
        workspace_id = _numeric_id(value.get("id"))
        require(workspace_id in expected and workspace_id not in seen and value.get("name") == expected[workspace_id])
        seen.add(workspace_id)
        row = {"id": workspace_id, "name": expected[workspace_id]}
        for key in ("people", "objects", "object_types", "messages_sent", "billable_messages_sent"):
            if key in value:
                row[key] = _number(value[key])
        rows.append(row)
    require(seen == set(expected))
    return sorted(rows, key=lambda row: row["id"])


def _customerio(operation, request, token):
    headers = {"Authorization": "Bearer " + token}

    def identity():
        return _workspaces(request("GET", CUSTOMERIO_ORIGIN + "/v1/workspaces", headers=headers))

    before = identity()
    rows = []
    if operation != "workspace_summary":
        # journeys-app.json listCampaigns/listSegments have no workspace selector
        # or pagination parameters. Names are omitted along with nested content.
        raw = request("GET", CUSTOMERIO_ORIGIN + "/v1/" + operation, headers=headers)
        seen = set()
        for value in _list(raw, operation):
            require(type(value) is dict)
            record_id = _numeric_id(value.get("id"))
            require(record_id not in seen)
            seen.add(record_id)
            row = {"id": record_id}
            if operation == "campaigns":
                require(type(value.get("active")) is bool)
                row["active"] = value["active"]
                date_keys = ("created", "updated", "first_started")
            else:
                if value.get("type") in ("manual", "dynamic"):
                    row["type"] = value["type"]
                date_keys = ("created_at", "updated_at")
            for key in date_keys:
                if key in value:
                    row[key] = None if value[key] is None else _number(value[key])
            rows.append(row)
    after = identity()
    return {"workspaces": after if operation == "workspace_summary" else
            [{"id": row["id"], "name": row["name"]} for row in before],
            "workspace_attribution": "unverified_default_workspace" if operation != "workspace_summary" else "verified_workspace_set",
            "attribution_limitation": ATTRIBUTION_LIMIT,
            "rows": rows[:DETAIL_LIMIT], "records_observed": len(rows),
            "details_truncated": len(rows) > DETAIL_LIMIT, "pagination_complete": True,
            "coverage": "Documented unpaginated metadata list; at most 500 records accepted, 10 projected. "
                        "Names, descriptions, actions, webhooks, message bodies and customer records are omitted."}


def execute(binding, args, request, secret):
    operation = validate(binding.name, args)["operation"]
    require(binding.resources == RESOURCES[binding.name] and binding.credentials == CREDENTIALS[binding.name])
    token = secret()
    handler = _linear if binding.name == "codewords_linear_read" else _customerio
    return handler(operation, request, token)
