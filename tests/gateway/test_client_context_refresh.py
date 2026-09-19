"""Local refresh artifacts preserve explicit trust and cannot activate themselves."""

import copy
import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from gateway import client_context_refresh as refresh
from gateway.client_context_policy import ContextDenied, load_registry, snapshot


@pytest.fixture
def staged(tmp_path):
    base = tmp_path.resolve()
    active, candidate = base / "active", base / "candidate"
    active.mkdir()
    candidate.mkdir()
    (active / "decision.txt").write_text("PREVIOUS_APPROVED_SYNTHETIC")
    (candidate / "decision.txt").write_text("CHANGED_SYNTHETIC")
    record = {
        "id": "decision-a", "client": "client-a", "audiences": ["shared"], "path": "decision.txt",
        "sha256": hashlib.sha256((active / "decision.txt").read_bytes()).hexdigest(),
        "kind": "decision", "status": "approved", "source_ref": "source-a",
        "observed_at": "2020-01-01T00:00:00Z", "effective_at": "2020-01-01T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z", "approval_evidence": "old-approval",
        "sharing_evidence": "old-sharing", "decision_key": "budget", "supersedes": [],
    }
    route = {"scope_id": "workspace-a", "chat_id": "channel-a", "client": "client-a",
             "audience": "shared", "source_ids": ["decision-a"], "grant_evidence": "grant-a",
             "expires_at": "2099-01-01T00:00:00Z"}
    raw = {"version": 1, "root": str(active), "sources": [record], "routes": [route], "owner_private": []}
    manifest = base / "manifest.json"
    manifest.write_text(json.dumps(raw))
    report = refresh.propose(str(manifest), str(candidate))
    decisions = {"base_digest": report["base_digest"], "candidate_digest": report["candidate_digest"],
        "reviews": [{"id": "decision-a", "sha256": report["source_hashes"]["decision-a"],
                     "review_evidence": "operator-review", "approval_evidence": "new-approval",
                     "sharing_evidence": "new-sharing", "observed_at": "2021-01-01T00:00:00Z",
                     "effective_at": "2021-01-01T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z"}]}
    return SimpleNamespace(base=base, active=active, candidate=candidate, manifest=manifest,
                           raw=raw, report=report, decisions=decisions)


@pytest.mark.parametrize("status", ["approved", "observation", "proposed"])
def test_refresh_cli_requires_review_and_emits_separate_validated_manifest(staged, status):
    staged.raw["sources"][0]["status"] = status
    if status != "approved":
        staged.raw["sources"][0]["approval_evidence"] = None
        staged.decisions["reviews"][0]["approval_evidence"] = None
    staged.manifest.write_text(json.dumps(staged.raw))
    staged.report = refresh.propose(str(staged.manifest), str(staged.candidate))
    staged.decisions.update(base_digest=staged.report["base_digest"],
                            candidate_digest=staged.report["candidate_digest"])
    before = staged.manifest.read_bytes(), (staged.active / "decision.txt").read_bytes()
    proposal = staged.base / "proposal.json"
    result = subprocess.run([sys.executable, "-m", "gateway.client_context_refresh", "propose",
        "--manifest", str(staged.manifest), "--candidate-root", str(staged.candidate),
        "--output", str(proposal)], capture_output=True, text=True, check=True)
    assert "activation remains unchanged" in result.stdout
    candidate = json.loads(proposal.read_text())
    assert candidate == staged.report
    assert candidate["changes"][0]["status"] == "proposed"
    assert candidate["changes"][0]["approval_evidence"] is None
    assert candidate["changes"][0]["sharing_evidence"] is None
    decisions = staged.base / "decisions.json"
    decisions.write_text(json.dumps(staged.decisions))
    output = staged.base / "reviewed.json"
    result = subprocess.run([sys.executable, "-m", "gateway.client_context_refresh", "review",
        "--manifest", str(staged.manifest), "--candidate-root", str(staged.candidate),
        "--decisions", str(decisions), "--output", str(output)], capture_output=True, text=True, check=True)
    assert "activation remains unchanged" in result.stdout
    refreshed = load_registry(str(output))
    source = SimpleNamespace(platform="slack", scope_id="workspace-a", chat_id="channel-a",
                             user_id="user-a", chat_type="channel", is_bot=False, profile_route_rejected=False)
    assert "CHANGED_SYNTHETIC" in snapshot(refreshed, source, "What changed?").packet
    assert refreshed.routes == load_registry(str(staged.manifest)).routes
    assert refreshed.sources["decision-a"]["status"] == status
    expected_approval = "new-approval" if status == "approved" else None
    assert refreshed.sources["decision-a"]["approval_evidence"] == expected_approval
    assert refreshed.sources["decision-a"]["sharing_evidence"] == "new-sharing"
    assert before == (staged.manifest.read_bytes(), (staged.active / "decision.txt").read_bytes())


@pytest.mark.parametrize("invalid", ["no-review", "old-approval", "old-sharing", "wrong-hash", "extra-source",
    "changed-candidate", "changed-grant", "expired-grant", "expired-source", "candidate-symlink",
    "candidate-hardlink", "root-symlink", "root-escape", "active-output", "source-output", "candidate-output",
    "existing-output", "output-parent-symlink", "duplicate-json", "status-override", "grant-override",
    "during-validation-manifest", "during-validation-candidate", "during-validation-active",
    "ungranted-unreviewed-source", "historical-expired"])
def test_refresh_rejects_unreviewed_or_changed_inputs_without_touching_active_state(staged, invalid, monkeypatch):
    decisions = copy.deepcopy(staged.decisions)
    output = staged.base / "reviewed.json"
    decision_path = staged.base / "decisions.json"
    root = staged.candidate
    if invalid == "no-review":
        decisions["reviews"] = []
    elif invalid in {"old-approval", "old-sharing"}:
        field = "approval_evidence" if invalid == "old-approval" else "sharing_evidence"
        decisions["reviews"][0][field] = invalid
    elif invalid == "wrong-hash":
        decisions["reviews"][0]["sha256"] = "0" * 64
    elif invalid == "extra-source":
        decisions["reviews"][0]["id"] = "client-b-secret"
    elif invalid == "changed-candidate":
        (root / "decision.txt").write_text("UNREVIEWED_SYNTHETIC")
    elif invalid in {"changed-grant", "expired-grant", "expired-source"}:
        if invalid == "changed-grant":
            staged.raw["routes"][0]["grant_evidence"] = "another-grant"
        elif invalid == "expired-grant":
            staged.raw["routes"][0]["expires_at"] = "2021-01-01T00:00:00Z"
        else:
            staged.raw["sources"][0]["expires_at"] = "2021-01-01T00:00:00Z"
            decisions["reviews"][0].update(observed_at="2020-01-01T00:00:00Z",
                effective_at="2020-01-01T00:00:00Z", expires_at="2021-01-01T00:00:00Z")
        staged.manifest.write_text(json.dumps(staged.raw))
        if invalid != "changed-grant":
            report = refresh.propose(str(staged.manifest), str(root))
            decisions.update(base_digest=report["base_digest"], candidate_digest=report["candidate_digest"])
    elif invalid in {"candidate-symlink", "candidate-hardlink"}:
        (root / "decision.txt").unlink()
        if invalid == "candidate-symlink":
            (root / "decision.txt").symlink_to(staged.active / "decision.txt")
        else:
            os.link(staged.active / "decision.txt", root / "decision.txt")
    elif invalid == "root-symlink":
        root = staged.base / "linked-root"
        root.symlink_to(staged.candidate, target_is_directory=True)
    elif invalid == "root-escape":
        root = f"{staged.candidate}/../candidate"
    elif invalid == "active-output":
        output = staged.manifest
    elif invalid == "source-output":
        output = staged.active / "new.txt"
    elif invalid == "candidate-output":
        output = staged.candidate / "new.txt"
    elif invalid == "existing-output":
        output.write_text("DO_NOT_REPLACE")
    elif invalid == "output-parent-symlink":
        linked = staged.base / "linked-output"
        linked.symlink_to(staged.base, target_is_directory=True)
        output = linked / "reviewed.json"
    elif invalid in {"status-override", "grant-override"}:
        if invalid == "status-override":
            decisions["reviews"][0]["status"] = "approved"
        else:
            decisions["routes"] = []
    elif invalid in {"ungranted-unreviewed-source", "historical-expired"}:
        extra = copy.deepcopy(staged.raw["sources"][0])
        extra.update(id="other-source", path="other.txt", decision_key="other-budget")
        (staged.active / "other.txt").write_text("OTHER_SYNTHETIC")
        (root / "other.txt").write_text("OTHER_SYNTHETIC")
        extra["sha256"] = hashlib.sha256((staged.active / "other.txt").read_bytes()).hexdigest()
        if invalid == "ungranted-unreviewed-source":
            (root / "other.txt").write_text("UNREVIEWED_OTHER_SYNTHETIC")
        else:
            extra.update(decision_key="budget", expires_at="2021-01-01T00:00:00Z")
            staged.raw["sources"][0].update(effective_at="2022-01-01T00:00:00Z", supersedes=[extra["id"]])
            decisions["reviews"][0].update(effective_at="2022-01-01T00:00:00Z")
            staged.raw["routes"][0]["source_ids"].append(extra["id"])
        staged.raw["sources"].append(extra)
        staged.manifest.write_text(json.dumps(staged.raw))
        report = refresh.propose(str(staged.manifest), str(root))
        decisions.update(base_digest=report["base_digest"], candidate_digest=report["candidate_digest"])
    decision_path.write_text(json.dumps(decisions))
    if invalid == "duplicate-json":
        decision_path.write_text('{"base_digest":"x",' + decision_path.read_text()[1:])
    before = staged.manifest.read_bytes(), (staged.active / "decision.txt").read_bytes()
    if invalid.startswith("during-validation-"):
        original_snapshot = refresh.snapshot

        def change_after_snapshot(*args, **kwargs):
            result = original_snapshot(*args, **kwargs)
            if invalid == "during-validation-manifest":
                staged.raw["routes"] = []
                staged.manifest.write_text(json.dumps(staged.raw))
            elif invalid == "during-validation-candidate":
                (root / "decision.txt").write_text("RACE_CANDIDATE_SYNTHETIC")
            else:
                (staged.active / "decision.txt").write_text("RACE_ACTIVE_SYNTHETIC")
            return result

        monkeypatch.setattr(refresh, "snapshot", change_after_snapshot)
    previous_output = output.read_bytes() if output.is_file() else None
    with pytest.raises((ContextDenied, OSError)):
        refresh.review(str(staged.manifest), str(root), str(decision_path), str(output))
    if not invalid.startswith("during-validation-"):
        assert before == (staged.manifest.read_bytes(), (staged.active / "decision.txt").read_bytes())
    assert (output.read_bytes() if output.is_file() else None) == previous_output
