"""Real skill tool/CLI contracts for canonical packages and profile projections."""

import json
from pathlib import Path

import pytest
import hermes_yaml as yaml


def _content(name, body="Original instructions."):
    return f"---\nname: {name}\ndescription: Use when checking skill routing.\n---\n\n{body}\n"


def _package(root, directory, name=None):
    package = root / directory
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(_content(name or package.name), encoding="utf-8")
    return package


def _config(home, **skills):
    (home / "config.yaml").write_text(yaml.safe_dump({"skills": skills}), encoding="utf-8")
    from agent.skill_utils import _external_dirs_cache_clear
    from tools.skills_tool import _SKILLS_CACHE
    _external_dirs_cache_clear()
    _SKILLS_CACHE.clear()


def _tool(tool, **args):
    import tools.skill_manager_tool  # noqa: F401 — register real handlers
    import tools.skills_tool  # noqa: F401
    from tools.registry import registry
    result = registry.dispatch(tool, args)
    assert isinstance(result, str)
    return json.loads(result)


def _manage(action, name, **kwargs):
    return _tool("skill_manage", action=action, name=name, **kwargs)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _config(home)
    return home


@pytest.mark.parametrize("nested", [False, True])
def test_linked_package_identity_approval_and_rollback(home, tmp_path, nested):
    from agent.skill_utils import iter_skill_index_files
    from hermes_cli.write_approval_commands import handle_pending_subcommand

    canonical = tmp_path / "canonical"
    package = _package(canonical, "vendor-dir", "offered-name")
    original = (package / "SKILL.md").read_text(encoding="utf-8")
    local = home / "skills"
    local.mkdir()
    projection = local / ("category" if nested else "vendor-dir")
    projection.symlink_to(canonical if nested else package, target_is_directory=True)
    name = "category/vendor-dir" if nested else "vendor-dir"
    # Same physical package through both roots is not a name collision.
    _config(home, write_approval=True)
    assert "offered-name" in {s["name"] for s in _tool("skills_list")["skills"]}
    assert _tool("skill_view", name=name)["success"]
    staged = _manage("patch", name, old_string="Original", new_string="Approved")
    assert staged.get("staged"), staged
    assert (package / "SKILL.md").read_text(encoding="utf-8") == original
    handle_pending_subcommand("skills", ["approve", staged["pending_id"]])
    assert "Approved instructions" in (package / "SKILL.md").read_text(encoding="utf-8")
    assert projection.is_symlink()
    _config(home, external_dirs=[str(canonical)])
    assert _manage("write_file", "offered-name", file_path="references/note.md", file_content="Support.")["success"]
    assert _manage("patch", name, file_path="references/note.md", old_string="Support", new_string="Updated")["success"]
    assert "Updated." in _tool("skill_view", name=name, file_path="references/note.md")["content"]

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.md").write_text("Untouched", encoding="utf-8")
    escape = package / "references" / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    for action, args in [
        ("write_file", {"file_content": "Bad"}),
        ("patch", {"old_string": "Untouched", "new_string": "Bad"}),
        ("remove_file", {}),
    ]:
        denied = _manage(action, name, file_path="references/escape/keep.md", **args)
        assert not denied["success"], denied
    assert (outside / "keep.md").read_text(encoding="utf-8") == "Untouched"
    before = (package / "SKILL.md").read_bytes()
    failed = _tool("skill_manage", operations=[
        {"action": "patch", "name": name, "old_string": "Approved", "new_string": "Temporary"},
        {"action": "patch", "name": name, "old_string": "missing exact passage", "new_string": "No"},
    ])
    assert not failed["success"] and failed["completed_before_failure"] == 1, failed
    assert (package / "SKILL.md").read_bytes() == before
    assert projection.is_symlink() and escape.is_symlink()
    renamed = _tool("skill_manage", operations=[
        {"action": "patch", "name": "offered-name", "content": _content("renamed-skill")},
        {"action": "patch", "name": "offered-name", "old_string": "Missing passage", "new_string": "No"},
    ])
    assert not renamed["success"] and renamed["completed_before_failure"] == 1, renamed
    assert (package / "SKILL.md").read_bytes() == before
    assert projection.is_symlink() and escape.is_symlink()
    assert not _manage("delete", name)["success"]
    assert (package / "SKILL.md").read_bytes() == before

    duplicate = _package(local, "different-dir", "offered-name")
    assert not _tool("skill_view", name="offered-name")["success"]
    collision = _manage("patch", "offered-name", old_string="Original", new_string="Wrong")
    assert not collision["success"] and "Ambiguous" in collision["error"], collision
    assert "Original" in (duplicate / "SKILL.md").read_text(encoding="utf-8")
    assert (package / "SKILL.md").read_bytes() == before
    assert not _manage("create", "offered-name", content=_content("offered-name"))["success"]
    assert not _manage("patch", "../canonical/vendor-dir", old_string="Approved", new_string="Wrong")["success"]

    hidden = _package(package, "references/hidden")
    assert not _manage("patch", "hidden", old_string="Original", new_string="Wrong")["success"]
    assert "Original" in (hidden / "SKILL.md").read_text(encoding="utf-8")
    protected = _package(local, "protected-alias", "hermes-agent")
    assert not _manage("delete", "protected-alias")["success"]
    assert (protected / "SKILL.md").exists()
    malicious = _package(local, "file-escape")
    (malicious / "SKILL.md").unlink()
    (malicious / "SKILL.md").symlink_to(outside / "keep.md")
    assert not _manage("edit", "file-escape", content=_content("file-escape"))["success"]
    (local / "create-escape").symlink_to(outside, target_is_directory=True)
    assert not _manage("create", "new", category="create-escape", content=_content("new"))["success"]
    assert not (outside / "new").exists()
    assert (outside / "keep.md").read_text(encoding="utf-8") == "Untouched"
    (local / "loop").symlink_to(local, target_is_directory=True)
    paths = list(iter_skill_index_files(local, "SKILL.md"))
    assert len(paths) == len({p.resolve() for p in paths})


def test_creation_discovery_and_native_import_ownership(home, tmp_path, monkeypatch, capsys):
    from hermes_cli import skills_hub as cli_hub
    from tools.skills_hub import HubLockFile
    from tools.skills_hub_official import OptionalSkillSource

    canonical = tmp_path / "canonical"
    _config(home, create_dir=str(canonical))
    created = _manage("create", "new-skill", content=_content("new-skill"))
    assert created["success"], created
    assert (canonical / "new-skill" / "SKILL.md").is_file()
    assert not (home / "skills" / "new-skill").exists()
    assert _tool("skill_view", name="new-skill")["success"]
    assert "new-skill" in {s["name"] for s in _tool("skills_list")["skills"]}

    # Native imports intentionally use the primary root, not create_dir. A
    # primary-root projection is an OUT-OF-BAND topology choice, tested only here.
    local = home / "skills"
    local.mkdir(exist_ok=True)
    assert not list(local.rglob("SKILL.md"))
    local.rename(home / "previous-metadata")
    local.symlink_to(canonical, target_is_directory=True)
    vendor = tmp_path / "vendor-input"
    upstream = _package(vendor, "imported")
    monkeypatch.setenv("HERMES_OPTIONAL_SKILLS", str(vendor))
    source = OptionalSkillSource()
    monkeypatch.setattr(cli_hub, "_sources", lambda: [source])
    from tools import skills_hub_search
    monkeypatch.setattr(skills_hub_search, "create_source_router", lambda **kwargs: [source])
    import socket
    def no_network(*args, **kwargs):
        raise AssertionError("Fixture import must not use network/auth")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    cli_hub.do_install("official/imported", skip_confirm=True, invalidate_cache=False)
    installed = canonical / "imported" / "SKILL.md"
    assert installed.read_bytes() == (upstream / "SKILL.md").read_bytes()
    entry = HubLockFile().get_installed("imported")
    assert entry is not None and entry["install_path"] == "imported"
    assert _manage("patch", "imported", old_string="Original", new_string="Local edit")["success"]
    assert "Local edit" in installed.read_text(encoding="utf-8")
    assert "Original" in (upstream / "SKILL.md").read_text(encoding="utf-8")
    (upstream / "SKILL.md").write_text(_content("imported", "Explicit update."), encoding="utf-8")
    cli_hub.do_update(name="imported")
    assert "Skipping:" in capsys.readouterr().out
    assert "Local edit" in installed.read_text(encoding="utf-8")
    cli_hub.do_install("official/imported", force=True, skip_confirm=True, invalidate_cache=False)
    assert "Explicit update" in installed.read_text(encoding="utf-8")
    assert local.is_symlink() and (local / "imported").samefile(installed.parent)
    assert _manage("patch", "imported", old_string="Explicit", new_string="Edited")["success"]

    # Rebinding the active home must not leak a previous profile's destination.
    other = tmp_path / "other-profile"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    _config(other)
    assert not _tool("skill_view", name="imported")["success"]
    assert not _manage("patch", "imported", old_string="Edited", new_string="Wrong")["success"]
    assert _manage("create", "default-skill", content=_content("default-skill"))["success"]
    assert (other / "skills" / "default-skill" / "SKILL.md").exists()
    assert not (canonical / "default-skill").exists()


def test_hub_update_keeps_legacy_flat_canonical_path_for_categorized_official_skill(
    home, tmp_path, monkeypatch,
):
    """Updating an old flat install must not re-home it under its new official category."""
    from hermes_cli import skills_hub as cli_hub
    from tools.skills_guard import content_hash
    from tools.skills_hub import HubLockFile
    from tools.skills_hub_official import OptionalSkillSource

    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (home / "skills").symlink_to(canonical, target_is_directory=True)
    installed = _package(canonical, "imported", "imported")
    upstream = _package(tmp_path / "vendor-input", "category/imported", "imported")
    recorded_hash = content_hash(installed)
    HubLockFile().record_install(
        name="imported", source="official", identifier="official/category/imported",
        trust_level="builtin", scan_verdict="clean", skill_hash=recorded_hash,
        install_path="imported", files=["SKILL.md"],
    )

    monkeypatch.setenv("HERMES_OPTIONAL_SKILLS", str(tmp_path / "vendor-input"))
    source = OptionalSkillSource()
    monkeypatch.setattr(cli_hub, "_sources", lambda: [source])
    from tools import skills_hub_search
    monkeypatch.setattr(skills_hub_search, "create_source_router", lambda **kwargs: [source])
    (upstream / "SKILL.md").write_text(_content("imported", "Updated upstream instructions."), encoding="utf-8")

    cli_hub.do_update(name="imported")

    assert "Updated upstream instructions." in (canonical / "imported" / "SKILL.md").read_text(encoding="utf-8")
    assert not (canonical / "category" / "imported").exists()
    updated_entry = HubLockFile().get_installed("imported")
    assert updated_entry is not None and updated_entry["install_path"] == "imported"


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("evaluating-llms-harness", "mlops/evaluation"),
        ("serving-llms-vllm", "mlops/inference"),
    ],
)
def test_hub_update_keeps_native_official_install_path(home, tmp_path, monkeypatch, name, category):
    """Native official updates retain the recorded category instead of making a shadow copy."""
    from hermes_cli import skills_hub as cli_hub
    from tools.skills_guard import content_hash
    from tools.skills_hub import HubLockFile
    from tools.skills_hub_official import OptionalSkillSource

    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (home / "skills").symlink_to(canonical, target_is_directory=True)
    installed = _package(canonical, f"{category}/{name}", name)
    upstream_root = tmp_path / "vendor-input"
    upstream = _package(upstream_root, f"{category}/{name}", name)
    HubLockFile().record_install(
        name=name, source="official", identifier=f"official/{category}/{name}",
        trust_level="builtin", scan_verdict="clean", skill_hash=content_hash(installed),
        install_path=f"{category}/{name}", files=["SKILL.md"],
    )

    monkeypatch.setenv("HERMES_OPTIONAL_SKILLS", str(upstream_root))
    source = OptionalSkillSource()
    monkeypatch.setattr(cli_hub, "_sources", lambda: [source])
    from tools import skills_hub_search
    monkeypatch.setattr(skills_hub_search, "create_source_router", lambda **kwargs: [source])
    (upstream / "SKILL.md").write_text(_content(name, "Updated upstream instructions."), encoding="utf-8")

    cli_hub.do_update(name=name)

    expected = canonical / category / name / "SKILL.md"
    assert "Updated upstream instructions." in expected.read_text(encoding="utf-8")
    updated_entry = HubLockFile().get_installed(name)
    assert updated_entry is not None
    assert updated_entry["install_path"] == f"{category}/{name}"


def test_batch_aliases_preserve_conflict_checks_and_snapshot_boundaries(home, tmp_path):
    canonical = tmp_path / "canonical"
    first = _package(canonical, "alpha", "cat")
    second = _package(canonical, "cat/folder", "second")
    (home / "skills").symlink_to(canonical, target_is_directory=True)
    before = {p.relative_to(canonical): p.read_bytes() for p in canonical.rglob("*") if p.is_file()}
    clobber = _tool("skill_manage", operations=[
        {"action": "patch", "name": "cat", "old_string": "Original", "new_string": "First change"},
        {"action": "patch", "name": "alpha", "content": _content("cat", "Clobbered.")},
    ])
    assert not clobber["success"], clobber
    assert (first / "SKILL.md").read_bytes() == before[Path("alpha/SKILL.md")]
    # A legitimate sibling must not be removed to make room for rollback state.
    sibling = canonical / "alpha.rollback-broken"
    sibling.mkdir()
    (sibling / "keep").write_bytes(b"Preserve this sibling.")
    before[Path("alpha.rollback-broken/keep")] = b"Preserve this sibling."
    failed = _tool("skill_manage", operations=[
        {"action": "patch", "name": "cat", "old_string": "Original", "new_string": "Temporary"},
        {"action": "patch", "name": "cat/folder", "old_string": "Missing passage", "new_string": "No"},
    ])
    assert not failed["success"] and failed["completed_before_failure"] == 1, failed
    # Hidden state (skill_manage's .locks/ lock files) is not skill content.
    after = {p.relative_to(canonical): p.read_bytes() for p in canonical.rglob("*")
             if p.is_file() and not any(part.startswith(".") for part in p.relative_to(canonical).parts)}
    assert after == before
    assert (second / "SKILL.md").is_file()
