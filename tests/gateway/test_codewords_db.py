"""Fixed Postgres transport contracts using real protected-file loading."""
import json
import os
from types import SimpleNamespace

import certifi
import pytest

from gateway import client_context_codewords_db as db
from gateway.client_context_policy import ContextDenied

URI = "postgresql://codewords_workflow_readonly:secret-canary@db.example.test:5432/codewords?sslmode=require"


class Connection:
    def __init__(self):
        self.identity = ["codewords", db.EXPECTED_USER, db.EXPECTED_USER, "on", "on", True, *([False] * 7)]
        self.queries = []
        self.closed = False
        self.hook = lambda: None
        self.schemas = [("public",)]

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        self.hook()

    def fetchone(self):
        return self.identity

    def fetchmany(self, limit):
        assert limit == 17
        return self.schemas if self.queries[-1][0] == db.DISCOVERY_SQL else [("public", 12, 3, 1)]

    def close(self):
        self.closed = True


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for key in tuple(os.environ):
        if key.startswith("PG"):
            monkeypatch.delenv(key)
    path = tmp_path / "secrets" / db.CREDENTIAL_REF
    path.parent.mkdir(mode=0o700)
    path.write_text(URI, encoding="utf-8")
    path.chmod(0o600)
    resources = dict(host="db.example.test", port="5432", database="codewords",
                     user=db.EXPECTED_USER, schemas="public", sslrootcert=certifi.where())
    binding = SimpleNamespace(resources=tuple(resources.items()),
                              credentials=(("credential_file", str(path)),))
    connection, calls = Connection(), []

    def connect(**kwargs):
        calls.append(kwargs)
        return connection

    monkeypatch.setattr(db, "_connect", connect)
    return path, binding, connection, calls


def test_inventory_fixed_transport_and_rollback(setup):
    _, binding, conn, calls = setup
    output = db.inventory(binding, lambda: None)
    assert output["schemas"] == [dict(schema="public", table_count=12, view_count=3, materialized_view_count=1)]
    assert "secret-canary" not in json.dumps(output)
    options = calls[0]
    assert options["sslmode"] == "verify-full" and options["sslcertmode"] == "disable"
    assert options["gssencmode"] == "disable" and options["passfile"] == os.devnull
    assert options["require_auth"] == "password,md5,scram-sha-256"
    assert options["connect_timeout"] == 5 and options["autocommit"] is True
    assert "statement_timeout=5000" in options["options"]
    assert "default_transaction_read_only=on" in options["options"]
    assert [q for q, _ in conn.queries] == ["BEGIN READ ONLY", db.IDENTITY_SQL, db.INVENTORY_SQL, db.IDENTITY_SQL]
    assert conn.queries[2][1] == (["public"],) and conn.closed


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory", "foreign", "mode", "empty", "oversize", "malformed", "hardlink", "parent_symlink"])
def test_unsafe_file_fails_before_network(setup, monkeypatch, kind):
    path, binding, _, calls = setup
    if kind == "missing":
        path.unlink()
    elif kind == "symlink":
        target = path.with_suffix(".target")
        path.rename(target)
        path.symlink_to(target)
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "foreign":
        monkeypatch.setattr(db.os, "getuid", lambda: -1)
    elif kind == "mode":
        path.chmod(0o640)
    elif kind == "hardlink":
        os.link(path, path.with_suffix(".link"))
    elif kind == "parent_symlink":
        parent = path.parent
        target = parent.with_name("other")
        parent.rename(target)
        parent.symlink_to(target, target_is_directory=True)
    else:
        path.write_bytes({"empty": b"", "oversize": b"x" * (db.MAX_CREDENTIAL + 1), "malformed": b"secret-canary"}[kind])
    with pytest.raises(db.PostgresUnavailable) as error:
        db.inventory(binding, lambda: None)
    assert not calls and "secret-canary" not in str(error.value)


@pytest.mark.parametrize("index,value", [(0, "foreign"), (1, "admin"), (2, "admin"), (3, "off"), (4, "off"), (5, False)] + [(i, True) for i in range(6, 13)])
def test_each_identity_gate_prevents_inventory_and_discovery(setup, index, value):
    _, binding, conn, _ = setup
    conn.identity[index] = value
    with pytest.raises(ContextDenied):
        db.preflight(dict(binding.resources)["sslrootcert"], lambda: None)
    assert conn.closed
    assert not any(sql in {db.DISCOVERY_SQL, db.INVENTORY_SQL} for sql, _ in conn.queries)


@pytest.mark.parametrize("uri", [URI.replace("db.example.test", "foreign.example.test"), URI.replace("5432", "5433"), URI.replace("/codewords?", "/foreign?"), URI.replace(db.EXPECTED_USER, "admin"), URI.replace("sslmode=require", "sslmode=disable"), URI + "&options=secret-canary", URI + "&sslmode=require", URI.replace("secret-canary", "%xx"), URI + "#secret-canary", URI.replace("5432", "0"), URI.replace("secret-canary", "")])
def test_uri_mismatch_or_unsafe_options_never_connect(setup, uri):
    path, binding, _, calls = setup
    path.write_text(uri, encoding="utf-8")
    with pytest.raises(ContextDenied):
        db.inventory(binding, lambda: None)
    assert not calls


@pytest.mark.parametrize("key", ["PGHOSTADDR", "PGSERVICE", "PGPASSWORD", "PGSSLKEY", "PGOPTIONS"])
def test_ambient_pg_settings_deny(setup, monkeypatch, key):
    _, binding, _, calls = setup
    monkeypatch.setenv(key, "secret-canary")
    with pytest.raises(ContextDenied):
        db.inventory(binding, lambda: None)
    assert not calls


@pytest.mark.parametrize("boundary", ["connect", "query", "fetch", "close"])
def test_credential_change_discards_result_and_closes(setup, monkeypatch, boundary):
    path, binding, conn, calls = setup

    def change():
        path.write_text(URI.replace("secret-canary", "replacement-canary"), encoding="utf-8")

    if boundary == "connect":
        def connect(**kwargs):
            calls.append(kwargs)
            change()
            return conn
        monkeypatch.setattr(db, "_connect", connect)
    elif boundary == "query":
        conn.hook = change
    elif boundary == "fetch":
        def fetch():
            change()
            return conn.identity
        conn.fetchone = fetch
    else:
        def close():
            conn.closed = True
            change()
        conn.close = close
    with pytest.raises(ContextDenied):
        db.inventory(binding, lambda: None)
    assert conn.closed


@pytest.mark.parametrize("boundary", ["connect", "query"])
def test_cancellation_after_io_closes_and_never_queries_again(setup, monkeypatch, boundary):
    _, binding, conn, calls = setup
    cancelled = False

    def revoke():
        nonlocal cancelled
        cancelled = True

    def check():
        if cancelled:
            raise ContextDenied("cancelled")

    if boundary == "connect":
        def connect(**kwargs):
            revoke()
            return conn
        monkeypatch.setattr(db, "_connect", connect)
    else:
        conn.hook = revoke
    with pytest.raises(ContextDenied, match="cancelled"):
        db.inventory(binding, check)
    assert conn.closed and len(conn.queries) == (0 if boundary == "connect" else 1)


def test_preflight_derives_only_fixed_file_identities(setup):
    _, binding, conn, _ = setup
    result = db.preflight(dict(binding.resources)["sslrootcert"], lambda: None)
    assert result["status"] == "verified" and result["resources"] == dict(binding.resources)
    assert result["credentials"] == [list(pair) for pair in binding.credentials]
    assert [q for q, _ in conn.queries] == ["BEGIN READ ONLY", db.IDENTITY_SQL, db.DISCOVERY_SQL, db.INVENTORY_SQL, db.IDENTITY_SQL]
    assert "secret-canary" not in json.dumps(result)


@pytest.mark.parametrize("schemas", [[], [("public",)] * 17, [("secret-canary contact data",)], [("public",), ("public",)]])
def test_discovery_requires_bounded_valid_distinct_schemas(setup, schemas):
    _, binding, conn, _ = setup
    conn.schemas = schemas
    with pytest.raises(ContextDenied):
        db.preflight(dict(binding.resources)["sslrootcert"], lambda: None)
    assert conn.closed and db.INVENTORY_SQL not in [q for q, _ in conn.queries]


def test_invalid_ca_stops_before_network(setup, tmp_path):
    _, _, _, calls = setup
    ca = tmp_path / "invalid-ca.pem"
    ca.write_text("secret-canary", encoding="utf-8")
    with pytest.raises(db.PostgresUnavailable, match="ca_unavailable"):
        db.preflight(str(ca), lambda: None)
    assert not calls



def test_dispatcher_uses_real_file_backend_and_sanitizes_driver_failure(setup, monkeypatch):
    from dataclasses import replace
    from gateway import client_context_codewords_reads as reads

    _, binding, conn, _ = setup
    original = next(item for item in reads.bindings() if item.name == "codewords_postgres_read")
    candidate = replace(original, resources=binding.resources, credentials=binding.credentials, blocker="")
    monkeypatch.setattr(reads, "_BINDINGS", tuple(candidate if item == original else item for item in reads.bindings()))
    args = json.dumps({"operation": "inventory"})
    result = json.loads(reads.execute(original.name, args, lambda: None))
    assert result["status"] == "observation" and conn.closed

    def fail():
        raise RuntimeError("secret-canary " + URI)

    conn.closed = False
    conn.hook = fail
    output = reads.execute(original.name, args, lambda: None)
    assert json.loads(output)["status"] == "unavailable"
    assert "secret-canary" not in output and conn.closed


def test_credential_reference_cannot_select_another_file(setup):
    _, binding, _, calls = setup
    binding.credentials = (("credential_file", "/foreign/secret"),)
    with pytest.raises(ContextDenied):
        db.inventory(binding, lambda: None)
    assert not calls


def test_atomic_replacement_with_identical_contents_is_revocation(setup):
    path, binding, conn, _ = setup

    def replace():
        replacement = path.with_suffix(".replacement")
        replacement.write_text(URI, encoding="utf-8")
        replacement.chmod(0o600)
        replacement.replace(path)

    conn.hook = replace
    with pytest.raises(ContextDenied):
        db.inventory(binding, lambda: None)
    assert conn.closed
