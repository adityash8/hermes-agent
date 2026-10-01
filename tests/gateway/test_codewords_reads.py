"""Execution contracts: fixed transport targets, normalization, and revocation."""

from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import certifi
import httpx
import pytest

from gateway import client_context_codewords_db as db
from gateway import client_context_codewords_reads as reads
from gateway.client_context_policy import ContextDenied

DAY = (datetime.now(timezone.utc).date() - timedelta(days=4)).isoformat()
DATE_ARGS = json.dumps({"start_date": DAY, "end_date": DAY})
INVENTORY_ARGS = '{"operation":"inventory"}'


def bind(monkeypatch, name, resources, **kwargs):
    binding = reads.Binding(name, tuple(resources.items()), blocker="", **kwargs)
    monkeypatch.setattr(reads, "_BINDINGS", tuple(binding if b.name == name else b for b in reads.bindings()))
    return binding


def fake_http(monkeypatch, handler, *, mock_secret=True):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, json=handler(request))

    monkeypatch.setattr(reads.transport, "_http_client", lambda: httpx.Client(
        transport=httpx.MockTransport(send), follow_redirects=False, trust_env=False))
    if mock_secret:
        monkeypatch.setattr(reads, "_credential_snapshot", lambda binding: ())
        monkeypatch.setattr(reads, "_secret", lambda binding, suffix, guard: (guard(), "secret-canary")[1])
    return calls


def body(request):
    return json.loads(request.content)


def test_schemas_match_immutable_bindings():
    schemas = reads.schemas()
    assert {s["function"]["name"] for s in schemas} == {b.name for b in reads.bindings()}
    for schema in schemas:
        params = schema["function"]["parameters"]
        assert params["additionalProperties"] is False
        properties = set(params["properties"])
        if schema["function"]["name"] == "codewords_stripe_read":
            assert {"comparison_start_date", "comparison_end_date"} <= properties
            assert not {"comparison_start_date", "comparison_end_date"} & set(params["required"])
            properties -= {"comparison_start_date", "comparison_end_date"}
        assert properties in ({"start_date", "end_date", "operation"}, {"operation"})
        assert tuple(params["properties"]["operation"]["enum"]) == reads.OPERATIONS[schema["function"]["name"]]
    with pytest.raises(FrozenInstanceError):
        reads.bindings()[0].blocker = "changed"
    schemas[0]["function"]["parameters"]["properties"]["account_id"] = {}
    assert "account_id" not in reads.schemas()[0]["function"]["parameters"]["properties"]


@pytest.mark.parametrize("name,arguments", [
    ("terminal", '{}'), ("codewords_posthog_write", DATE_ARGS),
    (reads.NAMES[0], '{"start_date":"2020-01-01","start_date":"2020-01-02","end_date":"2020-01-03"}'),
    (reads.NAMES[0], '{"start_date":"2020-01-01","end_date":"2020-02-01"}'),
    (reads.NAMES[0], '{"start_date":"2999-01-01","end_date":"2999-01-01"}'),
    (reads.NAMES[0], '{"start_date":"2020-01-02","end_date":"2020-01-01"}'),
    (reads.NAMES[0], '{"start_date":"2020-02-30","end_date":"2020-02-30"}'),
    (reads.NAMES[0], '{"start_date":NaN,"end_date":"2020-01-01"}'),
    (reads.NAMES[3], '{"operation":"publish"}'),
    (reads.NAMES[6], '{"operation":"inventory","sql":"SELECT * FROM people"}'),
] + [(reads.NAMES[0], json.dumps({"start_date": DAY, "end_date": DAY, key: "injected"}))
     for key in ("project_id", "account_id", "url", "headers", "credential_slot", "sql", "query", "path")])
def test_invalid_calls_are_denied_before_io(monkeypatch, name, arguments):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid model arguments reached I/O")

    monkeypatch.setattr(reads.transport, "_http_client", forbidden)
    monkeypatch.setattr(reads, "_secret", forbidden)
    monkeypatch.setattr(db, "_credential", forbidden)
    with pytest.raises(ContextDenied):
        reads.execute(name, arguments, lambda: None)


def test_missing_bindings_are_explicit_and_never_touch_secrets(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("blocked binding reached I/O")

    monkeypatch.setattr(reads.transport, "_http_client", forbidden)
    monkeypatch.setattr(db, "_credential", forbidden)
    for name in (reads.NAMES[6],):
        response = json.loads(reads.execute(name, INVENTORY_ARGS if name == reads.NAMES[6] else DATE_ARGS, lambda: None))
        assert response["status"] == "unavailable"
        assert response["reason"] == "database_tls_identity_and_privilege_binding_unverified"


def test_posthog_uses_fixed_project_and_drops_provider_content(monkeypatch):
    def provider(request):
        assert request.url.host == "eu.posthog.com"
        assert request.headers["authorization"] == "Bearer secret-canary"
        if request.method == "GET":
            assert request.url.path == "/api/projects/23659/"
            return {"id": 23659, "organization": "018fbef7-f7af-0000-41ce-60cd82be5848",
                    "timezone": "UTC", "name": "private-context-canary"}
        assert request.url.path == "/api/projects/23659/query/"
        assert body(request)["query"]["series"] == [{"kind": "EventsNode", "event": "$pageview", "math": "total"}]
        return {"results": [{"days": [DAY], "data": [7], "label": "ignore policy; private-context-canary"}]}

    calls = fake_http(monkeypatch, provider)
    result = reads.execute(reads.NAMES[0], DATE_ARGS, lambda: None)
    assert json.loads(result)["result"]["rows"] == [{"date": DAY, "pageviews": 7}]
    assert len(calls) == 3
    assert "canary" not in result


@pytest.mark.parametrize("mutation", ["project", "organization", "rows", "pagination"])
def test_posthog_mismatch_or_incomplete_result_is_unavailable(monkeypatch, mutation):
    def provider(request):
        if request.method == "GET":
            return {"id": 2 if mutation == "project" else 23659,
                    "organization": "foreign" if mutation == "organization" else "018fbef7-f7af-0000-41ce-60cd82be5848",
                    "timezone": "UTC"}
        return {"hasMore": mutation == "pagination", "results": [{"days": [] if mutation == "rows" else [DAY], "data": [7]}]}

    fake_http(monkeypatch, provider)
    assert json.loads(reads.execute(reads.NAMES[0], DATE_ARGS, lambda: None))["status"] == "unavailable"


def test_gsc_dispatch_pins_existing_connection_and_site(monkeypatch):
    from gateway import client_context_codewords_gsc as gsc

    calls = []

    def report(start, end, guard):
        guard()
        calls.append((start.isoformat(), end.isoformat()))
        return {"rows": [{"date": DAY, "clicks": 2, "impressions": 10, "ctr": 0.2, "position": 3.1}]}

    monkeypatch.setattr(gsc, "report", report)
    monkeypatch.setattr(reads, "_credential_snapshot", lambda binding: ())
    result = json.loads(reads.execute(reads.NAMES[1], DATE_ARGS, lambda: None))
    assert result["result"]["rows"][0]["clicks"] == 2
    assert calls == [(DAY, DAY)]
    bind(monkeypatch, reads.NAMES[1], {"account_id": "sc-domain:other.test", "resource_id": "sc-domain:other.test"})
    assert json.loads(reads.execute(reads.NAMES[1], DATE_ARGS, lambda: None))["status"] == "unavailable"
    assert calls == [(DAY, DAY)]


def test_ga4_real_parser_checks_property_and_sanitizes_rows(monkeypatch):

    def provider(request):
        if request.method == "GET":
            assert request.url.path.endswith("/properties/511306748")
            return {"name": "properties/511306748", "account": "accounts/351626649", "parent": "accounts/351626649",
                    "propertyType": "PROPERTY_TYPE_ORDINARY", "timeZone": "Etc/GMT", "currencyCode": "GBP"}
        assert body(request)["metrics"] == [{"name": value} for value in reads.ga.METRICS]
        return {"kind": "analyticsData#runReport", "dimensionHeaders": [{"name": "date"}],
                "metricHeaders": [{"name": value, "type": "TYPE_INTEGER"} for value in reads.ga.METRICS],
                "metadata": {"timeZone": "Etc/GMT", "currencyCode": "GBP"}, "rowCount": 1,
                "rows": [{"dimensionValues": [{"value": DAY.replace("-", "")}],
                          "metricValues": [{"value": str(v)} for v in (2, 3, 4)]}]}

    fake_http(monkeypatch, provider)
    result = json.loads(reads.execute(reads.NAMES[2], DATE_ARGS, lambda: None))
    assert result["result"]["rows"] == [{"date": DAY, "sessions": 2, "activeUsers": 3, "screenPageViews": 4}]


def test_gtm_inventory_never_returns_tag_content(monkeypatch):
    def provider(request):
        container = "186967704" if "186967704" in request.url.path else "233991397"
        path = "accounts/6234246789/containers/" + container
        result = {"accountId": "6234246789", "containerId": container, "path": path,
                  "publicId": "GTM-KFMB8N6L" if container == "186967704" else "GTM-T6KD93DR"}
        if request.url.path.endswith("versions:live"):
            result.update(containerVersionId="5", path=path + "/versions/5", container=dict(result), tag=[{
                "accountId": "6234246789", "containerId": container, "name": "secret-canary",
                "parameter": [{"key": "html", "value": "secret-canary"}]}])
        return result

    calls = fake_http(monkeypatch, provider)
    output = reads.execute(reads.NAMES[3], INVENTORY_ARGS, lambda: None)
    assert len(calls) == 4
    assert all(call.url.path.endswith("/versions:live") for call in calls)
    assert all("parameter" not in call.url.params["fields"] and "tag(accountId,containerId)" in call.url.params["fields"] for call in calls)
    assert "canary" not in output
    assert [v["tag_count"] for v in json.loads(output)["result"]["containers"]] == [1, 1]


def test_google_ads_real_adapter_never_accepts_model_gaql(monkeypatch):

    def provider(request):
        assert "login-customer-id" not in request.headers
        assert request.url.path == "/v24/customers/9855813343/googleAds:search"
        customer = {"id": "9855813343", "resourceName": "customers/9855813343", "currencyCode": "GBP", "timeZone": "America/Los_Angeles", "manager": False}
        query = body(request)["query"]
        assert "FROM customer" in query
        if "segments.date" in query:
            assert "LIMIT 31" in query
            return {"results": [{"customer": customer, "segments": {"date": DAY},
                                 "metrics": {"impressions": "7", "clicks": "2", "costMicros": "500"}}]}
        return {"results": [{"customer": customer}]}

    fake_http(monkeypatch, provider)
    result = json.loads(reads.execute(reads.NAMES[4], DATE_ARGS, lambda: None))
    assert result["result"]["rows"][0]["cost_micros"] == 500


def test_meta_rejects_foreign_binding_before_io(monkeypatch):
    bind(monkeypatch, reads.NAMES[5], {"account_id": "123", "resource_id": "456", "currency": "USD"})
    calls = fake_http(monkeypatch, lambda request: pytest.fail("foreign Meta binding reached HTTP"))
    result = json.loads(reads.execute(reads.NAMES[5], DATE_ARGS, lambda: None))
    assert result["status"] == "unavailable" and not calls


def test_binding_revocation_before_dispatch_and_after_http_denies_release(monkeypatch):
    original = reads.bindings()
    calls = fake_http(monkeypatch, lambda request: {})

    def check():
        if not calls:
            monkeypatch.setattr(reads, "_BINDINGS", tuple(replace(b, blocker="revoked") for b in original))

    with pytest.raises(ContextDenied):
        reads.execute(reads.NAMES[0], DATE_ARGS, check)
    assert not calls

    monkeypatch.setattr(reads, "_BINDINGS", original)

    def provider(request):
        monkeypatch.setattr(reads, "_BINDINGS", tuple(replace(b, blocker="revoked") for b in original))
        return {"id": 23659, "organization": "018fbef7-f7af-0000-41ce-60cd82be5848", "timezone": "UTC"}

    calls = fake_http(monkeypatch, provider)
    with pytest.raises(ContextDenied):
        reads.execute(reads.NAMES[0], DATE_ARGS, lambda: None)
    assert len(calls) == 1


def test_network_error_is_sanitized_and_cancellation_is_not_swallowed(monkeypatch):
    def provider(request):
        raise httpx.ReadTimeout("credential secret-canary; private-context-canary")

    fake_http(monkeypatch, provider)
    result = reads.execute(reads.NAMES[0], DATE_ARGS, lambda: None)
    assert json.loads(result)["status"] == "unavailable"
    assert "canary" not in result
    with pytest.raises(ContextDenied):
        reads.execute(reads.NAMES[0], DATE_ARGS, lambda: (_ for _ in ()).throw(ContextDenied("cancelled")))


class FakeConnection:
    def __init__(self, identity):
        self.identity = identity
        self.queries = []
        self.closed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchone(self):
        return self.identity

    def fetchmany(self, limit):
        assert limit == 17
        return [("public", 12, 3, 1)]

    def close(self):
        self.closed = True


def db_setup(monkeypatch, tmp_path, identity=None):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for key in tuple(os.environ):
        if key.startswith("PG"):
            monkeypatch.delenv(key)
    path = db.credential_path()
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text("postgresql://codewords_workflow_readonly:secret-canary@db.example.test:5432/codewords?sslmode=require",
                    encoding="utf-8")
    path.chmod(0o600)
    binding = bind(monkeypatch, reads.NAMES[6], {
        "host": "db.example.test", "port": "5432", "database": "codewords", "user": db.EXPECTED_USER,
        "schemas": "public", "sslrootcert": certifi.where()},
        credentials=(("credential_file", str(path)),))
    connection = FakeConnection(identity or ("codewords", db.EXPECTED_USER, db.EXPECTED_USER,
                                            "on", "on", True, *([False] * 7)))
    received = {}

    def connect(**kwargs):
        received.update(kwargs)
        return connection

    monkeypatch.setattr(db, "_connect", connect)
    return binding, connection, received


def test_database_uses_verified_tls_fixed_sql_and_read_only_transaction(monkeypatch, tmp_path):
    _, connection, received = db_setup(monkeypatch, tmp_path)
    output = reads.execute(reads.NAMES[6], INVENTORY_ARGS, lambda: None)
    result = json.loads(output)
    assert result["status"] == "observation"
    assert result["result"]["schemas"][0]["table_count"] == 12
    assert received["sslmode"] == "verify-full"
    assert received["sslcertmode"] == "disable"
    assert received["gssencmode"] == "disable"
    assert received["require_auth"] == "password,md5,scram-sha-256"
    assert received["passfile"] == db.os.devnull
    assert received["sslrootcert"] == certifi.where()
    assert received["connect_timeout"] == 5
    assert "default_transaction_read_only=on" in received["options"]
    assert [sql for sql, _ in connection.queries] == ["BEGIN READ ONLY", db.IDENTITY_SQL, db.INVENTORY_SQL, db.IDENTITY_SQL]
    assert connection.queries[2][1] == (["public"],)
    assert connection.closed and "secret-canary" not in output


@pytest.mark.parametrize("index,value", [(0, "other-client"), (1, "admin"), (2, "admin"), (3, "off"),
                                         (4, "off"), (5, False)] + [(i, True) for i in range(6, 13)])
def test_database_identity_or_write_privilege_denies_inventory(monkeypatch, tmp_path, index, value):
    identity = ["codewords", db.EXPECTED_USER, db.EXPECTED_USER, "on", "on", True, *([False] * 7)]
    identity[index] = value
    _, connection, _ = db_setup(monkeypatch, tmp_path, tuple(identity))
    result = reads.execute(reads.NAMES[6], INVENTORY_ARGS, lambda: None)
    assert json.loads(result)["status"] == "unavailable"
    assert db.INVENTORY_SQL not in [sql for sql, _ in connection.queries]
    assert connection.closed


@pytest.mark.parametrize("uri", [
    "postgresql://codewords_workflow_readonly:x@foreign.example.test/codewords",
    "postgresql://admin:x@db.example.test/codewords",
    "postgresql://codewords_workflow_readonly:x@db.example.test/other-client",
    "postgresql://codewords_workflow_readonly:x@db.example.test/codewords?sslmode=disable",
    "postgresql://codewords_workflow_readonly:x@db.example.test/codewords?options=-c%20role=admin",
])
def test_database_bad_dsn_is_denied_before_connection(monkeypatch, tmp_path, uri):
    _, _, received = db_setup(monkeypatch, tmp_path)
    db.credential_path().write_text(uri, encoding="utf-8")
    result = reads.execute(reads.NAMES[6], INVENTORY_ARGS, lambda: None)
    assert json.loads(result)["status"] == "unavailable"
    assert not received


def test_real_credential_file_does_not_expand_environment_or_release_secrets(monkeypatch, tmp_path):
    secret_file = tmp_path / "posthog.env"
    monkeypatch.setenv("PRIVATE_OWNER_TOKEN", "owner-secret-canary")
    secret_file.write_text('POSTHOG_PERSONAL_API_KEY="${PRIVATE_OWNER_TOKEN}"\n', encoding="utf-8")
    original = reads.bindings()[0]
    monkeypatch.setattr(reads, "_BINDINGS", (replace(original, credentials=(
        ("env_file", str(secret_file)), ("access_token_key", "POSTHOG_PERSONAL_API_KEY"))), *reads.bindings()[1:]))
    calls = fake_http(monkeypatch, lambda request: {}, mock_secret=False)
    output = reads.execute(reads.NAMES[0], DATE_ARGS, lambda: None)
    assert not calls
    assert json.loads(output)["status"] == "unavailable"
    secret_file.write_text("POSTHOG_PERSONAL_API_KEY=project-token-canary\n", encoding="utf-8")

    def provider(request):
        assert request.headers["authorization"] == "Bearer project-token-canary"
        if request.method == "GET":
            return {"id": 23659, "organization": "018fbef7-f7af-0000-41ce-60cd82be5848", "timezone": "UTC"}
        return {"results": [{"days": [DAY], "data": [3]}]}

    fake_http(monkeypatch, provider, mock_secret=False)
    output = reads.execute(reads.NAMES[0], DATE_ARGS, lambda: None)
    assert json.loads(output)["status"] == "observation"
    assert "canary" not in output


def test_incomplete_expired_oauth_denies_without_io_or_file_write(monkeypatch, tmp_path):
    token_file = tmp_path / "oauth.json"
    before = json.dumps({"token": "expired-token-canary", "refresh_token": "refresh-canary",
                         "token_uri": "https://oauth2.googleapis.com/token", "expiry": "2020-01-01T00:00:00Z"})
    token_file.write_text(before, encoding="utf-8")
    binding = reads.bindings()[3]
    monkeypatch.setattr(reads, "_BINDINGS", tuple(replace(b, credentials=(("oauth_file", str(token_file)),))
                                                  if b == binding else b for b in reads.bindings()))
    calls = fake_http(monkeypatch, lambda request: {}, mock_secret=False)
    result = reads.execute(reads.NAMES[3], INVENTORY_ARGS, lambda: None)
    assert json.loads(result)["status"] == "unavailable"
    assert not calls and token_file.read_text(encoding="utf-8") == before
    assert "canary" not in result


@pytest.mark.parametrize("status,content,headers", [
    (302, b"", {"location": "https://foreign.invalid/steal"}),
    (200, b'{"id":23659,"id":123}', {"content-type": "application/json"}),
    (200, b"x" * 65537, {"content-type": "application/json"}),
    (200, b"private-canary", {"content-type": "text/html"}),
])
def test_bounded_transport_rejects_redirect_duplicate_json_and_oversize(monkeypatch, status, content, headers):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(status, content=content, headers=headers)

    monkeypatch.setattr(reads.transport, "_http_client", lambda: httpx.Client(
        transport=httpx.MockTransport(send), follow_redirects=False, trust_env=False))
    monkeypatch.setattr(reads, "_secret", lambda *args: "secret-canary")
    monkeypatch.setattr(reads, "_credential_snapshot", lambda binding: ())
    output = reads.execute(reads.NAMES[0], DATE_ARGS, lambda: None)
    assert json.loads(output)["status"] == "unavailable"
    assert len(calls) == 1 and "canary" not in output


def test_database_file_ref_mismatch_and_cancellation_are_preconnection(monkeypatch, tmp_path):
    binding, connection, received = db_setup(monkeypatch, tmp_path)
    with monkeypatch.context() as mismatch:
        mismatch.setattr(reads, "_BINDINGS", tuple(replace(b, credentials=(("credential_file", "/foreign/credential"),))
                                                 if b == binding else b for b in reads.bindings()))
        secret_calls = []
        mismatch.setattr(db, "_credential", lambda: secret_calls.append(True))
        assert json.loads(reads.execute(reads.NAMES[6], INVENTORY_ARGS, lambda: None))["status"] == "unavailable"
        assert not secret_calls and not received

    _, connection, received = db_setup(monkeypatch, tmp_path)

    def check():
        if connection.queries and connection.queries[-1][0] == db.IDENTITY_SQL:
            raise ContextDenied("cancelled")

    with pytest.raises(ContextDenied):
        reads.execute(reads.NAMES[6], INVENTORY_ARGS, check)
    assert received and connection.closed
    assert db.INVENTORY_SQL not in [sql for sql, _ in connection.queries]


@pytest.mark.parametrize("key", ["PGHOSTADDR", "PGSERVICE", "PGSSLKEY", "PGPASSWORD"])
def test_database_refuses_ambient_libpq_configuration_before_file_read(monkeypatch, tmp_path, key):
    _, _, received = db_setup(monkeypatch, tmp_path)
    monkeypatch.setenv(key, "foreign-context-canary")
    secret_calls = []
    monkeypatch.setattr(db, "_credential", lambda: secret_calls.append(True))
    output = reads.execute(reads.NAMES[6], INVENTORY_ARGS, lambda: None)
    assert json.loads(output)["status"] == "unavailable"
    assert not secret_calls and not received
    assert "canary" not in output


def test_expired_pinned_oauth_refresh_is_memory_only_and_errors_are_operator_safe(monkeypatch, tmp_path, caplog):
    from urllib.parse import parse_qs

    token_file = tmp_path / "oauth.json"
    raw = {"token": "expired-canary", "refresh_token": "refresh+&=canary",
           "client_id": "client-canary", "client_secret": "client+&=canary",
           "token_uri": "https://oauth2.googleapis.com/token", "expiry": "2020-01-01T00:00:00Z"}
    before = json.dumps(raw)
    token_file.write_text(before, encoding="utf-8")
    binding = reads.bindings()[3]
    monkeypatch.setattr(reads, "_BINDINGS", tuple(replace(b, credentials=(("oauth_file", str(token_file)),))
                                                  if b == binding else b for b in reads.bindings()))
    requests = []
    revoked = False

    def send(request):
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            assert request.url.path == "/token" and request.method == "POST"
            assert parse_qs(request.content.decode()) == {
                "grant_type": ["refresh_token"], "client_id": [raw["client_id"]],
                "client_secret": [raw["client_secret"]], "refresh_token": [raw["refresh_token"]]}
            if revoked:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "refresh+&=canary"})
            return httpx.Response(200, json={"access_token": "new-token-canary", "token_type": "Bearer", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer new-token-canary"
        assert request.url.host == "tagmanager.googleapis.com"
        assert request.url.path.endswith("/versions:live")
        container = "186967704" if "186967704" in request.url.path else "233991397"
        return httpx.Response(200, json={
            "accountId": "6234246789", "containerId": container, "containerVersionId": "5",
            "path": "accounts/6234246789/containers/" + container + "/versions/5",
            "container": {"accountId": "6234246789", "containerId": container,
                          "publicId": "GTM-KFMB8N6L" if container == "186967704" else "GTM-T6KD93DR"}})

    monkeypatch.setattr(reads.transport, "_http_client", lambda: httpx.Client(
        transport=httpx.MockTransport(send), follow_redirects=False, trust_env=False))
    output = reads.execute(reads.NAMES[3], INVENTORY_ARGS, lambda: None)
    assert json.loads(output)["status"] == "observation"
    assert len(requests) == 5
    assert token_file.read_text() == before
    revoked = True
    requests.clear()
    output += reads.execute(reads.NAMES[3], INVENTORY_ARGS, lambda: None)
    assert len(requests) == 1
    assert "oauth_refresh_failed" in output and "invalid_grant" in caplog.text
    assert "status=400" in caplog.text
    assert "canary" not in output + caplog.text
    assert token_file.read_text() == before


@pytest.mark.parametrize("field,value", [("accountId", "other"), ("containerId", "other"),
                                         ("containerVersionId", "different"), ("deleted", True)])
def test_gtm_after_read_identity_change_denies_output(monkeypatch, field, value):
    count = 0

    def provider(request):
        nonlocal count
        count += 1
        raw = {"accountId": "6234246789", "containerId": "186967704", "containerVersionId": "5",
               "path": "accounts/6234246789/containers/186967704/versions/5",
               "container": {"accountId": "6234246789", "containerId": "186967704", "publicId": "GTM-KFMB8N6L"}}
        if count == 2:
            raw[field] = value
        return raw

    calls = fake_http(monkeypatch, provider)
    result = json.loads(reads.execute(reads.NAMES[3], INVENTORY_ARGS, lambda: None))
    assert result["status"] == "unavailable" and len(calls) == 2
