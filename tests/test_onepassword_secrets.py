"""Hermetic tests for the 1Password (`op` CLI) secret source.

We never invoke the real ``op`` binary: either ``subprocess.run`` is mocked or
a fake ``op`` script (the ``fake_op_binary`` fixture) stands in, so the suite
stays fast and offline-safe.  A live resolve is exercised manually via
``hermes secrets onepassword sync`` outside of pytest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest


# Make the worktree importable without depending on the installed wheel.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.secret_sources import onepassword as op  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_caches():
    op._reset_cache_for_tests()
    yield
    op._reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _clean_op_env(monkeypatch):
    """Start every test from a known 1Password auth state."""
    for key in list(os.environ):
        if key.startswith("OP_SESSION_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("OP_ACCOUNT", raising=False)
    monkeypatch.delenv("OP_CONNECT_HOST", raising=False)
    monkeypatch.delenv("OP_CONNECT_TOKEN", raising=False)
    yield


def _ok(value: str):
    return mock.Mock(returncode=0, stdout=value, stderr="")


def _err(code: int, stderr: str):
    return mock.Mock(returncode=code, stdout="", stderr=stderr)


def _dispatching_run(values, calls):
    """Mocked ``subprocess.run`` that understands ``op inject`` and ``op read``.

    ``values`` maps reference → secret; ``calls`` collects each argv (thread-safe
    ``list.append`` — fallback reads run in a pool)."""
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[1] == "inject":
            out = kwargs["input"]
            for ref, value in values.items():
                out = out.replace("{{ %s }}" % ref, value)
            if "{{ " in out:
                return _err(1, "[ERROR] item does not have a field")
            return _ok(out)
        return _ok(values[cmd[cmd.index("--") + 1]] + "\n")
    return fake_run


# Stand-in `op` binary: values come from op_values.json beside the script; each
# call's argv (and inject template, which holds references only) is appended to
# op_calls.log. `inject` is all-or-nothing like the real CLI.
_FAKE_OP_SOURCE = r'''
import json, re, sys
from pathlib import Path

here = Path(__file__)
values = json.loads(here.with_name("op_values.json").read_text())
args = sys.argv[1:]
stdin = sys.stdin.read() if args[:1] == ["inject"] else None
with here.with_name("op_calls.log").open("a") as log:
    log.write(json.dumps({"argv": args, "stdin": stdin}) + "\n")

if args[:1] == ["inject"]:
    def resolve(match):
        ref = match.group(1)
        if ref not in values:
            sys.stderr.write("[ERROR] item does not have a field '%s'\n" % ref.rsplit("/", 1)[-1])
            sys.exit(1)
        return values[ref]
    sys.stdout.write(re.sub(r"\{\{ (.+?) \}\}", resolve, stdin))
elif args[:1] == ["read"] and "--" in args:
    ref = args[args.index("--") + 1]
    if ref not in values:
        sys.stderr.write("[ERROR] could not read secret '%s': field not found\n" % ref)
        sys.exit(1)
    sys.stdout.write(values[ref] + "\n")
else:
    sys.exit(2)
'''


@pytest.fixture
def fake_op_binary(tmp_path):
    """``make(values) -> Path`` of an executable fake ``op`` serving ``values``."""
    def make(values):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / "op"
        script.write_text(f"#!{sys.executable}\n{_FAKE_OP_SOURCE}")
        script.chmod(0o755)
        (bin_dir / "op_values.json").write_text(json.dumps(values))
        return script
    return make


def _op_calls(script: Path):
    log = script.with_name("op_calls.log")
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


# ---------------------------------------------------------------------------
# Reference validation
# ---------------------------------------------------------------------------


def test_validate_references_filters_bad_names_and_refs():
    refs = {
        "OPENAI_API_KEY": "op://Private/OpenAI/api key",
        "1BAD_NAME": "op://Private/x/y",          # bad env name
        "HAS SPACE": "op://Private/x/y",          # bad env name
        "NOT_A_REF": "https://example.com",        # not op://
        "WHITESPACE": "  op://Private/z/field  ",  # stripped + kept
    }
    valid, warnings = op._validate_references(refs)
    assert valid == {
        "OPENAI_API_KEY": "op://Private/OpenAI/api key",
        "WHITESPACE": "op://Private/z/field",
    }
    assert len(warnings) == 3


# ---------------------------------------------------------------------------
# fetch_onepassword_secrets
# ---------------------------------------------------------------------------


def test_fetch_happy_path_is_one_inject_call(fake_op_binary):
    fake_op = fake_op_binary({
        "op://Private/OpenAI/api key": "sk-abc\n",
        "op://Private/Anthropic/credential": "sk-ant-xyz",
        "op://Private/Deploy/ssh key": "-----BEGIN KEY-----\nline2\n-----END KEY-----",
    })

    secrets, warnings = op.fetch_onepassword_secrets(
        references={
            "OPENAI_API_KEY": "op://Private/OpenAI/api key",
            "ANTHROPIC_API_KEY": "op://Private/Anthropic/credential",
            "DEPLOY_KEY": "op://Private/Deploy/ssh key",
        },
        account="acme", binary=fake_op, use_cache=False,
    )
    assert secrets == {
        "OPENAI_API_KEY": "sk-abc",  # op's trailing newline convention stripped
        "ANTHROPIC_API_KEY": "sk-ant-xyz",
        "DEPLOY_KEY": "-----BEGIN KEY-----\nline2\n-----END KEY-----",  # multiline intact
    }
    assert warnings == []
    calls = _op_calls(fake_op)
    assert [c["argv"] for c in calls] == [["inject", "--account", "acme"]]
    # References travel on stdin, never argv.
    assert "op://Private/OpenAI/api key" in calls[0]["stdin"]


def test_fetch_dedups_shared_reference(fake_op_binary):
    fake_op = fake_op_binary({"op://V/Shared/key": "shared-val", "op://V/Other/key": "other-val"})

    secrets, warnings = op.fetch_onepassword_secrets(
        references={"A_KEY": "op://V/Shared/key", "B_KEY": "op://V/Shared/key",
                    "C_KEY": "op://V/Other/key"},
        binary=fake_op, use_cache=False,
    )
    assert secrets == {"A_KEY": "shared-val", "B_KEY": "shared-val", "C_KEY": "other-val"}
    assert warnings == []
    (call,) = _op_calls(fake_op)
    assert call["stdin"].count("op://V/Shared/key") == 1


def test_inject_failure_falls_back_to_per_reference_reads(fake_op_binary, tmp_path):
    good = {f"op://V/I{i}/key": f"sk-live-SECRET-{i}" for i in range(10)}
    fake_op = fake_op_binary(good)
    references = {f"KEY_{i}": ref for i, ref in enumerate(good)}
    references["BROKEN_A"] = references["BROKEN_B"] = "op://V/Gone/key"
    op._reset_cache_for_tests(tmp_path)

    secrets, warnings = op.fetch_onepassword_secrets(
        references=references, binary=fake_op, cache_ttl_seconds=300, home_path=tmp_path,
    )
    # Every good reference still resolves; the bad one is reported once, naming
    # both affected env vars and the reference.
    assert secrets == {f"KEY_{i}": f"sk-live-SECRET-{i}" for i in range(10)}
    assert len(warnings) == 1
    assert warnings[0].startswith("BROKEN_A, BROKEN_B: op read failed for 'op://V/Gone/key'")
    assert not any(value in w for value in good.values() for w in warnings)

    argvs = [c["argv"] for c in _op_calls(fake_op)]
    assert argvs[0] == ["inject"]
    assert sorted(a[-1] for a in argvs[1:]) == sorted({*good, "op://V/Gone/key"})
    assert all(a[:2] == ["read", "--"] for a in argvs[1:])  # `--` before every ref

    # A pull with any error is never cached: the next fetch goes back to op.
    assert not (tmp_path / "cache" / "op_cache.json").exists()
    op._CACHE.clear()
    op.fetch_onepassword_secrets(
        references=references, binary=fake_op, cache_ttl_seconds=300, home_path=tmp_path,
    )
    assert len(_op_calls(fake_op)) > len(argvs)


def test_empty_value_is_rejected_and_not_cached(fake_op_binary, tmp_path):
    fake_op = fake_op_binary({"op://V/I/full": "val", "op://V/I/blank": ""})
    op._reset_cache_for_tests(tmp_path)

    secrets, warnings = op.fetch_onepassword_secrets(
        references={"FULL": "op://V/I/full", "BLANK": "op://V/I/blank"},
        binary=fake_op, cache_ttl_seconds=300, home_path=tmp_path,
    )
    assert secrets == {"FULL": "val"}
    assert warnings == ["BLANK: op read returned an empty value for 'op://V/I/blank'"]
    assert not (tmp_path / "cache" / "op_cache.json").exists()


@pytest.mark.parametrize("unsafe_ref", ["op://V/I/$FIELD", "op://V/I/${FIELD}", "op://V/I/a{b", "op://V/I/a}b"])
def test_reference_with_variable_syntax_bypasses_inject(fake_op_binary, unsafe_ref):
    # `op inject` would expand $FIELD from the environment (and braces would
    # corrupt the template); `op read` takes the reference literally.
    fake_op = fake_op_binary({unsafe_ref: "literal", "op://V/I/plain": "p"})

    secrets, warnings = op.fetch_onepassword_secrets(
        references={"UNSAFE": unsafe_ref, "PLAIN": "op://V/I/plain"},
        binary=fake_op, use_cache=False,
    )
    assert secrets == {"UNSAFE": "literal", "PLAIN": "p"}
    assert warnings == []
    calls = _op_calls(fake_op)
    inject = [c for c in calls if c["argv"][0] == "inject"]
    assert len(inject) == 1 and unsafe_ref not in inject[0]["stdin"]
    assert ["read", "--", unsafe_ref] in [c["argv"] for c in calls]


def test_partial_inject_output_retries_only_missing_refs(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls = []
    values = {"op://V/I/a": "va", "op://V/I/b": "vb", "op://V/I/c": "vc"}
    dispatch = _dispatching_run(values, calls)

    def fake_run(cmd, **kwargs):
        if cmd[1] == "inject":
            # Drop b's marker line entirely, as if op had mangled it.
            kwargs["input"] = "".join(line for line in kwargs["input"].splitlines(keepends=True)
                                      if "op://V/I/b" not in line)
        return dispatch(cmd, **kwargs)

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    secrets, warnings = op.fetch_onepassword_secrets(
        references={"A": "op://V/I/a", "B": "op://V/I/b", "C": "op://V/I/c"},
        account="acme", binary=fake_op, use_cache=False,
    )
    assert secrets == {"A": "va", "B": "vb", "C": "vc"}
    assert warnings == []
    # Only the missing reference is re-read, and the fallback keeps --account.
    assert [c[1:] for c in calls] == [["inject", "--account", "acme"],
                                      ["read", "--account", "acme", "--", "op://V/I/b"]]


def test_fallback_reads_see_per_fetch_source_environment(monkeypatch, tmp_path):
    # Profile hydration installs a per-fetch env view (a ContextVar); the pooled
    # `op read` threads must build their child env from it, not os.environ.
    from agent.secret_sources.base import reset_source_environment, set_source_environment

    fake_op = tmp_path / "op"
    fake_op.write_text("")
    read_envs = []

    def fake_run(cmd, **kwargs):
        if cmd[1] == "inject":
            return _err(1, "[ERROR] boom")
        read_envs.append(kwargs["env"])
        return _ok("v\n")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    token = set_source_environment({"OP_SESSION_profile": "sess", "OP_ACCOUNT": "profile-acct"})
    try:
        secrets, warnings = op.fetch_onepassword_secrets(
            references={"K1": "op://V/I/one", "K2": "op://V/I/two"}, binary=fake_op, use_cache=False,
        )
    finally:
        reset_source_environment(token)
    assert secrets == {"K1": "v", "K2": "v"}
    assert warnings == []
    assert len(read_envs) == 2
    assert all(env.get("OP_SESSION_profile") == "sess" and env.get("OP_ACCOUNT") == "profile-acct"
               for env in read_envs)


def test_inject_timeout_falls_back_to_reads(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls = []
    reads = _dispatching_run({"op://V/I/F": "v"}, calls)

    def fake_run(cmd, **kwargs):
        if cmd[1] == "inject":
            raise subprocess.TimeoutExpired(cmd, 30)
        return reads(cmd, **kwargs)

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    secrets, warnings = op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, binary=fake_op, use_cache=False,
    )
    assert secrets == {"K": "v"}
    assert warnings == []



def test_fetch_read_failure_becomes_warning(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(
        op.subprocess, "run", lambda *a, **k: _err(1, "\x1b[31m[ERROR] not signed in\x1b[0m")
    )

    secrets, warnings = op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, binary=fake_op, use_cache=False
    )
    assert secrets == {}
    assert len(warnings) == 1
    # ANSI control sequences are fully scrubbed from the surfaced message.
    assert "\x1b" not in warnings[0]
    assert "[31m" not in warnings[0]
    assert "not signed in" in warnings[0]










# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_inprocess_cache_hit(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls = []
    monkeypatch.setattr(op.subprocess, "run", _dispatching_run({"op://V/I/F": "v"}, calls))
    op._reset_cache_for_tests(tmp_path)
    for _ in range(2):
        op.fetch_onepassword_secrets(
            references={"K": "op://V/I/F"}, cache_ttl_seconds=60,
            binary=fake_op, home_path=tmp_path,
        )
    assert len(calls) == 1  # second call served from L1 cache








def test_connect_credential_change_invalidates_cache(monkeypatch, tmp_path):
    """A different 1Password Connect identity must not reuse a cached value."""
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    calls = []
    monkeypatch.setattr(op.subprocess, "run", _dispatching_run({"op://V/I/F": "v"}, calls))
    op._reset_cache_for_tests(tmp_path)

    monkeypatch.setenv("OP_CONNECT_HOST", "https://connect.example.com")
    monkeypatch.setenv("OP_CONNECT_TOKEN", "tokenA")
    op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, cache_ttl_seconds=300,
        binary=fake_op, home_path=tmp_path,
    )
    # Rotate the Connect token → new identity.
    monkeypatch.setenv("OP_CONNECT_TOKEN", "tokenB")
    op._CACHE.clear()
    op.fetch_onepassword_secrets(
        references={"K": "op://V/I/F"}, cache_ttl_seconds=300,
        binary=fake_op, home_path=tmp_path,
    )
    assert len(calls) == 2  # cache key changed → refetch






# ---------------------------------------------------------------------------
# find_op
# ---------------------------------------------------------------------------


def test_find_op_pinned_path_not_on_path(tmp_path, monkeypatch):
    pinned = tmp_path / "op"
    pinned.write_text("")
    pinned.chmod(0o755)
    # PATH lookup must NOT be consulted when a binary_path is pinned.
    monkeypatch.setattr(op.shutil, "which", lambda name: "/usr/bin/op")
    assert op.find_op(str(pinned)) == pinned




# ---------------------------------------------------------------------------
# apply_onepassword_secrets
# ---------------------------------------------------------------------------


def test_apply_disabled_returns_empty():
    result = op.apply_onepassword_secrets(enabled=False, env={"K": "op://V/I/F"})
    assert result.ok
    assert not result.applied


def test_apply_missing_binary_sets_error(monkeypatch):
    monkeypatch.setattr(op, "find_op", lambda binary_path="": None)
    result = op.apply_onepassword_secrets(
        enabled=True, env={"K": "op://V/I/F"}
    )
    assert not result.ok
    assert "op CLI" in result.error


def test_apply_sets_env(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _ok("resolved-val"))
    monkeypatch.delenv("MY_OP_KEY", raising=False)

    result = op.apply_onepassword_secrets(
        enabled=True, env={"MY_OP_KEY": "op://V/I/F"}, cache_ttl_seconds=0,
    )
    assert result.ok
    assert result.applied == ["MY_OP_KEY"]
    assert os.environ["MY_OP_KEY"] == "resolved-val"


def test_apply_skips_before_fetch_when_not_overriding(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setenv("MY_OP_KEY", "from-env")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("from-1password")

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    result = op.apply_onepassword_secrets(
        enabled=True, env={"MY_OP_KEY": "op://V/I/F"},
        override_existing=False, cache_ttl_seconds=0,
    )
    assert "MY_OP_KEY" in result.skipped
    assert os.environ["MY_OP_KEY"] == "from-env"
    assert calls["n"] == 0  # never even called op for a value we'd discard


def test_apply_never_overrides_token_var(monkeypatch, tmp_path):
    fake_op = tmp_path / "op"
    fake_op.write_text("")
    monkeypatch.setattr(op, "find_op", lambda binary_path="": fake_op)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "original")
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _ok("malicious")

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    result = op.apply_onepassword_secrets(
        enabled=True,
        env={"OP_SERVICE_ACCOUNT_TOKEN": "op://V/I/F"},
        override_existing=True, cache_ttl_seconds=0,
    )
    assert "OP_SERVICE_ACCOUNT_TOKEN" in result.skipped
    assert os.environ["OP_SERVICE_ACCOUNT_TOKEN"] == "original"
    assert calls["n"] == 0




