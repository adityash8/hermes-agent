"""Offline source-refresh proposals and separately reviewed manifests; never activate.

The candidate root is a complete local copy of the registered source tree. Review
keeps grants, scope and status fixed. Source lifecycle dates require explicit review.
Changed approved/shared bytes need new operator attestations bound to the proposal digest. These are local evidence
references, not a claim that an external approval service was consulted.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from gateway.client_context_policy import (
    MAX_FILE,
    MAX_MANIFEST,
    ContextDenied,
    _reject_json_constant,
    _unique_object,
    absolute_path,
    directory_fd,
    fields,
    identifier,
    items,
    load_registry,
    read_file,
    require,
    snapshot,
    timestamp,
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def _read_json(path: str) -> dict:
    absolute_path(path)
    data, _ = read_file(str(Path(path).parent), Path(path).name, MAX_MANIFEST)
    return json.loads(data.decode(), object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)


def propose(manifest: str, candidate_root: str) -> dict:
    """Hash local candidate bytes without copying prior approval or sharing grants."""
    registry = load_registry(manifest)
    absolute_path(candidate_root)
    active, candidate = Path(registry.root), Path(candidate_root)
    require(active != candidate and active not in candidate.parents and candidate not in active.parents)
    hashes, changes = {}, []
    for sid, record in sorted(registry.sources.items()):
        old, _ = read_file(registry.root, record["path"], MAX_FILE)
        require(hashlib.sha256(old).hexdigest() == record["sha256"])
        data, _ = read_file(candidate_root, record["path"], MAX_FILE)
        require("\x00" not in data.decode("utf-8"))
        digest = hashlib.sha256(data).hexdigest()
        hashes[sid] = digest
        if digest != record["sha256"]:
            changes.append({
                "id": sid, "client": record["client"], "audiences": record["audiences"],
                "old_sha256": record["sha256"], "sha256": digest,
                "status": "proposed", "approval_evidence": None, "sharing_evidence": None,
            })
    require(changes)
    report = {"version": 1, "base_digest": registry.digest, "candidate_root": candidate_root,
              "source_hashes": hashes, "changes": changes, "activation": "not_performed"}
    report["candidate_digest"] = hashlib.sha256(_json_bytes(report)).hexdigest()
    require(load_registry(manifest) == registry)
    return report


def _write_separate(output: str, data: bytes, manifest: str, candidate_root: str, validate=None) -> None:
    """Publish exclusively through a no-follow directory, never replacing any file."""
    absolute_path(output)
    registry = load_registry(manifest)
    target = Path(output)
    require(target != Path(manifest))
    for root in (Path(registry.root), Path(candidate_root)):
        require(target != root and root not in target.parents)
    require(len(data) <= MAX_MANIFEST)
    temporary = f".client-refresh-{uuid.uuid4().hex}.json"
    with directory_fd(str(target.parent)) as parent:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if validate:
                validate(str(target.parent / temporary))
            # link is atomic and exclusive: unlike rename, it cannot replace an existing destination.
            os.link(temporary, target.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        finally:
            os.unlink(temporary, dir_fd=parent)


def write_proposal(manifest: str, candidate_root: str, output: str) -> dict:
    report = propose(manifest, candidate_root)
    _write_separate(output, _json_bytes(report), manifest, candidate_root)
    return report


def review(manifest: str, candidate_root: str, decisions: str, output: str) -> None:
    """Build a separate review artifact; no command promotes it or changes activation."""
    report = propose(manifest, candidate_root)
    decision = fields(_read_json(decisions), {"base_digest", "candidate_digest", "reviews"})
    require(decision["base_digest"] == report["base_digest"])
    require(decision["candidate_digest"] == report["candidate_digest"])
    registry = load_registry(manifest)
    require(registry.digest == report["base_digest"])
    by_id = {}
    for entry in items(decision["reviews"], 128, empty=False):
        fields(entry, {"id", "sha256", "review_evidence", "approval_evidence", "sharing_evidence",
                       "observed_at", "effective_at", "expires_at"})
        sid = identifier(entry["id"])
        require(sid not in by_id)
        identifier(entry["review_evidence"])
        by_id[sid] = entry
    require(set(by_id) == {c["id"] for c in report["changes"]})
    refreshed = copy.deepcopy({"version": 1, "root": candidate_root,
        "sources": list(registry.sources.values()), "routes": list(registry.routes.values()),
        "owner_private": list(registry.owners.values())})
    if registry.analytics:
        refreshed["analytics"] = [asdict(grant) for grant in registry.analytics]
    for record in refreshed["sources"]:
        sid = record["id"]
        if sid not in by_id:
            continue
        entry = by_id[sid]
        require(entry["sha256"] == report["source_hashes"][sid])
        for key, needed in (("approval_evidence", record["status"] == "approved"),
                            ("sharing_evidence", "shared" in record["audiences"])):
            if needed:
                identifier(entry[key])
                require(entry[key] != record[key])
            else:
                require(entry[key] is None)
            record[key] = entry[key]
        for key in ("observed_at", "effective_at", "expires_at"):
            reviewed = timestamp(entry[key])
            if key != "expires_at":
                require(reviewed >= timestamp(record[key]))
            record[key] = entry[key]
        record["sha256"] = entry["sha256"]

    def validate(path: str) -> None:
        staged = load_registry(path)
        for route in staged.routes.values():
            source = SimpleNamespace(platform="slack", scope_id=route["scope_id"], chat_id=route["chat_id"],
                user_id="offline-reviewer", chat_type="channel", is_bot=False, profile_route_rejected=False)
            snapshot(staged, source, "Validate reviewed source refresh.", include_history=True)
        # Re-read every registered candidate, including records excluded by lifecycle.
        require(propose(manifest, candidate_root) == report)
        require(load_registry(manifest) == registry)

    _write_separate(output, _json_bytes(refreshed), manifest, candidate_root, validate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("propose", "review"))
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--decisions")
    args = parser.parse_args(argv)
    try:
        if args.command == "propose":
            require(args.decisions is None)
            write_proposal(args.manifest, args.candidate_root, args.output)
        else:
            require(args.decisions is not None)
            review(args.manifest, args.candidate_root, args.decisions, args.output)
    except (ContextDenied, OSError, ValueError, UnicodeError):
        parser.exit(1, "Source refresh rejected; no activation performed.\n")
    print("Separate review artifact created; activation remains unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
