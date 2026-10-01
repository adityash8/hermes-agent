# Codewords text-only isolation research

Approved plan: /Users/adityasheth/.hermes/plans/2026-09-19_103928-codewords-channel-isolation-repair.md.
User amendment takes precedence: reuse client_context, no deployment or external writes.
Baseline: /private/tmp/codewords-isolation-baseline-evs79ebq (54 dirty entries).

- Existing gateway/client_context.py intercepts ingress before hooks, slash dispatch and session reads, and turns before agent construction. complete() uses concrete OpenAI SDK with explicit messages and no auxiliary task overrides; AIAgent injection, memory, context engines and caches are bypassed.
- Existing client_context enabled mode globally gates Slack and denies routes absent from its manifest. New bounded channel selector must run independently while enabled:false keeps all other routes unchanged.
- Existing stateless evidence answer reads manifests and sources; reuse only complete() and isolated ingress/turn lifecycle, never snapshot()/answer() or scoped tool generation.
- Stateless answer lacks pre-provider revalidation. New branch must seal policy, source and current event; validate before provider transmission and release, with cancellation/deadline gate.
- Native admission occurs before file/history enrichment. Workspace maps come from auth.test. Fresh human metadata verification must fail closed. Admin allowlists must neither bypass isolation nor be globally extended.
- Existing dirty Slack participation layer enforces request ownership but calls ordinary authorization. Target grant must be confined to authenticated native isolated source and revalidated at egress.
- Base adapter slash and busy machinery can cancel or concatenate before runner ingress. Target must fail closed before these paths. Background and scheduled sends require target egress denial.
- Do not mutate global scoped history or private sessions. Keep all old records; the text-only route never reads or writes them.

Architecture reasoning:
Problem: share current-text assistance without private operator authority.
What breaks: global enabled mode would deny unrelated Slack; normal agents inject private context.
Simpler: reuse isolated provider and transport lifecycle, no new restricted_users wiring.
Constraint: native authenticated text only, zero tools/history/files, no live changes.
Classification: Feature with security boundary tests and independent review.

Final findings: native-marked receipts remain isolated after policy removal, including reconstructed
background/hook calls. Slack SDK fence permits only a validated plain-text final from its original task;
legacy callbacks, scheduled deliveries, media, reactions, streaming and deferred tools cannot use this lane.
Real SDK tests use fake HTTP transports; no live Slack authentication or activation was attempted.


# Approved Codewords bounded read replacement (2026-09-20)

Authorization: implementation-brief.md in Downloads/Jarvis/codewords-read-access. No activation, external writes, config, reloads, commits. Existing findings above describe the previous route.

- Reuse cc.complete raw HTTP transport, native users.info human admission, sealed event identity, and task-bound SDK final delivery. No ordinary agent, registry, source packets or session history.
- Existing scope accepted arbitrary configured workspace; replacement pins verified T07MMEX5658 and C09PF4QGQMC. Slack Connect human home team can differ.
- Replace text_only module/config/receipt vocabulary with Codewords reads; keep retired config/receipt recognition only for fail-closed rejection. No parallel route or compatibility executor.
- Existing scoped adapters provide bounded HTTP and typed aggregate normalization. New backend accepts only dates or fixed inventory operations; bindings and credential locations are server-owned. Integration evidence is pending separate probe worker.
- Worker cancellation must revoke the retained synchronous request callback: cancelling asyncio.to_thread alone cannot stop a stale thread. Every read and provider hop plus final SDK release rechecks deadline, event, resource snapshot and run generation.

Architecture reasoning:
Problem: provide seven useful Codewords reads to verified teammates without personal context or writes.
What breaks: extending the ordinary agent/global registry would expose unrelated capabilities and context.
Simpler: one stateless loop on the existing isolated transport with seven fixed handlers.
Constraint: pending IDs and integration evidence cannot be invented; failed probes remain blocked.
Classification: bounded Feature with security and real-import transport tests.

Final evidence: only PostHog project metadata passed the external live probe. Aggregate reporting remains unverified. GSC/GA4/Google Ads/Meta Ads lack verified resource/credential bindings; GTM probes failed and token expired; PostgreSQL Keychain/TLS/role proof failed and fixed connection/schema/CA bindings plus psycopg are absent. Unknown bindings stay unavailable. Native users.info verifies Slack Connect humans independently of their home workspace and admin flag. Independent review found and fixed stale new source-only receipts entering base-adapter commands after route removal. PostgreSQL additionally rejects ambient PG* settings and ambient client-certificate/GSS/password-file authentication. No activation or live writes performed.

# Parent corrections and five verified sources (2026-09-20)

The user explicitly authorized implementation of parent-corrections.md and integration-probes.json v2. This supersedes the obsolete one-source finding above: PostHog aggregate parent proof plus GSC, GA4, GTM, and Meta bounded reads are verified. Google Ads customer binding and database credentials remain unverified. The current code still leaves GSC/GA4/Meta blocked, rejects expired GTM access tokens, and uses the global 30-second timeout for the entire route.

Existing transport has 5-second HTTP requests, redirect rejection, a 64 KiB response cap, duplicate JSON rejection, and revalidation around I/O. Reuse existing aggregate parsers. Credential and connected-account handles must stay below fixed server-owned operations. OAuth refresh needs a bounded form transport; reusing JSON encoding would be incorrect. No file persistence of refreshed tokens. GTM live responses need a fields mask to avoid the demonstrated size failure and exclude tag content at the network boundary.

Reasoning: restore the already-authorized fixed reads; shared identities make generic dispatch unsafe; extend the existing narrow dispatcher and parsers; preserve revocation and secret isolation with no activation; classify as a security-sensitive bug fix within the approved Feature.

Baseline copied to /private/tmp/codewords-corrections-2bqc7fl5; current whole-tree file hashes retained there. No tasks/lessons.md existed at start. Parent owns source/config reconciliation and rollout; no config/reload is authorized in this task.

Latest evidence and connection implementation:
- Parent updated parent-corrections.md during work: Google Ads customer 9855813343 is verified by identity + daily aggregate, v24, GBP, America/Los_Angeles. The task's 'until verified' gate is now satisfied; no login-customer-id header is permitted. Database still fails Keychain lookup (exit 36).
- GA4 uses the existing pinned Google marketing OAuth file directly; do not import plugin _credentials(), which writes refreshed tokens to disk.
- GSC uses only fixed COMPOSIO_MULTI_EXECUTE_TOOL -> GOOGLE_SEARCH_CONSOLE_SEARCH_ANALYTICS_QUERY, alias ez money, property sc-domain:codewords.ai. The fixed Composio endpoint is initialized without discovery. Its runtime key is exactly ${COMPOSIO_CONSUMER_API_KEY}, resolved from the pinned existing .env without interpolation or environment fallback. Source fingerprints are rechecked around requests. Live connector read returned one row.
- Meta's verified account/business/currency/timezone are now recorded in the immutable binding, but remain unavailable. Existing local Pipeboard tokens authenticate the MCP service and are not Graph tokens. Its get_insights schema has no version/fields controls. No existing direct Graph credential or supported token retrieval method was located. A clarification asking the parent for probe credential provenance is pending; no substitute credential or generic MCP surface was added.
- Live dispatcher reads: GSC, GA4, Google Ads each one daily row for 2026-09-17; GTM two container inventories; PostHog one row on retry after initial unavailable. No arbitrary data or credential bodies saved.

# Approved Meta Pipeboard repair (2026-09-20)

meta-repair-brief.md supersedes the old direct-Graph prerequisite. The fixed dispatcher already validates dates, snapshots bindings, and fences cancellation through the route's 120-second guard. GSC demonstrates a bounded MCP transport; Meta needs its own credential resolver, notification acknowledgement handling and nested daily metrics parser. Reuse the HTTP client, strict JSON helpers and dispatcher boundary, not generic MCP discovery or the old Graph adapter. No Slack admission, profile config or live activation changes.

Reasoning: restore the verified Meta service read; generic MCP dispatch would widen authority; use one fixed endpoint and two fixed read envelopes; secrets and raw vendor content remain internal; this is a bounded Feature repair. Parent owns live protocol/report proof and activation.

Meta repair findings: installed MCP accepts bounded notification acknowledgements without interpreting a JSON-RPC response. Persistent SSE must close after its matching completed event, and unterminated events must fail closed. Hosted segmented wrapper follows the brief; nonempty synthetic fixtures use account-level Graph rows from public vendor defaults. Unused vendor aggregates/names/action metrics never enter output. No parent live nonempty projection arrived; do not describe synthetic tests as live proof. Real route cancellation test confirms the worker cannot issue the post-report identity request or a further provider round after cancellation.

# Approved Stripe restoration (2026-09-21)

Parent supplied live evidence: the dedicated CODEWORDS_STRIPE_RESTRICTED_KEY from
codewords-stripe.env returned 403 more_permissions_required on GET /v1/account,
explicitly identifying acct_1Oyuv1Ka5VSTfRCx. The same key with Stripe-Version
2024-06-20 and that fixed Stripe-Account header returned a live subscription list.
These are parent-reported probes; this task performs no live executor verification.

The existing reader has an unavailable binding and an account identity sandwich.
Replace that sandwich with server-owned account routing on every subscription page.
The dispatcher rejects extra caller fields before I/O, snapshots bindings, and guards
before/after each HTTP request and output release. The shared transport accepts only
HTTP 200, rejects redirects, and caps bodies at 64 KiB; subscription rows require
livemode=true. Keep page size 10 and max pages 20 without claiming incomplete totals.
Possible live oversize is unmeasured here: retain the cap pending parent evidence.

Reasoning: restore the proven restricted-key read; requiring account-read blocks it;
fixed request routing is simpler than discovery; preserve bounded, revocable reads;
this is a Feature repair within the explicitly authorized continuation.

# Approved verified Linear / Customer.io reads (2026-09-21)

The parent provided /tmp/codewords-verified-identities.md and explicitly approved
implementation. Linear organization e6ec22ea-cadc-4eab-8376-4caeacea20a3 (Agemo,
agemo) and nine team UUID/key pairs are now verified. Customer.io EU credential
owns exactly 214585 CodeWords Staging and 225222 CodeWords Production. Default
workspace attribution is NOT verified; no workspace selector is authorized.

Existing dedicated module only returns unavailable. Shared executor already fences
bindings, request/response chunks, credentials and final delivery. Its credential
snapshot does not cover json_file yet; add it for Customer.io using the same strict
JSON loader, no ambient fallback. Transport rejects redirects and >64 KiB bodies;
output cap is 8 KiB. Keep small projections and explicit incomplete receipts.

Reasoning: restore verified reads; caller-selected endpoints would break isolation;
extend the existing dispatcher and bounded transport; immutable identity pins and
read-only templates constrain all requests; this is an approved Feature repair.
Primary API contracts: https://linear.app/developers/graphql,
https://linear.app/developers/pagination and
https://docs.customer.io/files/journeys-app.json. Independent contract research runs
in parallel. Never request descriptions/comments, users, actions or message content.

Final contract verification used the official Linear SDK GraphQL schema and Customer.io
listcampaigns/listsegments/listworkspaces operation pages (large OpenAPI fetch timed
out). Linear state type includes duplicate. Customer.io list envelopes are exact and
unpaginated; reject any added envelope field rather than infer continuation. Unknown
optional segment type is omitted. Campaign/segment names are intentionally omitted.
Linear titles retain bounded useful text with common contact/credential redaction;
arbitrary names or novel secrets embedded in prose cannot be reliably classified.

Verification: full relevant official suite 72 files / 2,277 passed; final dedicated
plus credential tests 153 passed after eight additional dedicated cases. Thus 2,285
unique final passing tests across that scope. Ruff/ty clean on all four functional
files. Independent security review APPROVE, no blocker; parent owns every live probe.
Temporary test-only import links exposed installed Slack SDK/Bolt to the runner,
without changing either virtualenv. Source edits preserve all unrelated baseline
hashes. No configuration, credential, restart, send, commit or PR action performed.


# Approved Codewords continuity repair (2026-09-21)

Authority: /Users/adityasheth/Downloads/Jarvis/codewords-continuity-execution-brief.md.
This supersedes the old statelessness choice only for bounded same-channel/thread data.
Private baseline: /Users/adityasheth/Downloads/Jarvis/codewords-continuity-backup-20260921T135628Z
(89 pre-existing changed/untracked files plus dirty/staged diffs and SHA256 manifest).
Baseline focused official runner: 153 passed across loop, Stripe and isolation suites.

- The isolated completion loop uses raw provider transport and fixed dispatcher; keep it.
  It never creates an AIAgent, imports private sessions, or uses general tool dispatch.
- Native Slack admission binds T07MMEX5658/C09PF4QGQMC with users.info. Existing
  history helpers cache data and can fall back to a primary client; do not reuse them.
  A new topical reader will use only the exact authenticated workspace client,
  fresh replies for the immutable source thread and history for the constant channel.
- Slack docs confirm cursor pagination, ascending replies and descending channel
  history. Use bounded pages and explicit incomplete/filter/truncation coverage;
  no Slack search API or user-controlled channel/thread/resource identities.
- Codewords admission precedes ordinary deletion handling and rejects all subtypes.
  Restore authenticated deletion handling and invalidate in-flight context on edits
  or deletions, without treating historical authors as request authorization.
- Stripe already scans a fixed account with credential and deadline fences, but
  drops cancellation_details.reason. One scan can aggregate two validated windows,
  explicit unknown endings and computed deltas; incomplete scans withhold totals.
- Existing shipping convention is EZ MONEY/config/hermes/patches/; predecessor is
  ez-1048-codewords-channel-isolation. The local scripts/linear_api.py supports
  scoped issue creation with a proof receipt and readback. Verify workspace first.

Architecture reasoning:
Problem: follow-up questions lose their windows and cannot find channel evidence.
What breaks: ordinary sessions or fallback Slack clients could cross isolation.
Simpler: fresh bounded Slack reads plus the existing single Stripe aggregate scan.
Constraint: immutable identities, revocation, no private memory, sends or activation.
Classification: Feature repair with synthetic real-import integration proof.

Primary contracts: website/docs/developer-guide/gateway-internals.md;
https://docs.slack.dev/reference/methods/conversations.replies/;
https://docs.slack.dev/reference/methods/conversations.history/;
https://docs.slack.dev/reference/events/message/message_changed/;
https://docs.stripe.com/api/subscriptions/object?api-version=2024-06-20.


Continuity final findings and proof:
- Real SDK testing exposed a participation fence that mocked API methods hid.
  The exact read capability now binds adapter/client/method/payload/task/grant and
  is entered inside wait_for's SDK task. Modified targets and inherited children
  fail at the same fence; no unfenced SDK/regular-agent fallback was introduced.
- Lifecycle repair preserves actorless edit/delete invalidation, persistent bot
  deletion receipts, replay dedupe and fresh source-authorized direct summons.
- History reports incomplete pagination, filtered text and output truncation.
  Channel reads cover the timeline, not every thread reply. Attachments/files and
  foreign bots are excluded; author posts with unfurls may therefore be omitted.
- Final official regression: 74 files, 2,417 passed, zero failed. Lint passes;
  new/scoped gateway types pass and shared plugin diagnostics are baseline-only.
- Independent review APPROVE; red evidence, exact commands, scoped diffs, final
  source hashes, baseline preservation and activation/rollback are retained in
  the private backup and the EZ MONEY continuity patch artifact.
- No production activation or native live proof was attempted. Existing local
  Linear integration could not resolve api.linear.app; no new issue was created.


# Approved Codewords UX parity addendum (2026-09-21)

Authority: /Users/adityasheth/Downloads/Jarvis/codewords-ux-parity-addendum.md; user explicitly approves additive implementation. Phase-1 artifact remains immutable. Phase-2 baseline: /Users/adityasheth/Downloads/Jarvis/codewords-ux-parity-backup-20260921T142841Z (92 pre-existing dirty files). Parent reports tracking EZ-1059/EZ-1060/EZ-1061/EZ-1062 resolved and live Slack/Stripe reads verified; no duplicate tracking/network probes here.

Verified paths: dispatch_scoped omits reaction bookkeeping and disallows thread lookup. Existing active-task requester ownership expires when _process_message_background completes, so lookup alone cannot authorize post-answer continuation. A bounded Codewords-only receipt can admit the same native requester after a real summon; output remains a fresh task-local grant. _revoke_requester_for_no_reply needs an optional sealed source, as phase-1 deletion reopening does. Stop/no-reply must be handled before the isolated route drops commands; historical messages remain evidence only.

BaseAdapter already invokes on_processing_start/complete inside native participation. Existing Codewords SDK fence permits only the sealed final post and fixed reads, so eyes need an exact lifecycle capability bound to adapter/client/token/bot/request/task. Revoke cleanup must only remove an eyes reaction actually owned by this task and cannot add terminal output. Failure/timeout in the isolated loop currently returns None, which the generic lifecycle counts as success; a scoped failure receipt must correct only this route.

Presentation: send_codewords currently sets mrkdwn=False. Slack official chat.postMessage and formatting docs confirm mrkdwn=True with parse=none disables automatic URL linking; escaping &, < and > prevents explicit mention/link syntax. Bind all final payload fields at the SDK fence, not only text. Add a deterministic table-to-bullets presentation pass while keeping safe native bold and bounded safe-output validation. Official references: https://docs.slack.dev/reference/methods/chat.postMessage/ and https://docs.slack.dev/messaging/formatting-message-text/.

Visual blocker verified: ScopedReply bypasses media extraction; Codewords files.* denied. _upload_file fence receives no bytes/URL binding, and deletion cleanup only tracks/retracts created chat messages, not published file IDs. Existing browser ArtifactStore deliberately lacks Slack requester/thread/deletion authority. Safe deterministic chart delivery needs an aggregate/bytes receipt, a bound three-stage upload and owned-file cleanup; this is broader architecture. Deliver A/B/C and explicit visual limitation as authorized in D. No live source JSON or client traffic needed.


Phase-2 final findings: stale stop replays cannot retire newer invitations; unedged
two-column tables convert to native bullets; current channel controls share one
Codewords-only authority predicate across native participation and idle invitation
lookup; a rejected queued worker clears only its own guard before the base lifecycle
begins. Three-second exact reaction operations preserve acknowledged-eyes cleanup
across cancellation without allowing posts, alternate targets/emojis or child-task
capability reuse. The static prompt guides lowercase answer-first prose; the native
formatter/fence enforces table conversion and safe mrkdwn fields. All 2,485 official
regression tests and 165 independent focused tests passed; review hashes match final code.


## Phase 3 chart delivery research (2026-09-21)

The approved chart-delivery brief expands visual output only for the admitted Codewords route. Phase 1 and 2 artifact trees and all 96 current dirty files are snapshotted at Downloads/Jarvis/codewords-chart-delivery-backup-20260921T150009Z. Earlier source/test packages are immutable inputs.

The isolated loop receives actual ReadReceipt objects from the fixed Stripe executor, but currently retains only credential validators. Retain a separately validated immutable comparison only for an explicit current human chart request; derive all labels/counters/comment/PNG server-side. ScopedReply already survives BasePlatformAdapter._extract_response_content unchanged, so no generic media extraction or model tools need alteration.

Native Slack files_upload_v2 has separate allocation, bytes and completion requests. Installed _upload_file accepts url/data/transport arguments but does not forbid redirects; its present fence also lacks exact bytes/URL. Keep that generic helper denied on Codewords. Use a fixed no-redirect/no-proxy HTTPX byte request behind an exact one-task capability in the same adapter fence. Allocation/completion/owned deletion continue through the real fenced SDK. Slack documentation confirms raw byte POST then completeUploadExternal and permits only one completion; allocation gives the file ID before publication. The existing deletion helper owns chat messages, not files, so this operation must retain its own allocated ID and settle in-flight completion before exact owned-file cleanup. Cleanup is bound to unchanged authenticated client/token/bot even after route/task revocation.

Sources: https://docs.slack.dev/reference/methods/files.getUploadURLExternal/ ; https://docs.slack.dev/reference/methods/files.completeUploadExternal/ ; https://docs.slack.dev/reference/methods/files.delete/ . Reference renderer style is white/black with gray/gold paired bars; its old input is never used. Pillow 12.3.0 is already installed in the test environment.


Phase-3 review refinement: preserve the successful file receipt through processing completion, including the await for eyes removal. Independent staff review reproduced a retained successful follow-up invitation after cancellation during this await. Delay invitation finalization until chart/indicator settlement and mark failed/cancelled completion before clearing its active event. A second review probes terminal checkmark acceptance racing revoke; retain and retract only the confirmed owned terminal indicator using the same unchanged native identity. Explicit chart opt-in must also reject code/quoted/explanatory/text-only and unrelated-integration asks; common direct chart request phrases remain supported.


Phase-3 final source verification: 79 canonical files / 2,694 tests passed / zero failures and retries; all nine source/test hashes stable across that run. Ruff passes all nine; seven scoped source/test files pass ty, and two shared files retain eight baseline diagnostics with zero new issues. Security/lifecycle reviewer APPROVE after 277 passing tests; independent staff reviewer APPROVE after 281 passing tests including four separately reproduced native probes. Three high-severity findings (premature follow-up success, accidental visual intent, late terminal marker) were fixed and re-reviewed with no open in-scope findings. Maximum-change PNG visually checked for labels/caveats without clipping. No live Slack traffic or activation occurred.


## Parent phrase review (2026-09-21)

The parent rechecked current source after the phase-3 handoff: requested("design a visual of the Stripe comparison") is False because the affirmative action pattern omits design. The separate late-negation action list must also include design so a later "do not design it" veto remains effective. Existing quote/code filtering, target validation, aggregate provenance and SDK fences already cover this path and need no change. The current bounded grammar rejects "why don't you create a visual of the Stripe comparison?"; this remains an explicitly tested unsupported form, with direct "design a visual ..." supported. The optional zero-baseline label is outside this narrow required repair; its existing undefined-percent behavior stays unchanged.

Parent phrase verification: the two exact design forms first failed in both detector and native delivery tests (four failures). Adding design to the two verb alternatives makes all 141 focused tests pass. The full official runner passes 79 files / 2,709 tests / zero failures with retries disabled; all nine phase-3 source/test hashes are stable. Ruff passes all nine and ty passes the seven scoped files. The independent narrow reviewer APPROVES after 224 passing detector/delivery/security tests, confirms the other six authority/upload/security files are byte-identical and reports no in-scope findings. This supersedes the pre-phrase test totals above; earlier broad reviews remain historical evidence. No source/test change followed the final runs or review.
