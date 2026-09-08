# Scoped client context implementation

Status: the four findings in the parent review are fixed and locally verified
(2026-09-08). Work remains isolated to this worktree. This continuation used no live
Slack/provider calls, real client source reads, external mutations, child agents,
installs, commits or pushes. Parent owns packaging and tracker updates.

## Boundary and early findings

This MVP is stateless source-grounded Slack Q&A. It constructs no AIAgent and has
no history, memory, skills, tools, plugins, delegation, or action capability.
`gateway/run_turn.py::_handle_message_with_agent` is before session resolution.
`gateway/run_inbound.py::_handle_message` dispatches commands before that seam and
runs post-turn hooks afterward, so an additional authenticated ingress gate is
required. Scoped turns must bypass both command dispatch and post-turn hooks.
The existing raw gateway config reader returns `{}` on parse errors; an enabled
policy must not use that fail-open result as permission to enter the legacy path.

## Exact proposed schema v1

JSON manifest, UTF-8, duplicate keys and unknown fields rejected. All fields below
are required unless explicitly marked optional. No environment interpolation.

Gateway setting (read-only runtime configuration):

```yaml
gateway:
  client_context:
    enabled: true
    manifest: /absolute/operator/manifest.json
    provider: openai-codex
    model: explicit-main-model-id
```

Missing setting or exactly `{enabled: false}` retains legacy behavior. Any other
malformed present setting denies Slack. Provider/model are explicitly chosen by
the operator for this main completion; no auto routing, session override, or
fallback. The manifest is reloaded for every turn and checked again at release.

```json
{
  "version": 1,
  "root": "/absolute/canonical/source/root",
  "routes": [{
    "scope_id": "workspace-synthetic",
    "chat_id": "channel-synthetic",
    "client": "client-a",
    "audience": "internal",
    "source_ids": ["brief-a"],
    "grant_evidence": "grant-a",
    "expires_at": "2099-01-01T00:00:00Z"
  }],
  "owner_private": [],
  "sources": [{
    "id": "brief-a",
    "client": "client-a",
    "audiences": ["internal"],
    "path": "client-a/brief.txt",
    "sha256": "64-lowercase-hex-characters",
    "kind": "brief",
    "status": "observation",
    "source_ref": "brief-a",
    "observed_at": "2026-09-08T00:00:00Z",
    "effective_at": "2026-09-08T00:00:00Z",
    "expires_at": "2099-01-01T00:00:00Z",
    "approval_evidence": null,
    "sharing_evidence": null,
    "decision_key": null,
    "supersedes": []
  }]
}
```

- Identifiers: bounded ASCII letters/digits/underscore/hyphen, never paths.
- Audiences: `internal` or `shared`. A shared source also requires nonempty
  `sharing_evidence` (an opaque operator attestation ID); a route always requires
  `grant_evidence`. These attestations are trusted operator assertions, not model
  judgments or automated checks of a remote approval system.
- Owner bypass entries: exactly `scope_id`, `chat_id`, `user_id`, `chat_type`
  (must be `dm`), `grant_evidence`, `expires_at`. Exact tuple only; overlapping
  channel bindings and duplicate tuples are invalid.
- Kinds: `brief`, `research`, `decision`, `metric`. Status: `observation`,
  `proposed`, `approved`, `unresolved`. Only decisions may be approved; approved
  decisions require opaque `approval_evidence` and `decision_key`. Every decision
  requires `decision_key`. Other records require both fields null.
- `supersedes`: IDs of same-client, same-audience, same-kind records (same
  decision key for decisions). Unknown IDs, cycles, and invalid approval
  transitions deny. Multiple current approved decisions for a key deny.
- Future-effective records are excluded; superseded records are excluded from
  current content. V1 historical questions receive an explicit limitation rather
  than access to excluded historical bytes. Metrics are always labeled
  `historical; needs_live_verification`, never live facts.
- `source_ref`: safe opaque ID or HTTPS citation URL without credentials. Local
  paths, grant/approval/sharing attestations, and registry internals never enter
  the provider request. Only authorized source bytes and safe metadata do.
- All timestamps are UTC RFC3339 seconds with trailing `Z`; expiry is mandatory.
  Manifest/source file counts, sizes, question and response sizes are bounded.
  Relative source paths must stay under root; reject traversal, symlinks,
  nonregular files and multiply linked files. SHA-256 binds the exact bytes.

## Progress

- [x] Read task, gateway guidance, and exact turn/ingress seams.
- [x] Write early schema and progress artifact.
- [x] Implement strict manifest/grants/snapshots and local CLI.
- [x] Implement tool-free transport and both gateway gates.
- [x] Exercise real hooks/provider boundary with synthetic temporary sources.
- [x] Run canonical tests, ruff, syntax checks and review; fix the parent findings.
- [x] Record exact proof, file list, caveats and activation constraints.
- Static type checking unavailable: the existing venv has no `ty` executable.

## Parent review fixes

1. Native Slack admission now precedes `_prefilter_inbound`, bot/user lookups,
   thread wake reads, deletion-summon processing, enrichment and file downloads.
   A gateway-bound callback resolves authenticated workspace/channel/actor identity
   against the local manifest. Unknown, expired, malformed or unauthorized routes
   terminate at ingress. Missing identity never comes from caches, forwarded text,
   source labels, message authors in deletion payloads, or action values.
2. Exact owner DMs retain the legacy message/media path, approval and plugin buttons,
   reactions, busy approval handling and platform hooks. Restricted routes retain
   gateway and adapter authorization, ignored/allowed channel rules, DM disabling,
   mention rules, declared/cached bot rejection, and replay deduplication. Restricted
   turns never perform legacy thread wake lookups. File-share fallback and assistant
   lifecycle handlers also require owner admission before reading or changing state.
   Relay prompt/media handling uses the same owner decision. An event already marked
   scoped cannot become a legacy command through a subsequent config or grant change,
   including while busy.
3. `compare objective / bid strategy / attribution window` passes `safe_output`.
   Actual absolute path tokens (including quoted and Windows paths), file/media
   directives and images remain rejected. Slack mentions remain escaped/inert.
4. A source may contain up to 65,536 bytes. The complete serialized evidence packet
   remains capped at 98,304 bytes, with at most 16 granted sources per route. Reads
   remain descriptor-relative and bounded, with size checked before reading and
   identity/hash checks retained. The regression uses exactly 29,722 synthetic audit
   bytes plus orientation and scorecard records, and also rejects per-source and
   aggregate packet overflow.

The native boundary assumes Bolt has authenticated the transport before invoking
its listener. It uses the outer workspace identity and rejects contradictory inner
workspace fields. Native owner DMs require a Slack DM channel ID and a matching
actor; events without their own usable actor/destination fail closed. Scoped Q&A
accepts current messages and file-share message denials; edit/deletion events cannot
borrow the original author's identity to gain access.

## Verification receipt

Python: existing fallback venv, Python 3.11.15, pytest 9.1.1. All tests ran through
`scripts/run_tests.sh`, with isolated temporary `HERMES_HOME`, clean credentials,
UTC/C.UTF-8 and per-file subprocess isolation. File retries were disabled.

```bash
scripts/run_tests.sh \
  tests/gateway/test_client_context.py \
  tests/gateway/test_slack.py \
  tests/gateway/test_slack_approval_buttons.py \
  tests/gateway/test_slack_clarify_buttons.py \
  tests/gateway/test_slack_plugin_action_handlers.py \
  tests/gateway/test_slack_bolt_deletion_ingress.py \
  tests/gateway/test_slack_message_deletion.py \
  tests/gateway/test_slack_api_human_senders.py \
  tests/gateway/test_slack_bot_auth_bypass.py \
  tests/gateway/test_slack_wake_external_bot_messages.py \
  tests/gateway/test_slack_require_mention_channels.py \
  tests/gateway/test_slack_ignore_other_user_mentions.py \
  tests/gateway/test_gateway_platform_event_hook.py \
  tests/gateway/test_busy_session_auth_bypass.py \
  tests/gateway/relay/test_relay_adapter.py \
  tests/gateway/relay/test_relay_slack_prompt_dm_root.py \
  --file-retries 0
```

Final result: 16 files, **511 passed, 0 failed**, including **126 scoped tests**.
Complete console output: `/tmp/client-context-regressions.txt`.

Native tests run the actual prefilter, wake and deletion implementations with an
enabled fake Slack client, and assert zero transport calls or deletion-state
mutation for denied/restricted ingress. They also monitor forbidden enrichment
calls. Owner tests exercise real media collection/document caching in temporary
storage, approval resolution, reaction dispatch and gateway hooks with fake I/O.
Existing source metadata isolation, expiry/revocation, text-only delivery, real HTTP
SDK request construction and model-tool rejection tests all remain green.

Sensitivity checks:

- Before the two P2 fixes, the new audit-size and slash-prose regressions failed:
  `2 failed, 99 deselected`.
- Temporarily removing early native admission caused all five ingress cases to fail:
  `5 failed, 119 deselected`. Output: `/tmp/client-context-native-red.txt`.
- Temporarily applying scoped restrictions to the owner caused the owner behavior
  test to fail: `1 failed, 123 deselected`. Output: `/tmp/client-context-owner-red.txt`.
- Both temporary mutations were restored before final verification.

Ruff command (the existing venv's executable):

```bash
/Users/adityasheth/.hermes/hermes-agent/venv/bin/ruff check \
  gateway/config.py gateway/config_loader.py \
  gateway/client_context.py gateway/client_context_policy.py \
  gateway/platforms/base.py gateway/run_adapters.py gateway/run_busy.py \
  gateway/run_inbound.py gateway/run_turn.py gateway/relay/adapter.py \
  plugins/platforms/slack/adapter.py plugins/platforms/slack/client_context.py \
  tests/gateway/test_client_context.py
```

Output: `All checks passed!` (exit 0). Python `compileall -q` on those Python files
and `git diff --check` both exit 0 with no output. Static type-check attempt returned
exit 127: `ty` is absent from the existing venv. No dependencies were installed.

## Follow-up review and handoff

Reviewed the changed boundaries against the parent's four findings and the tracked
diff against local `origin/main`. Also reviewed the new policy, native admission and
test files directly because they are still untracked. No actionable findings remain
within these four fixes. This was a local self-review; no new independent reviewer
or live Slack verification is claimed.

Files changed in this continuation:

- `gateway/client_context.py`, `gateway/client_context_policy.py`
- `gateway/platforms/base.py`, `gateway/run_adapters.py`, `gateway/run_busy.py`
- `gateway/relay/adapter.py`
- `plugins/platforms/slack/adapter.py`, `plugins/platforms/slack/client_context.py` (new)
- `tests/gateway/test_client_context.py`
- `docs/client-context-implementation.md`

Limits: fixtures use synthetic identities and content; the audit test matches the
parent's measured byte size, not its private contents. No live human-authored Slack
round trip, production activation, full repository suite or static type-check pass
is claimed. Parent must include the new native helper when packaging the existing
uncommitted implementation.

## Parent final review repair

Independent review found relay reply-routing caches and replay state changed before
admission. Admission now runs first; denied, revoked, missing-identity, unauthorized,
and unwired events return before either mutation. Five behavioral tests retain the
real `_capture_scope` implementation and assert existing recipient caches and replay
state remain unchanged. The scoped relay test now wires the real runner callback.

Parent canonical union after repair: **747 passed, 0 failed, 1 skipped across 30 files**,
including 131 scoped tests. This is targeted regression acceptance, not the full suite.
Parent separately exercised actual Codewords/CookUnity local records through the real
OpenAI Codex provider with synthetic identities; no live Slack round trip is claimed.
