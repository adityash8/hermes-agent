import base64
import urllib.parse
from contextlib import nullcontext
from pathlib import Path

import pytest

from hermes_cli import ssh_workspace_fs, web_server
from hermes_cli.web_routers import files as file_routes
from tests.macos_path_aliases import ALIAS_KINDS, macos_alias

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required


def test_fs_list_sorts_and_hides_noise(client, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "b.txt").write_text("b")
    (root / "a_dir").mkdir()
    (root / "a.txt").write_text("a")
    (root / "node_modules").mkdir()
    (root / ".git").mkdir()

    response = client.get("/api/fs/list", params={"path": str(root)})

    assert response.status_code == 200
    entries = response.json()["entries"]
    assert [entry["name"] for entry in entries] == ["a_dir", "a.txt", "b.txt"]
    assert entries[0] == {"name": "a_dir", "path": str(root / "a_dir"), "isDirectory": True}
    assert all(entry["name"] not in {".git", "node_modules"} for entry in entries)


def test_fs_routes_workspace_reads_to_ssh_backend(client, monkeypatch):
    calls = []

    class FakeSshFs:
        cwd = "/srv/repos"

        def list_dir(self, path, _hidden_names):
            calls.append(("list", path))
            return {
                "entries": [
                    {
                        "name": "project",
                        "path": "/srv/repos/project",
                        "isDirectory": True,
                    }
                ]
            }

        def git_branch(self, path):
            calls.append(("branch", path))
            return "main"

        def git_root(self, path):
            calls.append(("git-root", path))
            return "/srv/repos/project"

        def read_bytes(self, path, *, max_bytes, read_limit=None):
            calls.append(("read", path, max_bytes, read_limit))
            return b"hello", 5, path

        def write_text(self, path, content, *, max_bytes):
            calls.append(("write", path, content, max_bytes))
            return path, len(content.encode("utf-8"))

        def inspect_file(self, path):
            calls.append(("inspect", path))
            return path, 5

        def stream_file(self, path):
            calls.append(("stream", path))
            yield b"hello"

    backend = FakeSshFs()
    monkeypatch.setattr(file_routes, "_fs_backend", lambda profile=None: backend)

    listing = client.get(
        "/api/fs/list",
        params={"path": "/srv/repos", "profile": "remote-dev"},
    )
    default_cwd = client.get(
        "/api/fs/default-cwd",
        params={"profile": "remote-dev"},
    )
    read_text = client.get(
        "/api/fs/read-text",
        params={"path": "/srv/repos/project/README.md", "profile": "remote-dev"},
    )
    read_data = client.get(
        "/api/fs/read-data-url",
        params={"path": "/srv/repos/project/logo.png", "profile": "remote-dev"},
    )
    write_text = client.post(
        "/api/fs/write-text",
        params={"profile": "remote-dev"},
        json={"path": "/srv/repos/project/README.md", "content": "updated"},
    )
    git_root = client.get(
        "/api/fs/git-root",
        params={"path": "/srv/repos/project/src", "profile": "remote-dev"},
    )
    download = client.get(
        "/api/fs/download",
        params={"path": "/srv/repos/project/report.txt", "profile": "remote-dev"},
    )

    assert listing.status_code == 200
    assert listing.json() == {
        "entries": [
            {
                "name": "project",
                "path": "/srv/repos/project",
                "isDirectory": True,
            }
        ]
    }
    assert default_cwd.status_code == 200
    assert default_cwd.json() == {"cwd": "/srv/repos", "branch": "main"}
    assert read_text.json()["text"] == "hello"
    assert read_data.json()["dataUrl"] == "data:image/png;base64,aGVsbG8="
    assert write_text.json() == {"ok": True, "path": "/srv/repos/project/README.md", "byteSize": 7}
    assert git_root.json() == {"root": "/srv/repos/project"}
    assert download.content == b"hello"
    assert calls == [
        ("list", "/srv/repos"),
        ("branch", "/srv/repos"),
        (
            "read",
            "/srv/repos/project/README.md",
            file_routes._FS_TEXT_SOURCE_MAX_BYTES,
            file_routes._FS_TEXT_PREVIEW_MAX_BYTES,
        ),
        ("read", "/srv/repos/project/logo.png", web_server._FS_DATA_URL_MAX_BYTES, None),
        ("write", "/srv/repos/project/README.md", "updated", file_routes._FS_TEXT_WRITE_MAX_BYTES),
        ("git-root", "/srv/repos/project/src"),
        ("inspect", "/srv/repos/project/report.txt"),
        ("stream", "/srv/repos/project/report.txt"),
    ]


def test_fs_backend_resolves_ssh_connection_from_profile_env(monkeypatch):
    captured = {}
    expected = object()
    monkeypatch.setattr(file_routes, "_profile_scope", lambda _profile: nullcontext())
    monkeypatch.setattr(
        file_routes,
        "load_config",
        lambda: {"terminal": {"backend": "ssh", "cwd": "/srv/repos"}},
    )
    monkeypatch.setattr(
        file_routes,
        "load_env",
        lambda: {
            "TERMINAL_SSH_HOST": "ssh.example",
            "TERMINAL_SSH_USER": "dev",
            "TERMINAL_SSH_PORT": "2222",
        },
    )

    def fake_factory(profile_key, terminal):
        captured.update(profile_key=profile_key, terminal=terminal)
        return expected

    monkeypatch.setattr(ssh_workspace_fs, "get_ssh_workspace_fs", fake_factory)

    assert file_routes._fs_backend("Remote-Dev") is expected
    assert captured == {
        "profile_key": "remote-dev",
        "terminal": {
            "backend": "ssh",
            "cwd": "/srv/repos",
            "ssh_host": "ssh.example",
            "ssh_user": "dev",
            "ssh_port": "2222",
        },
    }


def test_fs_download_rejects_sensitive_resolved_ssh_path_before_streaming(client, monkeypatch):
    class FakeSshFs:
        def inspect_file(self, _path):
            return "/srv/repos/.env", 8

        def stream_file(self, _path):
            pytest.fail("sensitive SSH files must not be streamed")

    monkeypatch.setattr(file_routes, "_fs_backend", lambda profile=None: FakeSshFs())

    response = client.get(
        "/api/fs/download",
        params={"path": "/srv/repos/safe-link", "profile": "remote-dev"},
    )

    assert response.status_code == 403


def test_fs_download_streams_ssh_files_without_preview_cap(client, monkeypatch):
    class FakeSshFs:
        def inspect_file(self, _path):
            return "/srv/repos/report.txt", 6

        def stream_file(self, _path):
            yield b"123"
            yield b"456"

    monkeypatch.setattr(file_routes, "_FS_TEXT_SOURCE_MAX_BYTES", 3)
    monkeypatch.setattr(file_routes, "_fs_backend", lambda profile=None: FakeSshFs())

    response = client.get(
        "/api/fs/download",
        params={"path": "/srv/repos/report.txt", "profile": "remote-dev"},
    )

    assert response.status_code == 200
    assert response.content == b"123456"


def test_fs_read_data_url_rejects_over_cap(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "_FS_DATA_URL_MAX_BYTES", 3)
    target = tmp_path / "image.png"
    target.write_bytes(b"1234")

    response = client.get("/api/fs/read-data-url", params={"path": str(target)})

    assert response.status_code == 413


def test_fs_download_streams_file_without_data_url_cap(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web_server, "_FS_DATA_URL_MAX_BYTES", 3)
    target = tmp_path / "report with spaces.pdf"
    target.write_bytes(b"123456")

    response = client.get("/api/fs/download", params={"path": str(target)})

    assert response.status_code == 200
    assert response.content == b"123456"
    assert response.headers["content-type"].startswith("application/pdf")
    assert "report%20with%20spaces.pdf" in response.headers["content-disposition"]


def test_fs_download_rejects_sensitive_files(client, tmp_path):
    target = tmp_path / ".env"
    target.write_text("SECRET=1")

    response = client.get("/api/fs/download", params={"path": str(target)})

    assert response.status_code == 403


@pytest.mark.parametrize("endpoint", ["/api/fs/read-text", "/api/fs/read-data-url", "/api/fs/download"])
# mcp-tokens/ and pairing/ are guarded as Hermes-dir entries, not by name anywhere (a
# checkout may own such a dir): test_fs_reads_reject_hermes_managed_names_in_hermes_home.
@pytest.mark.parametrize("relative", [".env", "auth.json"])
def test_fs_readers_reject_sensitive_paths(client, tmp_path, endpoint, relative):
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SECRET=1")

    response = client.get(endpoint, params={"path": str(target)})

    assert response.status_code == 403
    assert "SECRET" not in response.text


def test_fs_list_hides_credential_stores(client, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("SECRET=1")
    (root / "auth.json").write_text("{}")
    (root / "notes.txt").write_text("ok")

    response = client.get("/api/fs/list", params={"path": str(root)})

    assert response.status_code == 200
    assert [entry["name"] for entry in response.json()["entries"]] == ["notes.txt"]


def test_fs_endpoints_require_auth(tmp_path):
    client = TestClient(web_server.app)
    target = tmp_path / "secret.txt"
    target.write_text("secret")

    list_response = client.get("/api/fs/list", params={"path": str(tmp_path)})
    read_response = client.get("/api/fs/read-text", params={"path": str(target)})
    default_response = client.get("/api/fs/default-cwd")

    assert list_response.status_code == 401
    assert read_response.status_code == 401
    assert default_response.status_code == 401


_FS_READ_ENDPOINTS = ["/api/fs/read-text", "/api/fs/read-data-url", "/api/fs/download"]


def _write_secret(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SECRET=1")
    return target


@pytest.mark.parametrize("endpoint", _FS_READ_ENDPOINTS)
@pytest.mark.parametrize("rel", [".env", ".OP.ENV", "auth.json", "credentials", "cache/op_cache.json"])
def test_fs_reads_reject_sensitive_files(client, tmp_path, endpoint, rel):
    """Secret-bearing basenames are blocked anywhere on disk, not just in Hermes dirs."""
    target = _write_secret(tmp_path / "project" / rel)

    response = client.get(endpoint, params={"path": str(target)})

    assert response.status_code == 403
    assert "SECRET" not in response.text


@pytest.mark.parametrize("endpoint", _FS_READ_ENDPOINTS)
@pytest.mark.parametrize("rel", ["config.yaml", "mcp-tokens/github.json", "pairing/device"])
def test_fs_reads_reject_hermes_managed_names_in_hermes_home(client, endpoint, rel):
    from hermes_constants import get_hermes_home

    target = _write_secret(get_hermes_home() / rel)

    response = client.get(endpoint, params={"path": str(target)})

    assert response.status_code == 403
    assert "SECRET" not in response.text


@pytest.mark.parametrize("rel", ["config.yaml", ".env.example", "mcp-tokens/index.ts", "pairing/README.md"])
def test_fs_reads_allow_hermes_managed_names_in_projects(client, tmp_path, rel):
    """/api/fs serves arbitrary project paths; a repo's config.yaml is not a Hermes secret."""
    target = tmp_path / "project" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("project-file")

    response = client.get("/api/fs/read-text", params={"path": str(target)})

    assert response.status_code == 200
    assert "project-file" in response.text


def test_fs_list_hides_sensitive_entries(client, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("print()")
    (root / "config.yaml").write_text("name: app")
    (root / ".env.example").write_text("API_KEY=")
    (root / "pairing").mkdir()
    (root / ".env").write_text("SECRET=1")
    (root / "Auth.JSON").write_text("{}")

    response = client.get("/api/fs/list", params={"path": str(root)})

    assert response.status_code == 200
    assert [entry["name"] for entry in response.json()["entries"]] == [
        "pairing", ".env.example", "config.yaml", "main.py",
    ]


def test_fs_list_survives_symlink_loop_on_guarded_name(client, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("print()")
    (root / "config.yaml").symlink_to(root / "config.yaml")

    response = client.get("/api/fs/list", params={"path": str(root)})

    assert response.status_code == 200
    assert "main.py" in [entry["name"] for entry in response.json()["entries"]]


def test_fs_list_hides_hermes_managed_entries_in_hermes_home(client):
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    (home / "notes.md").write_text("notes")
    (home / "config.yaml").write_text("model: x")
    (home / "mcp-tokens").mkdir()

    response = client.get("/api/fs/list", params={"path": str(home)})

    assert response.status_code == 200
    names = [entry["name"] for entry in response.json()["entries"]]
    assert "notes.md" in names
    assert "config.yaml" not in names
    assert "mcp-tokens" not in names


@pytest.mark.require_symlinks
@pytest.mark.parametrize("endpoint", _FS_READ_ENDPOINTS)
@pytest.mark.parametrize("entry,rel", [
    (".env", ".env"), ("config.yaml", "config.yaml"), ("auth.json", "auth.json"),
    ("mcp-tokens", "mcp-tokens/github.json"),
])
def test_fs_reads_reject_hermes_stores_symlinked_out(client, tmp_path, endpoint, entry, rel):
    """A dotfiles-managed HERMES_HOME entry resolves to an innocuous name; still a Hermes secret."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    store = tmp_path / "dotfiles" / "store"
    _write_secret(store / Path(rel).relative_to(entry))
    (home / entry).symlink_to(store)

    response = client.get(endpoint, params={"path": str(home / rel)})

    assert response.status_code == 403
    assert "SECRET" not in response.text


@pytest.mark.require_symlinks
def test_fs_list_hides_hermes_stores_symlinked_out(client, tmp_path):
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    dotfiles = tmp_path / "dotfiles"
    _write_secret(dotfiles / "tokens" / "github.json")
    (dotfiles / "hermes-config").write_text("model: x")
    (home / "mcp-tokens").symlink_to(dotfiles / "tokens")
    (home / "config.yaml").symlink_to(dotfiles / "hermes-config")
    (home / "notes.md").write_text("notes")

    def names(path):
        response = client.get("/api/fs/list", params={"path": str(path)})
        assert response.status_code == 200
        return {entry["name"] for entry in response.json()["entries"]}

    assert "notes.md" in names(home)
    assert not {"config.yaml", "mcp-tokens"} & names(home)
    assert names(home / "mcp-tokens") == set()


@pytest.mark.parametrize("rel", [
    ".env.example", "config.yaml.example", "tests/fixtures/config.yaml", "src/pairing/README.md",
    "src/mcp-tokens/index.ts",
])
def test_fs_reads_allow_managed_names_in_checkout_under_hermes_home(client, rel):
    """The default install puts the source checkout at ~/.hermes/hermes-agent; only direct
    Hermes entries are secrets, not same-named files deeper in a repo."""
    from hermes_constants import get_hermes_home

    target = get_hermes_home() / "hermes-agent" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("project-file")

    response = client.get("/api/fs/read-text", params={"path": str(target)})

    assert response.status_code == 200
    assert "project-file" in response.text


def test_fs_list_shows_managed_names_in_checkout_under_hermes_home(client):
    from hermes_constants import get_hermes_home

    src = get_hermes_home() / "hermes-agent" / "src"
    (src / "pairing").mkdir(parents=True)
    (src / "mcp-tokens").mkdir()
    (src / "config.yaml").write_text("name: app")

    response = client.get("/api/fs/list", params={"path": str(src)})

    assert response.status_code == 200
    assert {entry["name"] for entry in response.json()["entries"]} == {"pairing", "mcp-tokens", "config.yaml"}


def test_fs_hides_hermes_written_copies_of_credential_stores(client):
    """Hermes snapshots config.yaml into backups/config/ (before setup, on a parse failure), older
    builds left the copies beside the original, and auth.json is copied aside when corrupt; the
    copies hold the same secrets."""
    from hermes_cli.config_backups import backup_config
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    config = home / "config.yaml"
    _write_secret(config)
    _write_secret(home / "auth.json.corrupt")
    snapshots = [backup_config(config, "pre-setup"), backup_config(config, "corrupt")]
    # Written after the snapshots: backup_config sweeps legacy siblings into backups/config/.
    legacy = home / "config.yaml.corrupt.20261003-000000.bak"
    _write_secret(legacy)
    copies = [*snapshots, legacy, home / "auth.json.corrupt"]
    (home / "notes.yaml.bak").write_text("notes")

    response = client.get("/api/fs/list", params={"path": str(home)})
    assert "notes.yaml.bak" in {entry["name"] for entry in response.json()["entries"]}

    for copy in copies:
        assert copy is not None
        response = client.get("/api/fs/list", params={"path": str(copy.parent)})
        assert copy.name not in {entry["name"] for entry in response.json().get("entries", [])}
        for endpoint in _FS_READ_ENDPOINTS:
            response = client.get(endpoint, params={"path": str(copy)})
            assert response.status_code == 403
            assert "SECRET" not in response.text


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("kind", ALIAS_KINDS)
@pytest.mark.parametrize("endpoint", _FS_READ_ENDPOINTS)
@pytest.mark.parametrize("rel", [".env", "config.yaml", "mcp-tokens/github.json", "pairing/device"])
def test_fs_reads_reject_macos_alias_spellings(client, endpoint, kind, rel):
    """Firmlink / volfs spellings open the store but keep their own prefix through ``resolve()``."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    target = _write_secret(home / rel)
    (home / "notes.md").write_text("notes")

    response = client.get(endpoint, params={"path": macos_alias(target, kind)})

    assert response.status_code == 403
    assert "SECRET" not in response.text
    assert client.get(endpoint, params={"path": macos_alias(home / "notes.md", kind)}).status_code == 200


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("kind", ALIAS_KINDS)
def test_fs_list_hides_hermes_entries_via_macos_alias(client, kind):
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    (home / "notes.md").write_text("notes")
    (home / "config.yaml").write_text("model: x")
    (home / "mcp-tokens").mkdir()

    response = client.get("/api/fs/list", params={"path": macos_alias(home, kind)})

    assert response.status_code == 200
    names = {entry["name"] for entry in response.json()["entries"]}
    assert "notes.md" in names
    assert not {"config.yaml", "mcp-tokens"} & names


def test_fs_read_text_decodes_double_encoded_non_ascii_path(client, tmp_path):
    target = tmp_path / "半导体行业运营分享会纪要.md"
    target.write_text("hello")

    # One percent-encoding on top of HTTP's own query decoding: the server
    # receives the literal "%E5%8D%8A..." string (issue #103425).
    double_encoded = urllib.parse.quote(str(target))

    response = client.get("/api/fs/read-text", params={"path": double_encoded})

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == str(target)
    assert body["text"] == "hello"


def test_fs_read_text_keeps_literal_percent_filename_verbatim(client, tmp_path):
    target = tmp_path / "50%25off.md"
    target.write_text("literal percent name")

    response = client.get("/api/fs/read-text", params={"path": str(target)})

    assert response.status_code == 200
    assert response.json()["text"] == "literal percent name"


def test_fs_write_text_never_decodes_percent_path(client, tmp_path):
    sibling = tmp_path / "report v2.md"
    sibling.write_text("existing")

    response = client.post(
        "/api/fs/write-text",
        json={"path": str(tmp_path / "report%20v2.md"), "content": "new"},
    )

    assert response.status_code == 200
    assert sibling.read_text() == "existing"
    assert (tmp_path / "report%20v2.md").read_text() == "new"
