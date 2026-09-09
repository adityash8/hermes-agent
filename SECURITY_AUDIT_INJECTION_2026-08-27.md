# Hermes Agent Injection Security Audit — 2026-08-27

Focus: command injection, SQL injection, SSRF, path traversal, unsafe
deserialization, template injection, header injection — with complete
attack chains from attacker-controlled input (network / gateway /
upload / URL) to impact.

Out of scope (per SECURITY.md / hunt brief): LLM→`terminal()` command
injection; tool-level R/W restrictions when shell is permitted;
approval-gate heuristic bypasses; already-reported items (spot-editor
`/api/fs/read-text`, provider-probe SSRF, backup `_external/` zip
members, WSL `openExternal` cmd.exe).

## Validated findings

### 1. HIGH — Managed-files upload/delete bypass credential write guard

| Field | Value |
| --- | --- |
| Severity | **HIGH** |
| Location | `hermes_cli/web_server.py` — `upload_managed_file`, `upload_managed_file_stream`, `delete_managed_file`, `create_managed_directory` |
| Title | Dashboard managed-files write APIs bypass the `#57505` sensitive-path guard, allowing overwrite of `.env` / credential stores and (on local dashboards) `~/.ssh/authorized_keys` |

**Description.** Issue `#57505` added `_is_sensitive_path()` so
`GET /api/files`, `/api/files/read`, and `/api/files/download` refuse
`.env*`, `auth.json`, `mcp-tokens/`, `pairing/`, and related credential
stores. The same helper is **never** called on the write side:

- `POST /api/files/upload`
- `POST /api/files/upload-stream`
- `POST /api/files/mkdir`
- `DELETE /api/files`

The comment at `_is_sensitive_path` explicitly marks write as “a
separate threat class … out of scope for this fix.” On a local
dashboard (`locked_root=None`, `can_change_path=True`) any absolute
path is accepted. On hosted installs the root is locked to
`HERMES_HOME` / `/opt/data`, which still contains `.env` and
`auth.json`.

**Impact.** An attacker with a dashboard session (XSS, stolen
`X-Hermes-Session-Token` / OAuth cookie, or malware on loopback `:9119`)
can:

1. Overwrite `$HERMES_HOME/.env` / `auth.json` despite those files being
   unreadable through the same API — injecting attacker API keys or
   destroying the operator’s credentials.
2. On local dashboards, overwrite `~/.ssh/authorized_keys` for SSH
   persistence / account takeover.
3. Delete the same credential files (availability + forced re-auth).

**Attack path.**

1. Obtain a dashboard session token/cookie.
2. `GET /api/files/read?path=…/.hermes/.env` → `403` (guard works).
3. `POST /api/files/upload` with `path` = that `.env` and a base64
   data URL containing `OPENAI_API_KEY=attacker-…` → `200`, file
   replaced.
4. Optionally `POST /api/files/upload` with
   `path=/home/<user>/.ssh/authorized_keys` (local policy) → SSH
   backdoor.

**Evidence.**

- `hermes_cli/web_server.py:1791-1810` — `_is_sensitive_path` documented read-only
- `hermes_cli/web_server.py:2358`, `2402` — read/download call the guard
- `hermes_cli/web_server.py:2422-2444`, `2457-2517`, `2521-2539`, `2542-2564` — write endpoints omit the guard
- `hermes_cli/web_server.py:2141-2142` — local policy unlocks home-wide absolute paths
- PoC (isolated `HOME`/`HERMES_HOME`): read `.env` → 403; upload overwrite → 200 and new contents; upload `authorized_keys` → 200

**Remediation.**

- Call `_is_sensitive_path(target)` (or a write-oriented denylist that
  also covers `~/.ssh`, `~/.aws`, etc. per `agent.file_safety`) on
  upload / upload-stream / mkdir / delete before any mutation.
- Prefer reusing `is_write_denied()` / `get_write_block_error()` so
  dashboard Files cannot write paths the agent file tools refuse.
- Add regression tests mirroring
  `tests/hermes_cli/test_web_server_files.py` for the write side.

---

### 2. MEDIUM — Dashboard MCP server test/connect SSRF

| Field | Value |
| --- | --- |
| Severity | **MEDIUM** |
| Location | `hermes_cli/web_routers/mcp.py` (`POST /api/mcp/servers`, `POST /api/mcp/servers/{name}/test`); `tools/mcp_tool.py` HTTP transport / content-type probe |
| Title | MCP add/test connects to attacker-controlled URLs with no SSRF allowlist, reaching loopback/IMDS |

**Description.** Dashboard MCP management lets an authenticated client
register an HTTP/SSE MCP server URL and immediately probe it via
`POST /api/mcp/servers/{name}/test`. Connection uses plain
`httpx.AsyncClient` (HEAD/GET/POST initialize probe in
`tools/mcp_tool.py`). There are **zero** calls to
`tools.url_safety.is_safe_url` / `create_ssrf_safe_client` anywhere in
`mcp_tool.py` or `mcp_config.py`. Private, loopback, and link-local
targets (including `169.254.169.254`) are accepted. This is distinct
from the already-reported provider-probe SSRF
(`/api/providers/*/validate`).

**Impact.** Same class as the provider-probe finding: host-local SSRF
oracle / IMDS reachability from a stolen dashboard session. Successful
MCP handshakes may also surface tool metadata from internal services.
Compose `network_mode: host` makes host IMDS directly reachable.

**Attack path.**

1. Obtain a dashboard session.
2. `POST /api/mcp/servers` with
   `{"name":"x","url":"http://169.254.169.254/latest/meta-data/…"}`
   (or `http://127.0.0.1:<internal>/`).
3. `POST /api/mcp/servers/x/test` — Hermes opens HTTP connections to
   that URL (content-type probe + MCP initialize).
4. Repeat as a port/path oracle; on IMDS-shaped JSON/HTML, infer
   reachability from error/status text.

**Evidence.**

- `hermes_cli/web_routers/mcp.py:71-104`, `137-194` — add + test routes
- `tools/mcp_tool.py:2618-2686` — `httpx.AsyncClient` HEAD/GET/POST probe, no SSRF filter
- PoC: local `HTTPServer` on `127.0.0.1`; `_save_mcp_server` +
  `_probe_single_server` produced `HEAD` and `POST initialize` hits to
  `/latest/meta-data/iam/security-credentials/` with no `is_safe_url`

**Remediation.**

- Before any MCP HTTP connect/probe, require
  `is_safe_url(url)` and use `create_ssrf_safe_client` /
  `create_ssrf_safe_async_client` (same stack as `web_extract` /
  skills hub).
- Reject non-http(s), private, loopback, link-local, and IMDS literals
  at `_normalize_mcp_server_create` / `_save_mcp_server` as well as at
  spawn/probe time (so hand-edited config cannot reconnect).
- Return generic errors that do not distinguish connection-refused from
  HTTP 4xx for blocked internal targets.

---

## Discarded near-misses

| Candidate | Why discarded |
| --- | --- |
| Spot-editor `/api/fs/read-text` / write-text credential bypass | Already reported |
| Provider-probe `/api/providers/*/validate` SSRF | Already reported |
| Backup import `_external/` → `$HOME` | Already reported |
| Desktop WSL `openExternal` → `cmd.exe /c start` | Already reported |
| LLM `terminal()` / file tools command & path abuse | Intended trust envelope / out of scope |
| FTS5 `MATCH` via session search | `_sanitize_fts5_query` + bound parameters; no injectable SQL |
| Backup non-`_external` zip members | `relative_to(hermes_root)` blocks absolute/`..` slip |
| Quick snapshot restore | `snapshot_id` and manifest paths validated against roots |
| Skill hub zip / quarantine paths | `_validate_bundle_rel_path` / `_normalize_bundle_path` reject traversal |
| Plugin git install subdir | `_resolve_subdir_within` rejects escape from clone |
| Webhook `gh pr comment` | `repo`/`pr_number` allowlisted; body is argv not shell |
| Profile `open-terminal` shell strings | `validate_profile_name` → `[a-z0-9_-]` only |
| FileResponse `Content-Disposition` CRLF | Starlette RFC 5987-encodes `%0D%0A` |
| `yaml.load` in `xai_retirement` | ruamel `YAML(typ="rt")` on operator config, not untrusted network input |
| `shell=True` memory-provider `install_cmd` | Comes from shipped plugin manifests; provider name charset-gated |
| Cron `/api/cron/fire` | Public path but JWT fail-closed without JWKS; purpose claim required |
| MCP stdio `python -c` RCE via dashboard | Intentional operator capability (stdio MCP = local command); not a bypass of a declared guard |
| Managed-files read of `~/.ssh` | Not in `#57505` denylist; file browser on unlocked local policy is intentional browse of home (write of SSH keys still counted above via unlocked write path) |

## Method notes

Source-traced call chains; two findings reproduced with isolated
`HOME`/`HERMES_HOME` and a loopback HTTP listener. No changes to
production Hermes behavior were required for the audit itself.
