# EZ-831 implementation evidence

Status: implemented and locally verified on the isolated continuation worktree,
based on `3d65a02547` / `e7cc08f99b`. Live activation remains gated. Parent owns
merge and Linear updates. No push, upstream PR, deploy, restart, Slack post, real
credential/customer-data read, live grant/config edit or live API request occurred.

## Delivered capability

`scoped_ga4_daily_report` executes real HTTP client requests for one immutable,
approved GA4 account/property/client/audience binding. Exact operation:

- Admin `GET /v1beta/properties/{property_id}` on `analyticsadmin.googleapis.com`,
  before and after the report, validating property/account/parent/type/timezone.
- Data `POST /v1beta/properties/{property_id}:runReport` on
  `analyticsdata.googleapis.com`. This is a read-only report RPC. Fixed date
  dimension and metrics: **sessions, activeUsers, screenPageViews**.
- At most 31 completed days within an approved window of at most 366 days;
  at most 31 rows and 65,536 bytes per upstream response. No pagination/retries.

No generic registry execution, AIAgent/global-memory fallback, arbitrary URL/SQL,
write methods, dynamic tool names, arbitrary reports or account discovery. The
explicit analytics grant, scoped allowlist and effective channel/platform `web`
toolset must intersect. Credentials enter only executor HTTP headers. Returned
metadata and numbers are validated before model input; raw response text and
secrets are not forwarded. Explicit local source approvals, hash-bound coverage,
scoped history and owner/unprotected routes remain intact. Source refresh preserves
analytics grants without promoting proposals or widening approvals.

Codewords and CookUnity can each receive separate operator-approved bindings; no
real GA4 availability, account IDs, tokens or results for either client were checked.
Google Ads, Search Console, Meta Ads, Mixpanel, Amplitude, PostHog, BigQuery,
real-time/user/event-level GA4 and general Jarvis tools remain unsupported in the
protected analytics surface. [Technical documentation](client-context-implementation.md)
contains the full matrix, manifest example, offline validation and parent-owned
deployment checklist.

## Final canonical verification

Used the existing `/Users/adityasheth/.hermes/hermes-agent/venv` through the canonical
runner, with isolated temporary HERMES_HOME, cleared credentials, UTC/C.UTF-8 and
per-file subprocesses. No dependencies were installed. Exact final command:

```bash
scripts/run_tests.sh \
  tests/gateway/test_client_context.py \
  tests/gateway/test_client_context_turn.py \
  tests/gateway/test_client_context_refresh.py \
  tests/gateway/test_client_context_analytics.py \
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
  tests/gateway/test_webhook_route_toolsets.py \
  tests/tools/test_refresh_agent_mcp_tools.py \
  --file-retries 0 -j 4 > /tmp/ez831-regressions.txt 2>&1
```

Actual result: **21 files, 741 tests passed, 0 failed, 100% complete**, runner wall
11.9 seconds. Includes **89 analytics cases**, 131 existing scoped boundary cases,
83 existing scoped tool/history cases, 29 source refresh cases, plus owner, native
Slack, relay, channel-toolset and MCP-refresh regressions. No flaky retries used.

The analytics suite exercises the real gateway imports, real OpenAI SDK tool
rounds and actual httpx request/stream/JSON handling with synthetic MockTransport
responses. It verifies two client/account canaries, exact HTTP methods/resources
and body, secret/Admin-field exclusion, correlated grant argument validation,
request/date bounds, proposed/approved/shared grants, malformed outputs, metadata
mismatch, redirects/errors/oversize, source/grant/tool-policy revocation at each
HTTP exchange, provider continuation and final release, old history invalidation,
credential scope misses, delayed credential resolution/cancellation, small-chunk
revocation, offline validation/refresh, and analytics-present owner/unprotected
regressions. No HTTP request went to a live GA4 or model service.

Additional checks (all exit 0):

```bash
/Users/adityasheth/.hermes/hermes-agent/venv/bin/ruff check \
  gateway/client_context*.py tests/gateway/test_client_context*.py
/Users/adityasheth/.hermes/hermes-agent/venv/bin/python -m compileall -q \
  gateway/client_context.py gateway/client_context_policy.py \
  gateway/client_context_turn.py gateway/client_context_refresh.py \
  gateway/client_context_analytics.py tests/gateway/test_client_context_analytics.py
git diff --check
```

Ruff: `All checks passed!`. Compileall/diff checks: no errors. Static type-check
availability check, `.../venv/bin/python -m ty --version`, failed with
`No module named ty`; no static type-check pass is claimed. An early
`scripts/run_tests.sh --help` forwarded help to pytest while discovering files;
it provided no behavioral verification and is excluded from these results.

## Review and repairs

An independent agent researched integrations, reviewed the plan, then reviewed
the implementation and final documentation. Final verdict: **APPROVE**, no remaining
blockers. Review identified HTTP small-chunk buffering delaying revocation checks.
The executor now checks every received chunk. A real httpx stream reproduction
changed from consuming a later chunk after revocation to stopping, and the new
regression exercises this through the gateway/provider SDK path. Initial QA also
restored the existing local-executor rejection seam and corrected new tests to
expect silent suppression when authorization changes at continuation/release.
Final 741-test acceptance includes all code repairs.

Compared the incremental change with `3d65a02547` and inspected divergence from
local `origin/main`. The baseline already has 47 changed files versus the merge
base (5,333 insertions / 134 deletions); unrelated baseline work was not reconciled.
This task changes only the scoped analytics implementation, its tests, and local
research/plan/technical evidence. No protected config.yaml, AGENTS, SOUL or skill
files were changed.

## Remaining limitations

- Synthetic HTTP/SDK execution is not live account verification or a human Slack
  round trip. Full repository suite and remote CI were not run.
- GA4 reports do not echo account identity. Fixed authenticated Google routing plus
  pre/post Admin verification trusts the provider and operator-approved binding.
- Ordinary GA4 properties only. External token issuance/refresh and Admin/Data API
  permissions are operator prerequisites; missing/expired credentials fail closed.
- Sampling, thresholding, active restrictions, truncation and unexpected metadata
  fail closed rather than returning a misleading partial report.
- A network operation already in flight may finish after cancellation, bounded by
  network timeout. It cannot initiate the next request, continue to the provider
  or release results. No continuous revocation monitor is introduced.
- Cached report observations keep their retrieval timestamps; daily active users
  cannot be summed as distinct period users. Source reports remain limited to
  explicit manifest grants; no deeper client reports are automatically released.

## Git handoff

Staging the ten scoped implementation/test/document files was attempted with
`git add` and blocked by the filesystem sandbox before the index changed:

```text
fatal: Unable to create '/Users/adityasheth/.hermes/hermes-agent/.git/worktrees/ez-831-direct/index.lock': Operation not permitted
```

No workaround or security bypass was attempted. Changes remain **unstaged and
uncommitted** in this isolated worktree. No commit or merge was attempted after
this rejection. Parent must stage/commit/merge from an authorized session; the
verified code and this receipt are the handoff.
