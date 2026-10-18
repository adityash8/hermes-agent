"""Shutdown forensics — capture context when the gateway receives SIGTERM/SIGINT (and SIGUSR1 restarts).

``shutdown_signal_handler`` runs synchronously inside the asyncio loop, so
:func:`snapshot_shutdown_context` is a fast (<10ms) non-blocking probe and
:func:`spawn_async_diagnostic` is a fire-and-forget ``ps`` walk in a detached
subprocess. Anything that waits belongs in the async helper, never in the probe.
The async helper also names the live requester (``launchctl kickstart``/``bootout``,
``hermes gateway restart``, ``kill``) by its matched verb phrase only (redacted), with
its launchd label and ancestor names; nothing else from any argv is written (EZ-1577).
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from gateway.restart import DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT, resolve_systemd_timeout_stop_sec
import contextlib

_SIGNAL_NAME_BY_NUM: Dict[int, str] = {
    int(getattr(signal, _name)): _name
    for _name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2")
    if getattr(signal, _name, None) is not None
}


def _signal_name(sig: Any) -> str:
    """Human-readable signal name (``str(sig)`` as fallback)."""
    if sig is None:
        return "UNKNOWN"
    try:
        sig_int = int(sig)
    except (TypeError, ValueError):
        return str(sig)
    return _SIGNAL_NAME_BY_NUM.get(sig_int, f"signal#{sig_int}")


def _read_proc_field(pid: int, key: str) -> Optional[str]:
    """Read a single field from /proc/<pid>/status.  Linux only; None elsewhere."""
    with contextlib.suppress(OSError), open(f"/proc/{pid}/status", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith(key + ":"):
                return line.split(":", 1)[1].strip()
    return None


def _proc_summary(pid: int) -> Dict[str, Any]:
    """Compact /proc/<pid> identity (pid, name, state, ppid, uid). Never reads cmdline/argv —
    those bytes are not safe to persist (tokens, URIs, ``-e KEY=`` overlays)."""
    summary: Dict[str, Any] = {"pid": pid}
    if pid <= 0:
        return summary
    for out_key, proc_key in (("name", "Name"), ("state", "State")):
        if (value := _read_proc_field(pid, proc_key)) is not None:
            summary[out_key] = value
    if (ppid := _read_proc_field(pid, "PPid")) is not None:
        with contextlib.suppress(ValueError):
            summary["ppid"] = int(ppid)
    if (uid := _read_proc_field(pid, "Uid")) is not None:
        summary["uid"] = uid.split()[0] if uid else uid  # "real effective saved fs"
    if "name" not in summary and (name := _darwin_proc_name(pid)):
        summary["name"] = name  # macOS has no /proc; without this every parent logs as "?" (EZ-1577)
    return summary


_LIBPROC: List[Any] = []


def _darwin_proc_name(pid: int) -> Optional[str]:
    """Short process name via libproc ``proc_name`` (one syscall, no subprocess). macOS only."""
    if sys.platform != "darwin":
        return None
    try:
        if not _LIBPROC:
            _LIBPROC.append(ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True))
        buf = ctypes.create_string_buffer(256)
        n = _LIBPROC[0].proc_name(int(pid), buf, ctypes.c_uint32(len(buf)))
        return buf.value.decode("utf-8", "replace") if n > 0 else None
    except (OSError, AttributeError, ValueError):
        return None


def _read_marker(path: Path) -> Optional[str]:
    """Return the marker file's text, or None if absent/unreadable."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return None


def snapshot_shutdown_context(received_signal: Any = None) -> Dict[str, Any]:
    """Fast (<10ms) snapshot of who/what is asking us to shut down: signal name/number, own + parent
    /proc summaries, systemd parentage, takeover/planned-stop markers, TracerPid, 1-min load,
    timestamps. Pure stdlib, never raises, never blocks."""
    pid, ppid = os.getpid(), os.getppid()
    ctx: Dict[str, Any] = {
        "ts": time.time(), "ts_monotonic": time.monotonic(),
        "signal": _signal_name(received_signal),
        "signal_num": int(received_signal) if received_signal is not None else None,
        "pid": pid, "ppid": ppid, "parent": _proc_summary(ppid), "self": _proc_summary(pid),
    }
    # INVOCATION_ID is set by systemd units; ppid==1 also suggests systemd forwarded the SIGTERM.
    for ctx_key, env_key in (("systemd_invocation_id", "INVOCATION_ID"),
                             ("systemd_journal_stream", "JOURNAL_STREAM")):
        if os.environ.get(env_key):
            ctx[ctx_key] = os.environ[env_key]
    ctx["under_systemd"] = bool(os.environ.get("INVOCATION_ID")) or ppid == 1
    # High load points at "something crushing the box" rather than an external killer.
    with contextlib.suppress(OSError, AttributeError):
        ctx["loadavg_1m"] = os.getloadavg()[0]
    # Nonzero TracerPid means a debugger/strace is attached.
    with contextlib.suppress(TypeError, ValueError):
        if (tracer := _read_proc_field(pid, "TracerPid")) is not None and tracer != "0":
            ctx["tracer_pid"] = int(tracer) if tracer.isdigit() else tracer
            ctx["tracer"] = _proc_summary(int(tracer)) if tracer.isdigit() else None
    # Race hint: a takeover marker on disk that does NOT name us is a smoking gun for "another
    # --replace instance is killing us". Filenames mirror gateway.status; literals keep the signal-
    # handler path import-light.
    with contextlib.suppress(Exception):  # noqa: BLE001 — never raise from a signal handler
        hermes_home_str = os.path.expanduser(os.environ.get("HERMES_HOME", ""))
        if hermes_home_str:
            raw = _read_marker(Path(hermes_home_str) / ".gateway-takeover.json")
            if raw is not None:
                ctx["takeover_marker"] = raw[:300]
                ctx["takeover_marker_for_self"] = (f'"target_pid": {pid}' in raw
                                                   or f"'target_pid': {pid}" in raw)
            raw = _read_marker(Path(hermes_home_str) / ".gateway-planned-stop.json")
            if raw is not None:
                ctx["planned_stop_marker"] = raw[:300]
    return ctx


def _async_diagnostic_script(signal_name: str, self_pid: int) -> str:
    """POSIX listing used by the detached diagnostic. Columns are identity/resource only — no argv.

    macOS ``comm`` is argv[0], which a process can rewrite (``exec -a``, setproctitle), so the whole
    listing is piped through :data:`_REDACT_SED` before it reaches the log.
    """
    return (
        "{ "
        f"echo '=== shutdown diagnostic @ {signal_name} ==='; "
        "echo '--- date ---'; date -u +%Y-%m-%dT%H:%M:%SZ; "
        "echo '--- ps (top 60 by cpu, comm only) ---'; "
        # ``sort`` instead of GNU ``--sort=-pcpu`` so BSD ps (macOS) produces a listing too; the header
        # line is echoed first so ``sort`` does not bury it among the 0.0-cpu rows.
        "ps -eo pid,ppid,user,pcpu,pmem,stat,comm 2>/dev/null | { IFS= read -r h; echo \"$h\"; sort -nrk4; } | head -60; "
        f"echo '--- pstree of self ---'; pstree -pl {self_pid} 2>/dev/null | head -40 || true; "
        "echo '--- loadavg ---'; cat /proc/loadavg 2>/dev/null || sysctl -n vm.loadavg 2>/dev/null || true; "
        "echo '--- recent dmesg (oom/killed) ---'; "
        "dmesg -T 2>/dev/null | tail -20 || journalctl --user -n 20 --no-pager 2>/dev/null | tail -20 || true; "
        + _REQUESTER_PROBE.replace("@SELF@", str(int(self_pid)))
        + "echo '=== end ==='; "
        "} 2>&1 | sed -E " + _REDACT_SED
    )


_REDACT_SED = (
    "'s/[A-Za-z0-9_+=-]{32,}/<redacted>/g; "
    "s/((KEY|TOKEN|SECRET|PASSWORD|key|token|secret|password)[A-Za-z_]*[=:])[^ ]+/\\1<redacted>/g'"
)


# Who asked for the stop/restart (EZ-1577). Neither asyncio nor macOS gives the handler the sender pid,
# and the gateway's own parent is just the per-launch wrapper, so this looks for the requester while it
# is still alive: ``launchctl kickstart -k``/``bootout`` and ``hermes gateway restart|stop`` block until
# we exit. Only the matched verb phrase is persisted (redacted), never the rest of argv; the launchd
# label comes from the nearest ancestor's XPC_SERVICE_NAME (Linux: its systemd ``.service``).
_REQUESTER_VERB_RE = (
    r"launchctl +(kickstart( +-[a-z]+)*|bootout|stop|kill +[A-Za-z0-9]+|unload|remove) +[^ ]+"
    r"|hermes[^ ]* +gateway +(restart|stop|start)( +--[a-z-]+)*"
    r"|(^|[ /])(kill|pkill|killall)( +-[A-Za-z0-9]+)* +[^ ]+"
)
_REQUESTER_PROBE = (
    "echo '--- ancestry of self ---'; "
    "snap=$(ps -axo pid=,ppid=,etime=,comm= 2>/dev/null); "
    "_chain() { printf '%s\\n' \"$snap\" | awk -v p=\"$1\" -v want=\"$2\" '"
    "{pp[$1]=$2; et[$1]=$3; c=$4; for(i=5;i<=NF;i++) c=c \" \" $i; n=split(c,a,\"/\"); split(a[n],w,\" \"); cm[$1]=w[1]} "
    "END{q=p; s=\"\"; ids=\"\"; k=0; while(q>1 && k<12 && (q in cm)){s=s (s?\" < \":\"\") cm[q] \"(\" q \")\"; "
    "ids=ids \" \" q; q=pp[q]; k++} if(want==\"ids\") print ids; else if(want==\"etime\") print et[p]; else print s}'; }; "
    "_label() { for q in $1; do "
    "l=$(ps -E -ww -o command= -p \"$q\" 2>/dev/null | tr ' ' '\\n' | grep -m1 '^XPC_SERVICE_NAME=' | cut -d= -f2-); "
    "[ -z \"$l\" ] && l=$(grep -o '[^/]*[.]service' /proc/$q/cgroup 2>/dev/null | head -1); "
    "if [ -n \"$l\" ] && [ \"$l\" != 0 ]; then echo \"$l\"; return; fi; done; echo '-'; }; "
    "_chain @SELF@; "
    "echo '--- requester candidates (EZ-1577) ---'; "
    "cands=$(ps -axo pid=,args= 2>/dev/null | awk -v self=@SELF@ -v re='" + _REQUESTER_VERB_RE + "' "
    "'index($0, \"shutdown diagnostic\") {next} $1 == self {next} "
    # Direct matches (argv starts with the verb, e.g. the ``launchctl`` process itself) rank ahead of
    # shells/agents whose script text merely contains it.
    "match($0, re) {st=index($0, $2); m=$1 \"\\t\" substr($0, RSTART, RLENGTH); "
    "if (index(substr($0, st, RSTART-st), \" \")) ind[++ni]=m; else dir[++nd]=m} "
    "END{for(i=1;i<=nd;i++) print dir[i]; for(i=1;i<=ni;i++) print ind[i]}' | head -3); "
    "if [ -z \"$cands\" ]; then echo 'requester: none found (sender exited, launchd KeepAlive/jetsam, or bare kill)'; "
    "else tag=requester; printf '%s\\n' \"$cands\" | while IFS=\"$(printf '\\t')\" read -r pid cmd; do "
    "echo \"$tag: pid=$pid etime=$(_chain \"$pid\" etime) label=$(_label \"$(_chain \"$pid\" ids)\") "
    "cmd=[${cmd# }] chain=$(_chain \"$pid\")\"; tag=candidate; done; fi; "
)


_DIAGNOSTIC_DEDUPE_S = 2.0
_LAST_DIAGNOSTIC: List[float] = []


def spawn_async_diagnostic(log_path: Path, signal_name: str, *,
                           timeout_seconds: float = 5.0) -> Optional[int]:
    """Fire-and-forget ``ps``-style snapshot appended to ``log_path``: a detached subprocess (own
    ``timeout`` so a wedged ``ps`` self-cleans) rather than a blocking process listing in the signal
    handler, which can freeze the loop >2s on a busy host. Returns the subprocess PID, or ``None``
    on failure / Windows (bash -c is available on every POSIX target; Windows has no ps anyway).
    The listing is comm-only plus the requester's matched verb phrase: full argv is not persisted.
    """
    if sys.platform == "win32":
        return None
    # launchd/wrappers deliver the same SIGTERM twice ~20ms apart; one listing per burst (EZ-1577).
    now = time.monotonic()
    if _LAST_DIAGNOSTIC and now - _LAST_DIAGNOSTIC[0] < _DIAGNOSTIC_DEDUPE_S:
        return None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    _LAST_DIAGNOSTIC[:] = [now]
    script = _async_diagnostic_script(signal_name, os.getpid())
    try:  # O_APPEND so concurrent diagnostics from rapid signals don't trample each other
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return None
    with contextlib.suppress(OSError):  # tighten logs created 0644 by earlier releases
        os.fchmod(fd, 0o600)
    # GNU ``timeout`` (Homebrew: ``gtimeout``) is absent from stock macOS; without it the detached
    # script still cannot block teardown, so run it unbounded rather than skip the diagnostic.
    timeout_bin = shutil.which("timeout") or shutil.which("gtimeout")
    bound = [timeout_bin, f"{timeout_seconds:.0f}"] if timeout_bin else []
    try:  # start_new_session: outlive systemd killing our cgroup (KillMode=control-group) to flush
        return subprocess.Popen(
            [*bound, "bash", "-c", script], stdout=fd,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
            close_fds=True).pid
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):  # subprocess inherited the fd; drop our handle
            os.close(fd)


def format_context_for_log(ctx: Dict[str, Any]) -> str:
    """Render a shutdown context dict as one scannable log line (parent identity, never argv)."""
    parent = ctx.get("parent") or {}
    load_str = f"{load:.2f}" if isinstance(load := ctx.get("loadavg_1m"), (int, float)) else "?"
    extras: List[str] = []
    if ctx.get("takeover_marker") is not None:
        who = 'self' if ctx.get('takeover_marker_for_self') else 'other'
        extras.append(f"takeover_marker_present={who}")
    if ctx.get("planned_stop_marker") is not None:
        extras.append("planned_stop_marker_present=yes")
    if ctx.get("tracer_pid"):
        extras.append(f"tracer_pid={ctx['tracer_pid']}")
    extras_str = (" " + " ".join(extras)) if extras else ""
    return (
        f"signal={ctx.get('signal', '?')} under_systemd={'yes' if ctx.get('under_systemd') else 'no'} "
        f"parent_pid={parent.get('pid') or '?'} parent_name={parent.get('name') or '?'} "
        f"loadavg_1m={load_str}{extras_str}"
    )


def context_as_json(ctx: Dict[str, Any]) -> str:
    """JSON-serialise a context dict for structured ingestion.  Never raises."""
    try:
        return json.dumps(ctx, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return "{}"


def check_systemd_timing_alignment(
    drain_timeout: float, cron_drain_timeout: float = DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT
) -> Optional[Dict[str, Any]]:
    """At startup, sanity-check that systemd's TimeoutStopSec covers stop. A stale unit file
    (upgraded without re-running ``hermes setup``) can have ``TimeoutStopSec`` below the stop
    budget, so systemd SIGKILLs the cgroup mid-drain (a phantom ``code=killed status=9`` in the
    journal). ``None`` when aligned OR undeterminable (not under systemd, no ``systemctl``);
    otherwise a dict with ``timeout_stop_sec``/``drain_timeout``/``expected_min``/``mismatch``.
    """
    if not os.environ.get("INVOCATION_ID"):
        return None  # Not running under systemd (or at least not directly)
    # /proc/self/cgroup: "0::/user.slice/.../hermes-gateway.service"
    unit_name: Optional[str] = None
    with contextlib.suppress(OSError), open("/proc/self/cgroup", encoding="utf-8") as fh:
        for line in fh:
            parts = reversed(line.strip().split("/"))
            unit_name = next((p for p in parts if p.endswith(".service")), None)
            if unit_name:
                break
    if (timeout_us := _systemd_timeout_stop_us(unit_name) if unit_name else None) is None:
        return None
    timeout_stop_sec = timeout_us / 1_000_000.0
    expected = float(resolve_systemd_timeout_stop_sec(drain_timeout, cron_drain_timeout))
    return {"unit": unit_name, "timeout_stop_sec": timeout_stop_sec, "drain_timeout": drain_timeout,
            "cron_drain_timeout": cron_drain_timeout, "expected_min": expected,
            "mismatch": timeout_stop_sec < expected}


def _systemd_timeout_stop_us(unit_name: str) -> Optional[int]:
    """``TimeoutStopUSec`` of ``unit_name`` in microseconds; ``--user`` first (hermes' usual)."""
    for flag in (["--user"], []):
        try:
            result = subprocess.run(
                ["systemctl", *flag, "show", unit_name, "--property=TimeoutStopUSec"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2.0,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        # Output: "TimeoutStopUSec=1min 30s" or "TimeoutStopUSec=90000000"
        for line in result.stdout.splitlines() if result.returncode == 0 else ():
            if line.startswith("TimeoutStopUSec="):
                value = line.split("=", 1)[1].strip()
                timeout_us = int(value) if value.isdigit() else parse_systemd_duration_to_us(value)
                if timeout_us is not None:
                    return timeout_us
    return None


def parse_systemd_duration_to_us(raw: str) -> Optional[int]:
    """Parse 'TimeoutStopUSec=1min 30s' / '90s' style values to microseconds. Covers us, ms, s, min,
    h, d, w, month, y; a bare number is seconds. None on anything unexpected; never raises. Public: also consumed by
    hermes_cli.gateway's restart-wait sizing.
    """
    if not raw:
        return None
    units = {"us": 1, "ms": 1_000, "s": 1_000_000, "sec": 1_000_000,
             "min": 60_000_000, "h": 3_600_000_000, "hr": 3_600_000_000,
             # Fixed systemd time-util.h constants, not variable calendar months/years.
             "d": 86_400_000_000, "w": 604_800_000_000,
             "month": 2_629_800_000_000, "y": 31_557_600_000_000}
    total_us, token, digits = 0, "", ""

    def _flush() -> bool:  # fold the pending digits/token pair into total_us
        nonlocal total_us, token, digits
        multiplier = units.get(token.lower()) if token else 1_000_000
        if multiplier is None or not digits:
            return False
        try:
            total_us += int(float(digits) * multiplier)
        except (ValueError, OverflowError):
            return False
        digits = token = ""
        return True
    for ch in raw + " ":
        if ch.isdigit() or ch == ".":
            if token and not _flush():  # a digit after a unit ends the previous number
                return None
            digits += ch
        elif ch.isalpha():
            token += ch
        elif digits and not _flush():
            return None
    return total_us if total_us > 0 else None

