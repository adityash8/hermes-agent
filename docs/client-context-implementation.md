# Scoped client context implementation

## EZ-831 complete local acceptance (2026-09-08)

All eight named platforms now have fixed, identity-bound read operations: GA4,
Meta, Mixpanel, Tableau, PostHog, Stripe, Search Console and Google Ads. Cross-channel
approved source/decision retrieval, superseded records, reviewed source refresh and
owner tool preservation are covered end to end with synthetic transports.

See [complete capability matrix, operator command, deployment prerequisites and
verification evidence](ez831-completion-verification.md) for the current contract.
The offline command is `python -m gateway.client_context capabilities --manifest ABS_PATH`.
No live credentials, reports, configuration, source grants or services were changed.
The GA4-only section below records the earlier increment and is historical.

## EZ-831 bounded GA4 extension (2026-09-08)

Implemented in the isolated continuation worktree. **Live activation remains gated.**
No live credentials, customer records, configuration, grants, services, live checkout,
Slack messages, deployments or upstream changes were accessed or modified.
[Verification receipt](ez831-verification.md) contains the exact local test commands.
The EZ-826 material below is historical baseline documentation.

### Capability matrix

| Protected-turn capability | Status | Exact scope |
|---|---|---|
| `scoped_source_read` | Preserved | Current explicit hash-bound source grants only |
| `scoped_history_read` | Preserved | Explicit approved decision records and bounded scoped conversation history |
| `scoped_ga4_daily_report` | Implemented, synthetic execution verified | GA4 ordinary property; date dimension; `sessions`, `activeUsers`, `screenPageViews` |
| GA4 Admin property lookup | Executor-only | Identity verification before and after reporting; no model-facing account discovery |
| Google Ads, Search Console, Meta Ads, Mixpanel, Amplitude, PostHog, BigQuery | Unsupported | No scoped executors for these services |
| Arbitrary GA4 reports, real-time, event/user details, segments, dimensions, filters, exports | Unsupported | No schema or dispatch path |
| General Jarvis tools, MCP tools, browser, terminal, files outside grants, global memory/history | Unsupported in protected turns | Owner/unprotected paths retain existing behavior |
| Source/grant promotion, writes, live activation | Unsupported | Operator review and separate parent-owned activation required |

The current client requirements are Codewords and CookUnity. Both can use this
same connector **only if** the operator supplies a separately reviewed GA4
account/property/route binding and an authorized credential. No real account IDs,
GA4 availability, credentials, or remote results for either client were checked.
This increment does not restore every general analytics or Jarvis capability.

### Account-bound execution

`gateway/client_context_analytics.py` adds one fixed executor using the installed
`httpx` dependency. Research found no existing GA4/GSC/Ads execution helper to
reuse. The existing Microsoft Graph client demonstrates transport injection and
executor-side bearer credentials, but its arbitrary URLs, write methods and
pagination are incompatible with this boundary. Mixpanel/Amplitude MCP catalog
entries do not provide account/client/audience isolation. No global registration,
MCP discovery, generic dispatch or AIAgent fallback was added. Official Hermes
[toolsets](https://hermes-agent.nousresearch.com/docs/reference/toolsets-reference)
and [MCP documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp/)
are the permission-model references.

The operation is exposed only through the intersection of:

1. An unexpired approved analytics manifest grant matching the exact authenticated
   workspace/channel and route client/audience.
2. `read_tools.allow` containing `scoped_ga4_daily_report` with a valid generation.
3. The existing source-effective **`web`** toolset, intersected with platform base
   permissions, channel override and disabled toolsets. `web` alone grants nothing.

The model provides an exact grant/account/property/client/audience echo and two
absolute dates. Immutable executor bindings determine the HTTP resource and secret
slot. Independent enums are not combined across accounts; multiple grants use
complete per-grant schema alternatives. Unknown arguments, duplicate JSON keys,
SQL, URLs, paths, tool names, write methods, pagination, filters and arbitrary
metrics reject before any analytics I/O. The date window is visible in the schema.

Each execution performs these exact requests, without retries or redirects:

```text
GET  https://analyticsadmin.googleapis.com/v1beta/properties/{bound_property_id}
POST https://analyticsdata.googleapis.com/v1beta/properties/{bound_property_id}:runReport
GET  https://analyticsadmin.googleapis.com/v1beta/properties/{bound_property_id}
```

The sole POST is Google's read-only
[`runReport`](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport)
RPC. Its fixed body specifies `date`, the three metrics above, `limit: "31"`, and
one date range. Dates cover at most 31 completed days inside an operator-approved
window of at most 366 days. No request-body property override or next-page request
is accepted. Daily `activeUsers` must not be summed as distinct users for a period.

Both Admin responses must identify the bound property, account and parent account,
ordinary property type, configured timezone and no deletion/expiry marker.
[Google's property schema](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties)
is authoritative. Display names and all other Admin fields are discarded.
`runReport` does **not** echo account/property identity. Fixed authenticated Google
routing plus the two Admin checks is the account assurance; it is not independent
cryptographic proof that report data originated in that account. An incorrectly
approved operator binding or a dishonest provider remains a trust limitation.

Report validation accepts only the expected kind, exact metric/dimension headers,
integer metrics, unique in-window dates, complete row-count agreement, and the
configured timezone. Unexpected metadata, sampling, active restrictions,
thresholding or data loss deny; malformed types, nonfinite/negative/text metrics,
oversized responses and HTTP errors deny. Empty reports can return zero rows;
missing dates are not fabricated as zero. Returned currency codes, if present, are
validated but not exposed because no monetary metric is supported. See
[response metadata](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/ResponseMetaData).

HTTP uses TLS verification, fixed Google origins, `trust_env=False`, five-second
network timeouts, no redirects, and at most 65,536 response bytes per request.
Compressed responses are rejected. Grant/config/source/run-generation checks run
before/after each HTTP exchange and on every received chunk, then before provider
continuation and final release. Existing 30-second turn, four-round, eight-call,
and aggregate prompt/history budgets remain. Cancellation stops later requests;
an in-flight blocking read can remain until its network timeout, but its result
cannot continue or deliver. No automatic OAuth refresh or credential pool fallback
is provided. Expired/missing tokens and service/quota errors fail closed.

Only normalized daily numbers, dates, bound IDs, timezone and retrieval timestamp
enter the tool result. Results are explicitly observations, not decision approvals
or real-time metrics. Provider errors, raw metadata and tokens are never forwarded.
The existing final-text safeguards remain active. Retained results are dated
historical observations: follow-ups may reuse them; they are not silently refreshed.
Any manifest/policy/source change invalidates the existing scoped history seal.

### Manifest extension and offline operator validation

Version 1 optionally accepts an `analytics` array (absent/empty means no analytics).
The following is a **synthetic schema example**, not an authorized or active grant:

```json
{
  "id": "synthetic-ga4-a",
  "scope_id": "workspace-synthetic",
  "chat_id": "channel-synthetic",
  "client": "client-a",
  "audience": "internal",
  "account_id": "101",
  "property_id": "1001",
  "credential_slot": "SYNTHETIC_A",
  "time_zone": "UTC",
  "start_date": "2026-09-01",
  "end_date": "2026-09-07",
  "expires_at": "2026-10-01T00:00:00Z",
  "operation": "scoped_ga4_daily_report",
  "status": "proposed",
  "approval_evidence": null,
  "sharing_evidence": null
}
```

Every field is required. IDs must be bounded identifiers; GA account/property IDs
are positive decimal strings; credential slots are uppercase bounded identifiers.
The referenced workspace/channel route must exist and match client/audience.
Grant expiry cannot exceed route expiry. A property ID or credential slot cannot
cross client/account ownership within the manifest. Up to 64 grants are accepted.
Shared routes require explicit sharing evidence on approved analytics grants.
A proposal must have null approval/sharing evidence and exposes no tool. Approved
grants need a nonempty operator approval attestation; these references are trusted
operator assertions, not external approval verification. Expired approved grants
fail the affected route's snapshot before provider input.

Credentials are looked up **only during executor execution** using
`CLIENT_CONTEXT_GA4_{credential_slot}_ACCESS_TOKEN`. An installed profile scope is
authoritative; a missing scoped value cannot borrow the global environment. An
unscoped process uses the existing `agent.secret_scope.get_secret` semantics,
including failure under multiplexing. No `.env` file is opened by this connector,
and no credential names/values enter model schemas. A separately operated credential
source must provide a current token with `analytics.readonly` access and permissions
for the bound property through both Admin and Data APIs. This work creates none.

Offline validation remains:

```bash
python -m gateway.client_context validate --manifest /absolute/staged/manifest.json
```

It validates route/source bytes, hashes, account bindings, grant status/expiry and
schema. Its JSON receipt counts approved analytics grants and explicitly reports
`analytics_credentials_checked: false` and `activation: "not_performed"`.
It performs no analytics network calls or provider calls. Source refresh proposals
remain proposals. `client_context_refresh review` preserves analytics grants
exactly; source review cannot approve, broaden or renew them. Existing manifest
source IDs are the entire local source coverage. No automatic source discovery,
deep client report access, global history search or source approval expansion occurs.

### Deployment checklist (parent-owned; none performed here)

- Review the code, verification receipt and local diff, then merge this branch via
  the parent. Do not apply or reset over the dirty live checkout.
- Confirm the real Codewords/CookUnity GA4 availability and intended account,
  ordinary property, timezone, audience, date range, and explicitly approved
  source coverage. Keep deeper client reports ungranted.
- Stage separate operator-reviewed grants, approval/sharing evidence and credential
  slots. Arrange current read-only tokens outside this code change. Validate the
  staged manifest offline; review the exact tool-policy intersection.
- Obtain separate authorization for live activation and controlled reads. No
  live config/grant changes, service restart or deployment is authorized by this task.
- After authorized activation, verify a genuine authenticated human inbound turn,
  account metadata and bounded report, cross-client denial, source/grant revocation,
  owner regression and dated follow-up history. Synthetic tests do not replace this.
- Roll back by disabling the scoped operation or removing its approved grant under
  operator control. Observed policy change clears retained history on the next
  boundary; restart also discards the in-memory cache. No background monitor is added.


## EZ-826 local tool-preserving extension (2026-09-08)

Implemented locally; activation remains gated. No configuration, live grants,
profiles, credentials, running services, external accounts or deployments changed.
The earlier pilot receipt below is historical; the extension adds only the bounded
capabilities described here.

### Working capability

The original authenticated ingress and pre-provider turn seam are preserved.
`client_context_turn.py` provides a dedicated loop; it never constructs an AIAgent,
restores normal sessions, loads identity/memory/context files, dispatches global
tools, refreshes MCP agents or persists session tool prefixes.

Two **local record readers** are available when explicitly permitted:

| Function | Normal source-effective toolset required | Capability |
|---|---|---|
| `scoped_source_read` | `file` | One current, hash-bound granted source record |
| `scoped_history_read` | `session_search` | One explicitly granted approved decision record, including superseded history |

These are scoped substitutes for read capabilities, not the ordinary file or
session-search executors. Their source/account arguments are exact validated
`source_id`, `account_id`, `client`, and `audience` fields. `account_id` is the
authenticated Slack workspace scope, not an arbitrary analytics account. Every
executor call checks both function allowlisting and all four arguments. Paths,
foreign accounts, extra fields, duplicate JSON keys and ungranted IDs deny.

Read schemas derive from the normal `_resolve_turn_toolsets` result intersected
with the platform's base permissions and a static two-function allowlist. Adapter
overrides can narrow this set. Missing/empty/malformed platform lists, resolver
failures and disabled toolsets grant no tools. Adding a global MCP tool or a global
handler with a matching name cannot change the scoped executor. Schemas and valid
names come from one immutable local surface. Policy is recomputed at each boundary;
drift aborts the active turn, and the next turn constructs a fresh surface.

The existing gateway setting optionally accepts the following additional field.
This is a schema example only; **no configuration was edited or activated**:

```json
"read_tools": {
  "generation": "reviewed-policy-v1",
  "allow": ["scoped_source_read", "scoped_history_read"]
}
```

Absent or malformed read policy retains source-only Q&A with no read tools or
conversation history. Valid empty `allow` grants no tools. Provider/model remain
explicit. Ordinary owner DMs and unprotected behavior retain their original paths.

The scoped cache retains the exact validated message sequence for at most four
successful turns in each of 64 conversations, exclusively in memory. This includes
authorized evidence, tool calls/results and scoped provider replay. Follow-ups
append to the existing prompt prefix without rewriting it. A turn or serialized
payload budget overflow starts a completely fresh scoped conversation; individual
messages are never trimmed from a retained prefix. Retained messages are capped at
196,608 bytes per conversation, and outbound messages plus schemas share that cap.
Keys bind workspace, channel, thread,
requester, chat type, profile/relay identity, route/client/audience, manifest digest
and file identities, source identities, tool generation/effective policy and
provider/model. Policy mismatch, observed disable/downgrade, failed authorization,
revocation, cancellation, stale run generation and failed turns clear relevant
history (failure paths conservatively clear the entire scoped cache). Restart
also discards it. This is not continuous monitoring of changes between requests.

Cross-thread material comes only from explicitly granted decision records with
approved metadata. Ordinary transcripts are never searched or imported. Superseded
approved records are labeled historical; observations/proposals cannot become
approved history. Future records are excluded. Expired granted historical records
deny before any source-content read. The full current evidence plus approved
history snapshot shares the existing 98,304-byte limit.

The loop allows four provider rounds and eight total read calls, bounded by a
30-second turn deadline. It revalidates authorization, source state, run generation,
configuration, effective tool policy, manifest and source bytes before/after each
tool and provider round, immediately before the SDK request after provider
resolution, and again before returning an answer. Revoked/changed results are
discarded. A cancelled or timed-out resolver worker cannot initiate a later
request. Requests already in flight can finish, but cannot continue or deliver.
Retained tool results and opaque reasoning remain confined to that grant-scoped
conversation and are cleared with it; nothing is written to normal session storage.

Explicit OpenAI/OpenRouter Chat Completions and raw OpenAI Codex Responses use the
existing concrete HTTP SDK restriction. Codex encrypted reasoning is requested and
replayed only inside the same bounded scoped conversation, using the existing capture helper;
no server-stored response lookup or normal agent fallback is used. Existing
`safe_output` text/media/path/mention safeguards apply to final answers.

### Reviewed source refresh

`python -m gateway.client_context_refresh` provides an offline operator workflow.
The candidate root must be a separate complete local source tree with the same
relative paths. `propose` emits changed hashes with proposed status and null
approval/sharing attestations. It cannot modify the active manifest or source tree.

```text
python -m gateway.client_context_refresh propose --manifest ABS_MANIFEST \
  --candidate-root ABS_CANDIDATE_ROOT --output ABS_PROPOSAL
python -m gateway.client_context_refresh review --manifest ABS_MANIFEST \
  --candidate-root ABS_CANDIDATE_ROOT --decisions ABS_DECISIONS --output ABS_REVIEWED_MANIFEST
```

The decisions JSON binds both proposal digests and every changed source:

```json
{
  "base_digest": "digest-from-proposal",
  "candidate_digest": "digest-from-proposal",
  "reviews": [{
    "id": "decision-a",
    "sha256": "new-source-sha256",
    "review_evidence": "operator-review-id",
    "approval_evidence": "new-approval-id",
    "sharing_evidence": "new-sharing-id",
    "observed_at": "2026-09-08T00:00:00Z",
    "effective_at": "2026-09-08T00:00:00Z",
    "expires_at": "2027-01-01T00:00:00Z"
  }]
}
```

Approval/sharing references must be new for changed approved/shared bytes; otherwise
the corresponding field must be null. They remain trusted operator attestations,
not remote approval verification. Every changed record, including ungranted records,
requires explicit review. Source dates require explicit review; grants and owner
bindings cannot be renewed or broadened. Record status stays unchanged, so a
proposal cannot gain approved status through refresh. Snapshot validation includes
approved historical sources, and hashes are rechecked before artifact publication.
Exclusive descriptor-relative writes reject existing outputs, links and outputs in
either source tree. `review` only creates a **separate reviewed manifest**; there is
no promotion/activation command.

### Omissions and acceptance limits

This is bounded local retrieval, **not general analytics integration**. It has no
GA4/Ads/Search Console connectors, remote account queries, arbitrary MCP/file/network
tools, live metrics, global transcript search, writable memory, delegation, actions,
automatic approval, automatic grant renewal or automatic activation. Current source
records remain in the initial evidence packet; reads do not introduce an unbounded
search index. An operator must stage local source copies and review refresh evidence.

Verification uses real runtime imports, the HTTP SDK, synthetic identities, temporary
source trees, and mock HTTP transport through `scripts/run_tests.sh`. It proves local
contracts, not live Slack/provider compatibility or live analytics access. Static
type checking is unavailable in the existing venv (`python -m ty`: no module named
ty); no dependency was installed.

### EZ-826 verification receipt

The official runner passed **648 tests across 20 files, zero failures**, with
`--file-retries 0 -j 4`. This includes 131 existing scoped boundary tests, 79 new
executor/history/provider cases, 29 refresh cases, and Slack, relay, owner,
toolset-resolution and MCP-refresh regressions. Log: `/tmp/ez826-regressions.txt`.
The file set is the earlier 16-file pilot regression list below plus
`test_client_context_turn.py`, `test_client_context_refresh.py`,
`test_webhook_route_toolsets.py`, and `tests/tools/test_refresh_agent_mcp_tools.py`.

Ruff on `gateway/client_context*.py` and `tests/gateway/test_client_context*.py`,
Python compileall on changed Python, and `git diff --check` all pass. An independent
review approved the implementation after fixes for history downgrade/restore,
source-state comparison, stale provider workers and historical-source refresh
validation. The review independently ran the new suites (99 passes before the
last protocol/registry/generation and prefix-budget cases were added) and the existing scoped
suite (131 passes). The final 648-test run includes all final code changes.

No live provider/Slack run, full repository suite or remote CI pass is claimed.
The initial worktree HEAD is the implementation baseline. Its comparison with local
`origin/main` includes substantial preexisting divergence; no unrelated files were
reconciled. Preexisting untracked `research.md`, `tasks/` and
`.client-context-test-tmp/` remain untouched and outside the scoped change.

## Earlier pilot implementation receipt (historical)

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
