"""Fixed read-only PostgreSQL inventory, disabled until its identity is verified."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import ssl
import stat
from urllib.parse import parse_qsl, unquote, urlsplit

from gateway.client_context_policy import ContextDenied, require
from hermes_constants import get_hermes_home

EXPECTED_USER = "codewords_workflow_readonly"
CREDENTIAL_REF = "codewords-postgres-readonly.url"
MAX_CREDENTIAL = 16384

IDENTITY_SQL = """
SELECT current_database(), current_user, session_user,
       current_setting('transaction_read_only'),
       current_setting('default_transaction_read_only'),
       (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()),
       EXISTS (SELECT 1 FROM pg_roles WHERE pg_has_role(current_user, oid, 'MEMBER')
               AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)),
       EXISTS (SELECT 1 FROM pg_roles r WHERE pg_has_role(current_user, r.oid, 'MEMBER')
               AND has_database_privilege(r.oid, current_database(), 'CREATE')),
       EXISTS (SELECT 1 FROM pg_roles r WHERE pg_has_role(current_user, r.oid, 'MEMBER')
               AND has_database_privilege(r.oid, current_database(), 'TEMP')),
       EXISTS (SELECT 1 FROM pg_namespace CROSS JOIN pg_roles r
               WHERE pg_has_role(current_user, r.oid, 'MEMBER') AND nspname !~ '^pg_' AND nspname <> 'information_schema'
                 AND has_schema_privilege(r.oid, pg_namespace.oid, 'CREATE')),
       EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN pg_roles r
               WHERE pg_has_role(current_user, r.oid, 'MEMBER') AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
                 AND c.relkind IN ('r','p','v','m','f')
                 AND has_table_privilege(r.oid, c.oid,
                     'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')),
       EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN pg_roles r
               WHERE pg_has_role(current_user, r.oid, 'MEMBER') AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
                 AND c.relkind='S'
                 AND has_sequence_privilege(r.oid, c.oid, 'USAGE,UPDATE')),
       EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace CROSS JOIN pg_roles r
               WHERE pg_has_role(current_user, r.oid, 'MEMBER') AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
                 AND has_function_privilege(r.oid, p.oid, 'EXECUTE'))
"""
INVENTORY_SQL = """
SELECT n.nspname, count(*) FILTER (WHERE c.relkind IN ('r','p')),
       count(*) FILTER (WHERE c.relkind='v'), count(*) FILTER (WHERE c.relkind='m')
FROM pg_namespace n LEFT JOIN pg_class c ON c.relnamespace=n.oid
  AND c.relkind IN ('r','p','v','m') AND has_table_privilege(current_user, c.oid, 'SELECT')
WHERE n.nspname = ANY(%s)
GROUP BY n.nspname ORDER BY n.nspname LIMIT 17
"""


DISCOVERY_SQL = """
SELECT nspname FROM pg_namespace
WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
  AND has_schema_privilege(current_user, oid, 'USAGE')
ORDER BY nspname LIMIT 17
"""


class PostgresUnavailable(ContextDenied):
    """Only a fixed category may cross the operator boundary."""


def credential_path():
    return get_hermes_home() / "secrets" / CREDENTIAL_REF


def _file_identity(metadata):
    # Reads may update atime; only stable security/content metadata seals the file.
    return (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode,
            metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _credential():
    # Directory-relative opens also reject a symlinked secrets directory.
    try:
        with_directory = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        home = os.open(get_hermes_home(), with_directory)
        try:
            directory = os.open("secrets", with_directory, dir_fd=home)
        finally:
            os.close(home)
        try:
            metadata = os.fstat(directory)
            require(metadata.st_uid == os.getuid() and not metadata.st_mode & 0o022)
            fd = os.open(CREDENTIAL_REF, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                    and stat.S_IMODE(before.st_mode) in {0o400, 0o600}
                    and before.st_nlink == 1 and 0 < before.st_size <= MAX_CREDENTIAL)
            raw = handle.read(MAX_CREDENTIAL + 1)
            after = os.fstat(handle.fileno())
            require(_file_identity(before) == _file_identity(after) and len(raw) == before.st_size)
        uri = raw.decode("utf-8").removesuffix("\n")
        _parse_uri(uri)
        fingerprint = (*_file_identity(before), hashlib.sha256(raw).digest())
        return uri, fingerprint
    except Exception:
        raise PostgresUnavailable("credential_file_unavailable") from None


def _parse_uri(value):
    try:
        require(isinstance(value, str) and value and not re.search(r"[\s\x00-\x1f\x7f]", value)
                and not re.search(r"%(?![0-9a-fA-F]{2})", value))
        uri = urlsplit(value)
        require(uri.scheme in {"postgres", "postgresql"} and not uri.fragment)
        host, user = uri.hostname or "", unquote(uri.username or "", errors="strict")
        database = unquote(uri.path.removeprefix("/"), errors="strict")
        password = unquote(uri.password or "", errors="strict")
        port = uri.port if uri.port is not None else 5432
        require(re.fullmatch(r"[a-z0-9.-]+", host) and 1 <= port <= 65535
                and re.fullmatch(r"[a-zA-Z0-9_-]{1,63}", database)
                and user and password and "\x00" not in password)
        pairs = parse_qsl(uri.query, keep_blank_values=True, strict_parsing=True)
        query = dict(pairs)
        require(len(pairs) == len(query) and set(query) <= {"sslmode"})
        # Existing exported URIs may say require; transport always upgrades to verify-full.
        require(query.get("sslmode", "verify-full") in {"require", "verify-ca", "verify-full"})
        return {"host": host, "port": str(port), "database": database, "user": user}, password
    except Exception:
        raise PostgresUnavailable("credential_uri_invalid") from None


def _connect(**kwargs):
    from importlib import import_module

    return import_module("psycopg").connect(**kwargs)


def _schemas(names):
    require(1 <= len(names) <= 16 and len(set(names)) == len(names)
            and all(isinstance(name, str) and re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", name)
                    for name in names))
    return tuple(names)


def _ca(path):
    try:
        require(isinstance(path, str) and Path(path).is_absolute() and Path(path).is_file())
        ssl.create_default_context(cafile=path)
    except Exception:
        raise PostgresUnavailable("ca_unavailable") from None


def inventory(binding, guard):
    r = dict(binding.resources)
    require(binding.credentials == (("credential_file", str(credential_path())),))
    require(set(r) == {"host", "port", "database", "user", "schemas", "sslrootcert"})
    schemas = _schemas(r["schemas"].split(","))
    return _run(r, guard, schemas)[0]


def preflight(ca_path, guard):
    """Operator-only: derive a candidate from the approved file, never caller identities."""
    guard()
    uri, fingerprint = _credential()
    resources, _ = _parse_uri(uri)
    resources["sslrootcert"] = ca_path
    result, schemas = _run(resources, guard, None, expected_fingerprint=fingerprint)
    resources["schemas"] = ",".join(schemas)
    return {"status": "verified", "resources": resources,
            "credentials": [["credential_file", str(credential_path())]], "result": result}


def _run(r, check, schemas, expected_fingerprint=None):
    require(not any(key.startswith("PG") and value for key, value in os.environ.items()))
    check()
    uri, fingerprint = _credential()
    require(expected_fingerprint is None or fingerprint == expected_fingerprint)
    identity_resources, password = _parse_uri(uri)
    if identity_resources["user"] != EXPECTED_USER:
        raise PostgresUnavailable("expected_readonly_role_required")
    require(all(r.get(key) == value for key, value in identity_resources.items()))
    _ca(r["sslrootcert"])

    def guard():
        check()
        require(_credential()[1] == fingerprint)
        require(not any(key.startswith("PG") and value for key, value in os.environ.items()))
        check()

    guard()
    connection = _connect(host=r["host"], port=int(r["port"]), dbname=r["database"],
        user=r["user"], password=password, sslmode="verify-full", sslrootcert=r["sslrootcert"],
        sslcertmode="disable", gssencmode="disable", require_auth="password,md5,scram-sha-256",
        passfile=os.devnull, connect_timeout=5, options="-c default_transaction_read_only=on -c statement_timeout=5000 "
                                   "-c lock_timeout=2000 -c search_path=pg_catalog", autocommit=True)
    try:
        guard()
        with connection.cursor() as cursor:
            def query(sql, params=None):
                guard()
                cursor.execute(sql, params)
                guard()

            query("BEGIN READ ONLY")

            def identity():
                query(IDENTITY_SQL)
                row = cursor.fetchone()
                guard()
                require(type(row) in (tuple, list) and len(row) == 13)
                require(tuple(row[:5]) == (r["database"], r["user"], r["user"], "on", "on"))
                require(row[5] is True)
                if not all(value is False for value in row[6:]):
                    raise PostgresUnavailable("readonly_role_privileges_required")

            identity()
            if schemas is None:
                query(DISCOVERY_SQL)
                rows = cursor.fetchmany(17)
                guard()
                require(all(type(row) in (tuple, list) and len(row) == 1 for row in rows))
                schemas = _schemas([row[0] for row in rows])
            query(INVENTORY_SQL, (list(schemas),))
            rows = cursor.fetchmany(17)
            guard()
            require(len(rows) == len(schemas))
            result, seen = [], set()
            for row in rows:
                require(type(row) in (tuple, list) and len(row) == 4 and row[0] in schemas
                        and row[0] not in seen)
                seen.add(row[0])
                require(all(type(count) is int and 0 <= count <= 1000000 for count in row[1:]))
                result.append(dict(zip(("schema", "table_count", "view_count", "materialized_view_count"), row)))
            identity()
            guard()
            output = {"schemas": result, "transaction_read_only": True, "tls_verified": True}
    finally:
        # Closing rolls back even on cancellation; never commit a transaction.
        connection.close()
    guard()
    return output, schemas
