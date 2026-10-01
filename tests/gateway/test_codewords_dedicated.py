"""Verified dedicated reads through the real dispatcher and bounded HTTP transport."""

import copy
from dataclasses import replace
import json
from pathlib import Path

import httpx
import pytest

from gateway import client_context_codewords_dedicated as dedicated
from gateway import client_context_codewords_reads as reads
from gateway.client_context_policy import ContextDenied

LINEAR = "codewords_linear_read"
CUSTOMERIO = "codewords_customerio_read"
TOKEN = "synthetic-dedicated-secret"
PRIVATE = "private-payload-canary"


@pytest.fixture
def vendor_payloads():
    team_id, key = dedicated.LINEAR_TEAMS[0]
    issue = {"id": "00000000-0000-4000-8000-000000000001", "identifier": key + "-123",
             "title": "Fix Stripe subscription cancellation", "url": "https://linear.app/agemo/issue/" + key + "-123/fix-stripe",
             "createdAt": "2026-09-01T12:00:00.000Z", "updatedAt": "2026-09-20T12:00:00Z",
             "completedAt": "2026-09-20T12:00:00Z", "canceledAt": None,
             "team": {"id": team_id, "key": key}, "state": {"type": "completed", "name": PRIVATE},
             "description": PRIVATE, "comments": [PRIVATE], "assignee": {"email": PRIVATE}}
    org = {"id": dedicated.LINEAR_ORGANIZATION, "name": "Agemo", "urlKey": "agemo"}
    def connection(nodes):
        return {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}
    return {
        "inventory": {"data": {"organization": org, "teams": connection([
            {"id": team_id, "key": key, "name": PRIVATE} for team_id, key in dedicated.LINEAR_TEAMS])}},
        "issues_summary": {"data": {"organization": org, "issues": connection([issue])}},
        "workspace_summary": {"workspaces": [{"id": workspace_id, "name": name, "people": 3,
            "objects": 4, "messages_sent": 5, "private": PRIVATE}
            for workspace_id, name in dedicated.CUSTOMERIO_WORKSPACES]},
        "campaigns": {"campaigns": [{"id": 12, "name": PRIVATE, "active": True, "state": "running",
            "created": 1700000000, "updated": 1750000000, "first_started": 1700000001,
            "actions": [{"body": PRIVATE, "webhook": PRIVATE}], "description": PRIVATE,
            "customers": [{"email": PRIVATE}]}]},
        "segments": {"segments": [{"id": 13, "name": PRIVATE, "type": "dynamic", "state": "done",
            "created_at": 1700000000, "updated_at": 1750000000, "description": PRIVATE,
            "customer_ids": [PRIVATE], "deduplicate_id": PRIVATE}]},
    }


@pytest.fixture
def harness(monkeypatch, tmp_path, vendor_payloads):
    files = {LINEAR: tmp_path / "linear.env", CUSTOMERIO: tmp_path / "customerio.json"}
    files[LINEAR].write_text("CODEWORDS_LINEAR_API_KEY=" + TOKEN + "\nAGEMO_LINEAR_API_KEY=wrong-secret\n")
    files[CUSTOMERIO].write_text(json.dumps({"app_api_key": TOKEN, "region": "eu",
        "base_url": dedicated.CUSTOMERIO_ORIGIN, "workspace_label": "untrusted label"}))
    credentials = {
        LINEAR: (("env_file", str(files[LINEAR])), ("access_token_key", "CODEWORDS_LINEAR_API_KEY")),
        CUSTOMERIO: (("json_file", str(files[CUSTOMERIO])), ("access_token_key", "app_api_key")),
    }
    monkeypatch.setattr(dedicated, "CREDENTIALS", credentials)
    monkeypatch.setattr(reads, "_BINDINGS", tuple(replace(b, credentials=credentials[b.name])
        if b.name in credentials else b for b in reads.bindings()))
    monkeypatch.setenv("LINEAR_API_KEY", "ambient-secret")
    monkeypatch.setenv("CUSTOMERIO_APP_API_KEY", "ambient-secret")
    calls = []

    def install(mutate=None, status=200):
        def handle(request):
            calls.append(request)
            if request.url.host == "api.linear.app":
                assert request.method == "POST" and str(request.url) == "https://api.linear.app/graphql"
                body = json.loads(request.content)
                operation = next(k for k, v in dedicated.LINEAR_QUERIES.items() if v == body["query"])
                assert request.headers["Authorization"] == TOKEN
                assert set(body) == {"query", "variables"}
                assert set(body["variables"]) == ({"after"} if operation == "inventory" else {"after", "teamIds"})
                if operation == "issues_summary":
                    assert body["variables"]["teamIds"] == [team_id for team_id, _ in dedicated.LINEAR_TEAMS]
            else:
                assert request.method == "GET" and request.url.host == "api-eu.customer.io"
                assert not request.url.query
                assert request.url.path in {"/v1/workspaces", "/v1/campaigns", "/v1/segments"}
                assert request.headers["Authorization"] == "Bearer " + TOKEN
                assert not any("workspace" in key for key in request.headers)
                operation = request.url.path.rsplit("/", 1)[1]
                if operation == "workspaces":
                    operation = "workspace_summary"
            payload = copy.deepcopy(vendor_payloads[operation])
            if mutate:
                mutate(operation, payload, request, len(calls))
            return httpx.Response(status, json=payload)
        monkeypatch.setattr(reads.transport, "_http_client", lambda: httpx.Client(
            transport=httpx.MockTransport(handle), follow_redirects=False, trust_env=False))

    def run(name=LINEAR, operation="issues_summary", check=lambda: None):
        return reads.execute(name, json.dumps({"operation": operation}), check)

    install()
    return install, run, calls, files


@pytest.mark.parametrize("name,operation", [(n, op) for n, ops in dedicated.OPS.items() for op in ops])
def test_real_fixed_reads_project_only_safe_fields(harness, name, operation):
    _, run, calls, _ = harness
    receipt = run(name, operation)
    result = json.loads(receipt)
    assert result["status"] == "observation"
    assert PRIVATE not in receipt and TOKEN not in receipt and "ambient-secret" not in receipt
    data = result["result"]
    assert data["pagination_complete"] is True
    if name == LINEAR:
        assert len(calls) == 1
        if operation == "issues_summary":
            row = data["issues"][0]
            assert row["title"] == "Fix Stripe subscription cancellation"
            assert row["state"] == "completed" and row["completedAt"]
            assert row["url"] == "https://linear.app/agemo/issue/CON-123"
        else:
            assert {r["id"] for r in data["teams"]} == {i for i, _ in dedicated.LINEAR_TEAMS}
    else:
        assert [str(r.url.path) for r in calls] == (["/v1/workspaces"] * 2 if operation == "workspace_summary"
            else ["/v1/workspaces", "/v1/" + operation, "/v1/workspaces"])
        assert {(w["id"], w["name"]) for w in data["workspaces"]} == set(dedicated.CUSTOMERIO_WORKSPACES)
        assert "not independently attributed" in data["attribution_limitation"]
        if operation != "workspace_summary":
            assert data["workspace_attribution"] == "unverified_default_workspace"
            assert len(data["rows"]) == 1


@pytest.mark.parametrize("args", [None, [], {"operation": None}, {"operation": []},
    {"operation": True}, {"operation": "create"}, {"operation": "mutation { issueCreate }"},
    {"operation": "send"}, {"operation": "actions"}, {"operation": "messages"},
    *({"operation": "inventory", k: "foreign"} for k in (
        "organization_id", "team_id", "workspace_id", "query", "url", "headers", "path", "filter", "after", "cursor"))])
@pytest.mark.parametrize("name", [LINEAR, CUSTOMERIO])
def test_injected_or_mutating_arguments_deny_before_io(monkeypatch, name, args):
    monkeypatch.setattr(Path, "open", lambda *a, **k: pytest.fail("invalid args opened credentials"))
    with pytest.raises(ContextDenied):
        reads.execute(name, json.dumps(args), lambda: pytest.fail("validation must precede guard"))


@pytest.mark.parametrize("field,value", [("id", "foreign"), ("name", "Other"), ("urlKey", "foreign")])
@pytest.mark.parametrize("operation", ["inventory", "issues_summary"])
def test_foreign_organization_is_unavailable(harness, field, value, operation):
    install, run, calls, _ = harness
    install(lambda op, raw, req, n: raw["data"]["organization"].__setitem__(field, value))
    assert json.loads(run(LINEAR, operation))["status"] == "unavailable"
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [("id", "foreign"), ("key", "OTHER")])
@pytest.mark.parametrize("operation", ["inventory", "issues_summary"])
def test_foreign_team_uuid_or_key_is_unavailable(harness, field, value, operation):
    install, run, _, _ = harness
    def mutate(op, raw, req, n):
        connection = raw["data"]["teams" if op == "inventory" else "issues"]
        row = connection["nodes"][0]
        (row if op == "inventory" else row["team"])[field] = value
    install(mutate)
    assert json.loads(run(LINEAR, operation))["status"] == "unavailable"


@pytest.mark.parametrize("mutation", ["foreign", "missing", "duplicate", "wrong_name"])
@pytest.mark.parametrize("boundary", ["before", "after"])
@pytest.mark.parametrize("operation", dedicated.OPS[CUSTOMERIO])
def test_exact_workspace_set_is_required_before_and_after(harness, mutation, boundary, operation):
    install, run, calls, _ = harness
    target = 1 if boundary == "before" else (2 if operation == "workspace_summary" else 3)
    def mutate(op, raw, req, n):
        if n != target:
            return
        rows = raw["workspaces"]
        if mutation == "foreign":
            rows.append({"id": "999", "name": "Other customer"})
        elif mutation == "missing":
            rows.pop()
        elif mutation == "duplicate":
            rows.append(rows[0])
        else:
            rows[0]["name"] = "Other customer"
    install(mutate)
    assert json.loads(run(CUSTOMERIO, operation))["status"] == "unavailable"
    assert len(calls) == target


@pytest.mark.parametrize("value", ["https://evil.invalid/agemo/issue/CON-123", "https://linear.app/other/issue/CON-123",
    "https://linear.app/agemo/issue/OTHER-123", "https://linear.app/agemo/issue/CON-123?secret=abc",
    "https://linear.app/agemo/issue/CON-123#secret", "https://linear.app@evil.invalid/agemo/issue/CON-123"])
def test_linear_foreign_or_sensitive_urls_never_escape(harness, value):
    install, run, _, _ = harness
    install(lambda op, raw, req, n: raw["data"]["issues"]["nodes"][0].__setitem__("url", value))
    assert json.loads(run())["status"] == "unavailable"


def test_title_redacts_contacts_tokens_and_urls_before_truncation(harness):
    install, run, _, _ = harness
    canaries = [TOKEN, "person@example.com", "+44 7700 900123", "sk_live_123456", "cus_private", "https://private.invalid/key"]
    title = "Fix Stripe " + " ".join(canaries)
    install(lambda op, raw, req, n: raw["data"]["issues"]["nodes"][0].__setitem__("title", title))
    receipt = run()
    assert json.loads(receipt)["status"] == "observation"
    assert "Fix Stripe" in receipt and "[redacted]" in receipt
    assert not any(value in receipt for value in canaries)


@pytest.mark.parametrize("failure", ["graphql_errors", "duplicate", "bad_date", "state", "oversize"])
def test_linear_rejects_partial_graphql_and_bad_shapes(harness, failure):
    install, run, _, _ = harness
    def mutate(op, raw, req, n):
        rows = raw["data"]["issues"]["nodes"]
        if failure == "graphql_errors":
            raw["errors"] = [{"message": PRIVATE}]
        elif failure == "duplicate":
            rows.append(rows[0])
        elif failure == "bad_date":
            rows[0]["createdAt"] = "2026-02-31T00:00:00Z"
        elif failure == "state":
            rows[0]["state"]["type"] = PRIVATE
        else:
            raw["extra"] = PRIVATE * 10000
    install(mutate)
    receipt = run()
    assert json.loads(receipt)["status"] == "unavailable" and PRIVATE not in receipt


@pytest.mark.parametrize("failure", [None, "repeated_cursor", "duplicate_issue", "foreign_second_page", "empty_page", "page_limit"])
def test_linear_bounded_pagination_revalidates_each_page(harness, failure):
    install, run, calls, _ = harness
    def mutate(op, raw, req, n):
        data = raw["data"]
        node = data["issues"]["nodes"][0]
        if failure != "duplicate_issue":
            node["id"] = f"00000000-0000-4000-8000-{n:012d}"
            node["identifier"] = f"CON-{n}"
            node["url"] = f"https://linear.app/agemo/issue/CON-{n}/slug"
        if failure == "foreign_second_page" and n == 2:
            data["organization"]["id"] = "foreign"
        if failure == "empty_page":
            data["issues"]["nodes"] = []
        data["issues"]["pageInfo"] = {"hasNextPage": n < 2 or failure in {"repeated_cursor", "page_limit", "empty_page"},
            "endCursor": "repeated" if failure == "repeated_cursor" else f"cursor-{n}"}
        if n > 1:
            assert json.loads(req.content)["variables"]["after"] == ("repeated" if failure == "repeated_cursor" else f"cursor-{n-1}")
    install(mutate)
    result = json.loads(run())
    if failure in {None, "page_limit"}:
        assert result["status"] == "observation"
        assert result["result"]["pagination_complete"] is (failure is None)
        assert result["result"]["matched_issues_scanned"] == len(calls)
        assert len(calls) == (2 if failure is None else dedicated.LINEAR_MAX_PAGES)
    else:
        assert result["status"] == "unavailable"


@pytest.mark.parametrize("operation", ["campaigns", "segments"])
@pytest.mark.parametrize("failure", ["next_url", "unknown_continuation", "oversize_list", "duplicate", "bad_number", "foreign_after_limit"])
def test_customerio_rejects_unsupported_pagination_and_bad_rows(harness, operation, failure):
    install, run, calls, _ = harness
    def mutate(op, raw, req, n):
        if op != operation:
            return
        if failure == "next_url":
            raw["next"] = "https://evil.invalid/steal"
        elif failure == "unknown_continuation":
            raw["page_info"] = {"next_cursor": "opaque"}
        elif failure == "oversize_list":
            raw[op] *= dedicated.CUSTOMERIO_LIST_LIMIT + 1
        elif failure == "duplicate":
            raw[op].append(raw[op][0])
        elif failure == "bad_number":
            raw[op][0]["created" if op == "campaigns" else "created_at"] = True
        else:
            original = raw[op][0]
            raw[op] = [{**original, "id": i + 1} for i in range(dedicated.DETAIL_LIMIT + 1)]
            raw[op][-1]["id"] = "foreign-private-id"
    install(mutate)
    assert json.loads(run(CUSTOMERIO, operation))["status"] == "unavailable"
    assert len(calls) == 2


@pytest.mark.parametrize("name,operation", [(LINEAR, "issues_summary"), (CUSTOMERIO, "campaigns")])
@pytest.mark.parametrize("mutation", ["delete", "rotate"])
@pytest.mark.parametrize("boundary", ["response", "release", "delivery"])
def test_credentials_are_sealed_through_response_release_and_delivery(harness, monkeypatch, name, operation, mutation, boundary):
    install, run, calls, files = harness
    def revoke():
        if mutation == "delete":
            files[name].unlink()
        else:
            files[name].write_text("replaced")
    if boundary == "response":
        install(lambda *args: revoke())
    if boundary == "release":
        original = reads.transport._normalized
        def normalized(data, depth=0):
            original(data, depth)
            if depth == 0:
                revoke()
        monkeypatch.setattr(reads.transport, "_normalized", normalized)
    if boundary == "delivery":
        receipt = run(name, operation)
        revoke()
        with pytest.raises(ContextDenied):
            receipt.validate_credentials()
    else:
        with pytest.raises(ContextDenied):
            run(name, operation)
    if boundary == "response":
        assert len(calls) == 1


@pytest.mark.parametrize("name,operation", [(LINEAR, "issues_summary"), (CUSTOMERIO, "campaigns")])
def test_revoked_turn_cannot_continue_after_first_response(harness, name, operation):
    install, run, calls, _ = harness
    revoked = False
    def mutate(*args):
        nonlocal revoked
        revoked = True
    def guard():
        if revoked:
            raise ContextDenied("revoked")
    install(mutate)
    with pytest.raises(ContextDenied, match="revoked"):
        run(name, operation, guard)
    assert len(calls) == 1


@pytest.mark.parametrize("name,operation", [(LINEAR, "issues_summary"), (CUSTOMERIO, "campaigns")])
@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_http_failures_are_safe_and_never_followed(harness, name, operation, status):
    install, run, calls, _ = harness
    install(status=status)
    receipt = run(name, operation)
    assert json.loads(receipt)["status"] == "unavailable"
    assert len(calls) == 1 and PRIVATE not in receipt and TOKEN not in receipt


@pytest.mark.parametrize("name,operation", [(LINEAR, "issues_summary"), (CUSTOMERIO, "campaigns")])
def test_ambient_credentials_cannot_replace_missing_dedicated_key(harness, name, operation):
    _, run, calls, files = harness
    files[name].write_text("AGEMO_LINEAR_API_KEY=wrong-secret" if name == LINEAR else json.dumps({
        "region": "eu", "base_url": dedicated.CUSTOMERIO_ORIGIN}))
    assert json.loads(run(name, operation))["status"] == "unavailable"
    assert calls == []


@pytest.mark.parametrize("field,value", [("region", "us"), ("base_url", "https://evil.invalid")])
def test_customerio_credential_metadata_cannot_change_origin(harness, field, value):
    _, run, calls, files = harness
    content = json.loads(files[CUSTOMERIO].read_text())
    content[field] = value
    files[CUSTOMERIO].write_text(json.dumps(content))
    assert json.loads(run(CUSTOMERIO, "campaigns"))["status"] == "unavailable"
    assert calls == []


@pytest.mark.parametrize("name", [LINEAR, CUSTOMERIO])
def test_binding_changes_cannot_clear_or_replace_identity_pins(harness, monkeypatch, name):
    _, run, calls, _ = harness
    monkeypatch.setattr(reads, "_BINDINGS", tuple(replace(b, resources=()) if b.name == name else b for b in reads.bindings()))
    assert json.loads(run(name, dedicated.OPS[name][0]))["status"] == "unavailable"
    assert calls == []


def test_documented_duplicate_issue_state_is_readable(harness):
    install, run, _, _ = harness
    install(lambda op, raw, req, n: raw["data"]["issues"]["nodes"][0]["state"].__setitem__("type", "duplicate"))
    result = json.loads(run())
    assert result["status"] == "observation"
    assert result["result"]["issues"][0]["state"] == "duplicate"


@pytest.mark.parametrize("operation", ["campaigns", "segments"])
def test_customerio_details_limit_is_explicit_and_all_records_are_validated(harness, operation):
    install, run, _, _ = harness
    def mutate(op, raw, req, n):
        if op == operation:
            row = raw[op][0]
            raw[op] = [{**row, "id": i + 1} for i in range(dedicated.DETAIL_LIMIT + 1)]
    install(mutate)
    result = json.loads(run(CUSTOMERIO, operation))["result"]
    assert result["records_observed"] == dedicated.DETAIL_LIMIT + 1
    assert len(result["rows"]) == dedicated.DETAIL_LIMIT
    assert result["details_truncated"] is True and result["pagination_complete"] is True


@pytest.mark.parametrize("value", [None, PRIVATE, {"injection": PRIVATE}])
def test_unknown_optional_segment_type_is_omitted(harness, value):
    install, run, _, _ = harness
    def mutate(op, raw, req, n):
        if op == "segments":
            raw[op][0]["type"] = value
    install(mutate)
    receipt = run(CUSTOMERIO, "segments")
    result = json.loads(receipt)
    assert result["status"] == "observation"
    assert "type" not in result["result"]["rows"][0] and PRIVATE not in receipt
