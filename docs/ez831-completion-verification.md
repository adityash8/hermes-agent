# EZ-831 complete local acceptance

Status: **all eight named analytics platforms implemented and synthetically verified**,
plus cross-channel approved source/decision retrieval, superseded history, reviewed
refresh and owner-path preservation. This completes local acceptance beyond the
GA4-only base `8f9b05454a`. Live activation is a separate parent-owned gate.

Implementation uses synthetic vendor evidence. No customer credential scopes or
real customer APIs were verified. No live checkout, configuration, AGENTS/SOUL,
profiles, active grants or services were changed. Parent tracking updates and
read-only approval-record checks are recorded in Linear. Existing untracked
`research.md` and `tasks/` are preserved. Local integration is not live activation.

## Exact supported capabilities

All analytics operations require an approved immutable grant matching authenticated
Slack workspace/channel, client ID and audience; an explicit `read_tools.allow`
entry; and source-effective `web` permission intersected with platform/channel and
disabled-toolset policy. Display names never establish client identity. Every
model argument is an exact binding echo except the bounded requested date range.
These are deliberately narrow operations, **not full vendor API parity**.

| Platform / tool | Actual transport and numeric result | Binding and limits |
| --- | --- | --- |
| GA4 / `scoped_ga4_daily_report` | Admin property GET; Data `runReport` POST; Admin property GET. Daily sessions, activeUsers, screenPageViews. | Ordinary property/account/parent/timezone verified before and after. Existing GA4 grant format retained. |
| Meta / `scoped_meta_daily_insights` | Graph v26.0 account GET; account `/insights` GET; account GET. Daily impressions, clicks, spend plus approved currency. | Numeric ad account and owning business ID; timezone/currency checked before and after. Fixed fields, account level, day interval. Pagination URLs rejected. Business-less accounts unsupported. |
| Mixpanel / `scoped_mixpanel_daily_event_count` | Regional `/api/query/segmentation` GET, Basic service-account auth. Daily total count of one approved literal event. | Explicit project and workspace IDs, fixed region/event, `type=general`, `unit=day`; no filters, expressions, properties or breakdowns. Exact one series and complete daily coverage. |
| Tableau / `scoped_tableau_saved_view` | Cloud REST 3.27 view/workbook GETs; view `/data` GET; repeat metadata GETs. Approved `date,value` CSV snapshot. | Cloud pod, site/view/workbook UUIDs, exact revisions and SHA256 of exact UTF-8 CSV bytes. Every approved day once, nonnegative numeric values; no BOM, filters or arbitrary URLs. Any changed snapshot requires renewed review. |
| PostHog / `scoped_posthog_saved_insight` | Cloud project GET; saved insight GET with `refresh=force_cache`; repeat insight/project GETs. Cached daily total or daily active-user counts. | Organization UUID, project/report numeric IDs, project timezone, canonical query SHA256. One EventsNode TrendsQuery, absolute grant dates, daily interval. No SQL, properties, breakdowns, formulas or project test-account filtering. Complete cached days and stable result/refresh timestamp required. |
| Stripe / `scoped_stripe_balance` | Pinned API 2024-06-20 account GET; balance GET; account GET with explicit Stripe-Account header. Available/pending amounts by currency in minor units, including negative balances. | Exact account ID and approved live/test mode. **Current balance snapshot**, not date-filtered revenue, MRR, subscriptions or customer records. Requested dates are authorization bounds only. |
| Search Console / `scoped_search_console_daily_report` | Sites GET; `searchAnalytics/query` POST; Sites GET. Finalized daily web clicks, impressions, CTR and position. | Exact `sc-domain:` or restricted URL-prefix property, encoded as a resource segment on fixed Google origin. Fixed date dimension, byProperty aggregation, final data; timezone America/Los_Angeles. No search-query/page text. |
| Google Ads / `scoped_google_ads_daily_report` | Three Google Ads REST v25 search POSTs: fixed customer identity query, fixed daily customer aggregate query, identity query. Impressions, clicks, cost_micros/currency. | Target and login-customer IDs explicit. Customer ID/resource name/timezone/currency verified before and after and on each row. GAQL is fixed application code with validated dates, never model-supplied SQL/GAQL. No mutations/pagination. |

Source tools remain `scoped_source_read` (effective `file`) and
`scoped_history_read` (effective `session_search`). They read only current
hash-bound source records or explicitly granted approved decision records.
Superseded approved decisions are labeled historical. Ordinary Slack transcripts,
global memory, arbitrary local files and deeper reports are not searched.
Owner/unprotected turns retain the existing executor/tool path.

Analytics date requests cover at most 31 completed days within an approved window
of at most 366 days. Tableau/PostHog may validate up to 366 approved cached days,
but return only the requested slice. GA4 daily activeUsers and PostHog daily active
users must not be summed as distinct period users. Missing returned dates are not
fabricated as zero. Stripe labels its date-independent snapshot explicitly.

## Transport and trust boundary

The existing httpx dependency and profile secret scope are reused. Inventory of
permitted integration **source files** found Google Ads clients that load config
and token files/default accounts, Stripe scripts loading default secrets with broad
reads, and a PostHog script loading env files/default project and host. They cannot
be safely imported across this boundary. No safely bound Meta, Mixpanel, Tableau or
Search Console source client was found. Their documented protocols are implemented
with the existing bounded HTTP transport; no new dependency was added.

`client_context_reads.py` owns a static operation mapping and frozen grants;
`client_context_marketing.py`, `client_context_bi.py` and
`client_context_google_stripe.py` own fixed vendor requests/normalization. GA4
retains its established executor. There is no MCP/global registry lookup, caller
URL/function/SQL, discovery, default-account fallback, write operation or AIAgent
fallback. POSTs are only the fixed Google reporting reads.

Each HTTP exchange uses TLS, `trust_env=False`, no redirects or retries, five-second
network timeouts and a 65,536-byte response limit. Compression is rejected. JSON
rejects duplicate keys and nonfinite constants; text is accepted only for the fixed
Tableau CSV path. Adapters validate identity, definitions, shape, dates and metrics;
only bounded normalized numeric observations, bound IDs, safe enums and timestamps
reach the provider. Free-form vendor metadata/errors, auth headers and secret slot
names do not. A final shared JSON size/type/finite-number check precedes continuation.

Authorization, source bytes/hashes, manifest/grant identity, tool policy and run
generation are checked before/after secret resolution, every HTTP exchange and
received chunk, provider rounds and final release. Any drift discards the turn and
retained scoped history. Cancellation prevents subsequent requests/continuation;
an already in-flight blocking request can remain until its network timeout. The
existing 30-second turn, four provider rounds, eight tool calls and aggregate prompt
budgets remain. No monitor, storage subsystem, cron or token-refresh job was added.

Account assurance trusts the explicitly reviewed operator binding and authenticated
provider routing. GA4, Search Console reports, Stripe balance and Mixpanel counts
do not independently sign or echo every account identity. The identity reads and
fixed resource routes are assurance, not cryptographic tenant proof. Mixpanel's
segmentation response also does not echo project timezone: the grant timezone is
an operator attestation. Its API is documented as maintenance mode. Saved Mixpanel
Insights are unsupported because the documented result lacks a retrievable report
definition/version to pin; the implemented fixed event-count operation avoids that
mutable-definition dependency.

## Manifest and executor credentials

The existing version-1 `analytics` array now accepts GA4 grants and these seven
additional grant types. No existing manifest or active source grant was edited.
All new grants have exactly these fields (synthetic example):

```json
{
  "id": "synthetic-meta-a",
  "scope_id": "workspace-synthetic",
  "chat_id": "channel-synthetic",
  "client": "client-a",
  "audience": "internal",
  "account_id": "101",
  "resource_id": "201",
  "credential_slot": "SYNTHETIC_A",
  "time_zone": "UTC",
  "start_date": "2026-01-01",
  "end_date": "2026-01-31",
  "expires_at": "2026-12-01T00:00:00Z",
  "operation": "scoped_meta_daily_insights",
  "status": "proposed",
  "approval_evidence": null,
  "sharing_evidence": null,
  "parameters": {"api_version": "v26.0", "currency": "USD"}
}
```

Every field is required; unknown fields/operations and duplicate grant IDs deny.
Route client/audience must match, expiry cannot exceed the route, proposed grants
must have null attestations, and approved shared grants require sharing evidence.
At most 64 analytics grants total. For new grants, a vendor account cannot map to
multiple clients, and a vendor credential slot cannot cross client/account ownership.
Different approved channels may bind the same client account. Report parameters are
stored as immutable serialized JSON; schemas never expose secrets or editable
parameters. Per-grant schema alternatives preserve all argument correlations.

| Platform | `account_id` / `resource_id` | Exact `parameters` fields | Secret namespace/suffix and prerequisite |
| --- | --- | --- | --- |
| Meta | ad account / owning business | `api_version: "v26.0"`, `currency` (three uppercase letters) | `META` / `ACCESS_TOKEN`, ads_read-capable token and readable owning-business metadata |
| Mixpanel | project / workspace | `region: "us"|"eu"|"in"`, `event` (bounded literal) | `MIXPANEL` / `USERNAME`, `PASSWORD`, service account authorized for explicit project/workspace |
| Tableau | site UUID / view UUID | `pod`, `workbook_id`, `view_updated_at`, `workbook_updated_at`, `report_sha256` | `TABLEAU` / `ACCESS_TOKEN`, existing site-scoped X-Tableau-Auth token with metadata and view-data access |
| PostHog | organization UUID / project numeric ID | `region: "us"|"eu"`, `report_id`, `definition_sha256` | `POSTHOG` / `ACCESS_TOKEN`, project and insight read scopes plus an already available approved cached report |
| Stripe | account ID / same account ID | `livemode` (boolean) | `STRIPE` / `ACCESS_TOKEN`, restricted/read credential permitting account and balance for explicit Stripe-Account context |
| Search Console | exact site property / same property | `{}` | `SEARCH_CONSOLE` / `ACCESS_TOKEN`, current webmaster read-only OAuth and readable property permission |
| Google Ads | target customer / login customer (both ten digits) | `{}` | `GOOGLE_ADS` / `ACCESS_TOKEN`, `DEVELOPER_TOKEN`; current Ads OAuth/developer access and approved customer relationship |

Secret names are `CLIENT_CONTEXT_{NAMESPACE}_{credential_slot}_{SUFFIX}`. They are
resolved only inside execution. An installed profile scope is authoritative; a
scope miss cannot borrow global environment values. No `.env` is opened. GA4 retains
`CLIENT_CONTEXT_GA4_{slot}_ACCESS_TOKEN` and its existing property grant format.
Token issuance, sign-in and renewal remain separately operated prerequisites.

PostHog definition hash is SHA256 of
`json.dumps(query, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()`.
The entire original approved query is hashed. Supported optional wrappers/fields
are validated by `_posthog_query`; a matching hash cannot authorize unsupported
query semantics. Tableau hash is SHA256 of exact UTF-8 CSV response bytes, with
header `date,value` and complete approved day coverage. Neither pin is supplied by
the model; neither changes automatically during source refresh.

## Offline operator capability check

Use the existing Python environment against a **separately staged approved source
copy and manifest**. These commands read local manifest/granted source bytes, never
credentials or customer APIs:

```bash
/Users/adityasheth/.hermes/hermes-agent/venv/bin/python -m gateway.client_context \
  capabilities --manifest /absolute/staged/manifest.json
```

The JSON receipt lists every implemented analytics capability, each route's approved
and proposed grant IDs/missing-grant blocker, explicit current source IDs, approved
history with lifecycle, and exact owner-private tuples with expiry status. It checks
route/analytics/source/history expiry and source hashes before emitting a receipt.
No grant for a named platform is reported ready just because the code exists.
`credentials_checked` and `live_effective_policy_checked` are false; `network_calls`
is zero and `activation` is `not_performed`.

Optionally supply the **reviewed effective policy**, after intersecting platform,
channel and disabled-toolset settings, to check the policy/grant intersection:

```bash
/Users/adityasheth/.hermes/hermes-agent/venv/bin/python -m gateway.client_context \
  capabilities --manifest /absolute/staged/manifest.json \
  --effective-toolset file --effective-toolset session_search --effective-toolset web \
  --allow-tool scoped_source_read --allow-tool scoped_history_read \
  --allow-tool scoped_ga4_daily_report --allow-tool scoped_meta_daily_insights \
  --allow-tool scoped_mixpanel_daily_event_count --allow-tool scoped_tableau_saved_view \
  --allow-tool scoped_posthog_saved_insight --allow-tool scoped_stripe_balance \
  --allow-tool scoped_search_console_daily_report --allow-tool scoped_google_ads_daily_report
```

Supplied policy inputs are operator assertions, not a read of live configuration.
One supplied policy set is applied to every reported route. When channel policies
differ, run separately for each reviewed effective policy and inspect only the
corresponding route; this command does not resolve per-channel configuration.
Without both policy inputs, `policy_permits` is null. Grant readiness alone is not
credential, report-availability or deployment readiness. Existing `validate` and
`client_context_refresh propose/review` commands remain available; reviewed source
refresh preserves analytics grants without approving or renewing them.

## Source coverage and exact activation checklist

No real Codewords/CookUnity source inventory or remote capability availability was
inspected. The local test corpus uses synthetic stable client IDs. Parent must:

1. Review this diff and receipt, stage/commit from an authorized session, and merge
   without overwriting unrelated live-checkout changes. No deployment has occurred.
2. Inventory approved source coverage separately for each client's orientation,
   current research, decision history, scorecards and allowed deeper reports. Assign
   stable client IDs; record exact source paths/hashes, safe citations, observation/
   effective/expiry dates, decision keys/supersession, approval and sharing evidence.
   Missing research remains a coverage gap; provider inference cannot fill it.
3. Bind every authorized Slack workspace/channel to exact client/audience/source IDs.
   For cross-channel retrieval, explicitly grant the same approved records to each
   intended route. Include current successors wherever superseded records are granted;
   otherwise that route denies rather than reviving old approval. Owner bypass remains
   an explicit workspace/channel/user/DM tuple. Ordinary transcript search is absent.
4. For every desired platform, confirm the exact immutable account/project/site/report
   IDs and semantics in the tables above. Review timezone, currency/mode, date window,
   expiry, report query/content/revision pins and remote read permissions. Missing
   cached PostHog results, nonconforming Tableau CSV, absent business ownership or
   unavailable account metadata are explicit retrieval blockers, not fallback triggers.
5. Stage proposals separately, have the operator record approval/sharing evidence,
   and arrange scoped current credentials externally. Source refresh cannot authorize
   analytics changes. Run `validate` and `capabilities` against the staged material;
   review the exact effective read policy and source coverage before activation.
6. Obtain separate live activation authorization. Parent controls configuration,
   source grants, installation and service lifecycle. No part of this task authorizes
   those actions. No credential scopes or current tokens were verified here.
7. After authorized activation, verify a genuine authenticated human Slack inbound
   request for each intended channel/client, cross-channel source/decision retrieval,
   narrow analytics results, source/grant/tool revocation, and preserved owner tools.
   Compare approved remote identity/metadata with operator ground truth. Synthetic
   SDK tests do not substitute for this live acceptance.
8. Roll back under operator control by removing the affected tool permission/grant
   or disabling scoped context. Policy changes invalidate retained history at the
   next boundary; restart also clears in-memory history. No cron is needed.

## Verification evidence

Parent-verified runs supersede the earlier, unreproducible 908-test receipt:
**496 client-context tests across 9 files and 510 adjacent Slack tests across
37 files; zero failures, no retries.** Test paths were enumerated from disk before
execution; the provider protocol and review regression files are included.
Reproduce from this checkout:

```bash
scripts/run_tests.sh tests/gateway/test_client_context*.py --file-retries 0 -j 4
scripts/run_tests.sh tests/gateway/test_slack*.py --file-retries 0 -j 4
```

Focused operation commands (same runner and isolation):

```bash
scripts/run_tests.sh tests/gateway/test_client_context_analytics.py --file-retries 0 -j 2
scripts/run_tests.sh tests/gateway/test_client_context_marketing.py --file-retries 0 -j 2
scripts/run_tests.sh tests/gateway/test_client_context_provider_protocol.py --file-retries 0 -j 2
scripts/run_tests.sh tests/gateway/test_client_context_review_fixes.py --file-retries 0 -j 2
scripts/run_tests.sh tests/gateway/test_client_context_bi.py --file-retries 0 -j 2
scripts/run_tests.sh tests/gateway/test_client_context_completion.py --file-retries 0 -j 2
```

The operation/lifecycle files provide 89 GA4, 39 Meta/Mixpanel, 51 Google/Stripe,
38 BI, 15 final review regressions and 21 whole-goal acceptance cases. Positive
tests exercise actual gateway imports,
OpenAI SDK request/continuation and httpx request/stream/JSON handling with synthetic
MockTransport servers. Tests inspect actual encoded resources, headers and bodies.
Adversarial cases cover foreign account/client/workspace/resource identity, secret
canaries, untrusted result text/numbers/dates/shape, redirects, arbitrary SQL/URLs/
writes, invalid grants, mutable report definitions, and grant/source/tool revocation.
The retained GA4/shared-loop tests also cover cancellation, credential scope misses,
small-chunk revocation, provider continuation/release, stale run generations, scoped
history invalidation and prompt budgets. The separately executed adjacent Slack
tests cover native controls, ingress authorization and normal tool preservation.

The whole-goal lifecycle test retrieves current and superseded approved decisions
in two explicitly granted channels/threads; denies a route missing the current
successor; proves a refresh proposal has no effect; creates and selects a separately
reviewed synthetic manifest; verifies old retained context is discarded and all
analytics grants survive exactly; denies the other client; and preserves owner and
unprotected admission. This changes only fixture configuration, never live config.

Additional final checks:

```bash
/Users/adityasheth/.hermes/hermes-agent/venv/bin/ruff check \
  gateway/client_context*.py tests/gateway/test_client_context*.py
/Users/adityasheth/.hermes/hermes-agent/venv/bin/python -m compileall -q \
  gateway/client_context*.py tests/gateway/test_client_context*.py
git diff --check
```

Ruff, compileall and diff checks pass. Static type-check attempt
`.../venv/bin/python -m ty --version` reports `No module named ty`; no dependencies
were installed and no type-check pass is claimed. The full repository suite and
remote CI were not run.

## Concise review record

Independent reviewers examined the shared grants/dispatch/transport, vendor
adapters, lifecycle test and offline checker against provider docs. Repaired:
PostHog fractional RFC3339 timestamps and mutable project filters; documented
organization-scoped project identity route; explicit Meta incomplete-day coverage;
Tableau decimal roundtrip precision checks; exact Search Console property binding
with valid dotted/percent-encoded paths and IDNA names. The last code review found
no additional actionable connector defects, but found a missing claimed test file.
Parent restored the real gateway protocol suite under
`test_client_context_provider_protocol.py`, added the review regressions, corrected
the test harness to use `_handle_message`, and reran the exact verified file set.
Do not reuse the earlier missing-file counts as acceptance evidence.

Protocol references used for implementation/review:

- [Meta official SDK account operations](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/adaccount.py)
  and [API version](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/apiconfig.py).
- [Mixpanel segmentation query](https://docs.mixpanel.com/reference/segmentation-query),
  [service accounts](https://docs.mixpanel.com/reference/service-accounts).
- [Tableau workbooks/views REST](https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_ref_workbooks_and_views.htm).
- [PostHog insight API](https://posthog.com/docs/api/insights),
  [project implementation](https://github.com/PostHog/posthog/blob/master/posthog/api/project.py),
  [official trends result examples](https://github.com/PostHog/posthog/blob/master/products/canvas/skills/querying-canvas-data/SKILL.md).
- [Stripe balance](https://docs.stripe.com/api/balance/balance_retrieve),
  [connected-account authentication](https://docs.stripe.com/connect/authentication),
  [account retrieval](https://docs.stripe.com/api/accounts/retrieve).
- [Search Console query](https://developers.google.com/webmaster-tools/v1/searchanalytics/query),
  [site identity](https://developers.google.com/webmaster-tools/v1/sites/get).
- [Google Ads search](https://developers.google.com/google-ads/api/rest/common/search),
  [REST authentication](https://developers.google.com/google-ads/api/rest/auth).
- [GA4 reporting](https://developers.google.com/analytics/devguides/reporting/data/v1/rest/v1beta/properties/runReport),
  [Admin property identity](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties).
