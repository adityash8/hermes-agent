"""Strict local grants and immutable evidence snapshots for stateless client Q&A."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

MAX_MANIFEST = 262144
MAX_FILE = 65536
MAX_PACKET = 98304
MAX_QUESTION = 4000
ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}\Z")
SOURCE_FIELDS = {
    "id", "client", "audiences", "path", "sha256", "kind", "status", "source_ref",
    "observed_at", "effective_at", "expires_at", "approval_evidence", "sharing_evidence",
    "decision_key", "supersedes",
}
ROUTE_FIELDS = {"scope_id", "chat_id", "client", "audience", "source_ids", "grant_evidence", "expires_at"}
OWNER_FIELDS = {"scope_id", "chat_id", "user_id", "chat_type", "grant_evidence", "expires_at"}


class ContextDenied(ValueError):
    """A policy or evidence check failed. Never expose exception details to a peer."""


class ContextChanged(ContextDenied):
    """An admitted scoped turn lost authorization; discard its output entirely."""


def require(condition: object) -> None:
    if not condition:
        raise ContextDenied("client context unavailable")


def identifier(value: object) -> str:
    require(isinstance(value, str) and ID.fullmatch(value))
    return str(value)


def fields(value: object, expected: set[str]) -> dict:
    require(isinstance(value, dict) and set(value) == expected)
    return value  # type: ignore[return-value]


def items(value: object, maximum: int, *, empty: bool = True) -> list:
    require(isinstance(value, list) and (0 if empty else 1) <= len(value) <= maximum)
    return value  # type: ignore[return-value]


def identifiers(value: object, maximum: int, *, empty: bool = True) -> list[str]:
    result = [identifier(v) for v in items(value, maximum, empty=empty)]
    require(len(set(result)) == len(result))
    return result


def timestamp(value: object) -> datetime:
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value))
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ContextDenied("invalid timestamp") from exc


def citation(value: object) -> str:
    require(isinstance(value, str) and len(value) <= 512)
    if ID.fullmatch(str(value)):
        return str(value)
    require(not re.search(r"[\s<>\[\]\\]", str(value)))
    url = urlsplit(str(value))
    require(url.scheme == "https" and url.hostname and not url.username and not url.password)
    require(not url.query and not url.fragment)
    return str(value)


def relative_path(value: object) -> str:
    require(isinstance(value, str) and 0 < len(value) <= 240)
    require(all(re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9_. -]{0,79}", p) for p in value.split("/")))
    require(not PurePosixPath(value).is_absolute() and all(p not in {".", ".."} for p in value.split("/")))
    return value


def absolute_path(value: object) -> str:
    require(isinstance(value, str) and len(value) <= 1024 and value.startswith("/"))
    require("\x00" not in value and "\\" not in value)
    require(all(p not in {"", ".", ".."} for p in value.split("/")[1:]))
    return value


@contextmanager
def directory_fd(path: str):
    """Walk every directory without following symlinks, including root ancestors."""
    absolute_path(path)
    require(os.name == "posix" and hasattr(os, "O_NOFOLLOW"))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in path.split("/")[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def file_identity(st: os.stat_result) -> tuple:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink


def read_file(root: str, relative: str, limit: int) -> tuple[bytes, tuple]:
    """Descriptor-relative bounded read; reject links, devices and mid-read changes."""
    parts = relative.split("/")
    with directory_fd(root) as root_fd:
        parent = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= limit)
                data = stream.read(limit + 1)
                after = os.fstat(stream.fileno())
                require(len(data) == before.st_size and file_identity(before) == file_identity(after))
                return data, file_identity(after)
        finally:
            os.close(parent)


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def _reject_json_constant(value: str):
    raise ContextDenied("nonfinite JSON")


@dataclass(frozen=True)
class Options:
    manifest: str
    provider: str
    model: str
    tool_generation: str = ""
    read_tools: tuple[str, ...] = ()


def options(value: object) -> Options | None:
    # None is malformed. Only absence (handled by GatewayConfig) or explicit false is off.
    if isinstance(value, dict) and set(value) == {"enabled"} and value["enabled"] is False:
        return None
    expected = {"enabled", "manifest", "provider", "model"}
    if isinstance(value, dict) and "read_tools" in value:
        expected.add("read_tools")
    cfg = fields(value, expected)
    require(cfg["enabled"] is True)
    require(cfg["provider"] in {"openai", "openai-codex", "openrouter"})
    require(isinstance(cfg["model"], str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,159}", cfg["model"]))
    require(cfg["model"].lower() not in {"auto", "default"})
    generation, allowed = "", ()
    if "read_tools" in cfg:
        try:
            tool_policy = fields(cfg["read_tools"], {"generation", "allow"})
            generation = identifier(tool_policy["generation"])
            allowed = tuple(sorted(identifiers(tool_policy["allow"], 16)))
        except ContextDenied:
            generation, allowed = "", ()  # Malformed policy grants no capability.
    return Options(absolute_path(cfg["manifest"]), cfg["provider"], cfg["model"], generation, allowed)


@dataclass(frozen=True)
class Registry:
    path: str
    digest: str
    identity: tuple
    root: str
    sources: dict[str, dict]
    routes: dict[tuple[str, str], dict]
    owners: dict[tuple[str, str], dict]
    analytics: tuple = ()


def load_registry(path: str) -> Registry:
    absolute_path(path)
    data, identity = read_file(str(Path(path).parent), Path(path).name, MAX_MANIFEST)
    raw = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
    expected = {"version", "root", "routes", "owner_private", "sources"}
    if isinstance(raw, dict) and "analytics" in raw:
        expected.add("analytics")
    fields(raw, expected)
    require(type(raw["version"]) is int and raw["version"] == 1)
    root = absolute_path(raw["root"])
    sources = {}
    for record in items(raw["sources"], 128):
        fields(record, SOURCE_FIELDS)
        sid = identifier(record["id"])
        require(sid not in sources)
        identifier(record["client"])
        audiences = identifiers(record["audiences"], 2, empty=False)
        require(set(audiences) <= {"internal", "shared"})
        relative_path(record["path"])
        require(isinstance(record["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]))
        require(record["kind"] in {"brief", "research", "decision", "metric"})
        require(record["status"] in {"observation", "proposed", "approved", "unresolved"})
        citation(record["source_ref"])
        observed, effective, expires = (timestamp(record[k]) for k in ("observed_at", "effective_at", "expires_at"))
        require(observed < expires and effective < expires)
        if record["kind"] == "decision":
            identifier(record["decision_key"])
        else:
            require(record["decision_key"] is None and record["status"] != "approved")
        if record["status"] == "approved":
            identifier(record["approval_evidence"])
        else:
            require(record["approval_evidence"] is None)
        if "shared" in audiences:
            identifier(record["sharing_evidence"])
        else:
            require(record["sharing_evidence"] is None)
        identifiers(record["supersedes"], 16)
        sources[sid] = record

    # Explicit relationships, never "newest wins". Validate even ungranted metadata.
    for record in sources.values():
        for sid in record["supersedes"]:
            require(sid in sources)
            old = sources[sid]
            require(all(record[k] == old[k] for k in ("client", "kind", "decision_key")))
            require(set(record["audiences"]) == set(old["audiences"]))
            require(timestamp(record["effective_at"]) > timestamp(old["effective_at"]))
            require(old["status"] != "approved" or record["status"] == "approved")
            require(record["status"] != "unresolved")
    # Strictly increasing effective_at along edges also rules out cycles.
    routes, owners = {}, {}
    for route in items(raw["routes"], 64):
        fields(route, ROUTE_FIELDS)
        key = (identifier(route["scope_id"]), identifier(route["chat_id"]))
        require(key not in routes)
        identifier(route["client"])
        require(route["audience"] in {"internal", "shared"})
        identifier(route["grant_evidence"])
        timestamp(route["expires_at"])
        for sid in identifiers(route["source_ids"], 16, empty=False):
            require(sid in sources)
            record = sources[sid]
            require(record["client"] == route["client"] and route["audience"] in record["audiences"])
        routes[key] = route
    for owner in items(raw["owner_private"], 8):
        fields(owner, OWNER_FIELDS)
        key = (identifier(owner["scope_id"]), identifier(owner["chat_id"]))
        require(key not in routes and key not in owners)
        identifier(owner["user_id"])
        identifier(owner["grant_evidence"])
        require(owner["chat_type"] == "dm")
        timestamp(owner["expires_at"])
        owners[key] = owner
    from gateway.client_context_analytics import parse_grants

    analytics = parse_grants(raw.get("analytics", []), routes)
    return Registry(path, hashlib.sha256(data).hexdigest(), identity, root, sources, routes, owners, analytics)


def authorize(registry: Registry, source, now: datetime) -> dict | None:
    require(getattr(source.platform, "value", source.platform) == "slack")
    key = (identifier(source.scope_id), identifier(source.chat_id))
    identifier(source.user_id)
    require(source.chat_type in {"dm", "group", "channel", "thread"})
    require(not source.is_bot and not source.profile_route_rejected)
    if key in registry.owners:
        owner = registry.owners[key]
        require(source.chat_type == owner["chat_type"] and source.user_id == owner["user_id"])
        require(now < timestamp(owner["expires_at"]))
        return None
    require(key in registry.routes)
    route = registry.routes[key]
    require(now < timestamp(route["expires_at"]))
    return route


@dataclass(frozen=True)
class Snapshot:
    registry_digest: str
    registry_identity: tuple
    file_identities: tuple
    packet: str
    history_packet: str = ""


def snapshot(registry: Registry, source, question: str, now: datetime | None = None,
             *, include_history: bool = False) -> Snapshot:
    now = now or datetime.now(timezone.utc)
    route = authorize(registry, source, now)
    require(route is not None)
    from gateway.client_context_analytics import route_grants

    route_grants(registry, source, now)
    require(isinstance(question, str) and 0 < len(question.encode("utf-8")) <= MAX_QUESTION)
    require(not any(ord(c) < 32 and c not in "\n\t\r" for c in question))
    granted = set(route["source_ids"])
    effective = {sid for sid in granted if timestamp(registry.sources[sid]["effective_at"]) <= now}
    # An effective successor outside this grant must not let this audience treat an old
    # decision as current. Grant the successor explicitly or remove the obsolete source.
    superseded = set()
    for sid, record in registry.sources.items():
        if timestamp(record["effective_at"]) <= now:
            targets = set(record["supersedes"]) & granted
            require(not targets or sid in granted)
            superseded.update(targets)
    current = effective - superseded
    approved = set()
    all_superseded = {
        old for record in registry.sources.values() if timestamp(record["effective_at"]) <= now
        for old in record["supersedes"]
    }
    for sid, record in registry.sources.items():
        if (record["client"] == route["client"] and route["audience"] in record["audiences"]
                and record["status"] == "approved" and sid not in all_superseded
                and timestamp(record["effective_at"]) <= now < timestamp(record["expires_at"])):
            key = record["decision_key"]
            require(key not in approved)
            approved.add(key)
    historical = {
        sid for sid in effective - current
        if include_history and registry.sources[sid]["kind"] == "decision"
        and registry.sources[sid]["status"] == "approved"
    }
    for sid in current | historical:
        record = registry.sources[sid]
        require(timestamp(record["observed_at"]) <= now < timestamp(record["expires_at"]))
    # All grant/lifecycle checks above precede the FIRST source-content read.
    records, history, identities = [], [], []
    for sid in sorted(current | historical):
        record = registry.sources[sid]
        data, identity = read_file(registry.root, record["path"], MAX_FILE)
        require(hashlib.sha256(data).hexdigest() == record["sha256"])
        content = data.decode("utf-8")
        require("\x00" not in content)
        safe = {k: record[k] for k in ("id", "kind", "status", "source_ref", "observed_at", "effective_at", "expires_at", "decision_key")}
        safe["freshness"] = "historical; needs_live_verification" if record["kind"] == "metric" else "dated_evidence"
        safe["content"] = content
        if sid in current:
            records.append(safe)
        if include_history and record["kind"] == "decision" and record["status"] == "approved":
            history.append({**safe, "lifecycle": "current" if sid in current else "superseded"})
        identities.append((sid, identity))
    packet = json.dumps({
        "evidence": records,
        "limitations": (
            "local source metrics are historical and not live-verified; no global transcripts; future records unavailable; "
            "only supplied bounded conversation history and granted approved decision history via scoped tools"
            if include_history else
            "no live verification; excluded historical and future records are unavailable; no conversation history"
        ),
        "question": question,
    }, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    require(len(packet.encode("utf-8")) <= MAX_PACKET)
    history_packet = json.dumps(history, ensure_ascii=True, sort_keys=True) if include_history else ""
    require(len(packet.encode("utf-8")) + len(history_packet.encode("utf-8")) <= MAX_PACKET)
    return Snapshot(registry.digest, registry.identity, tuple(identities), packet, history_packet)


def revalidate(opts: Options, original: Snapshot, source, question: str) -> None:
    current = snapshot(load_registry(opts.manifest), source, question,
                       include_history=bool(opts.tool_generation))
    require(current == original)
