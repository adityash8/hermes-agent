import base64
from pathlib import Path

import pytest

from hermes_cli import web_server

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
