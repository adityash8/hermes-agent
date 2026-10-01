# Codewords channel text-only repair

Approved implementation; activation explicitly excluded.

- [x] Read approved plan, rules and relevant source; preserve all 54 baseline entries.
- [x] Research existing isolation route and independent security review tracks.
- [x] Prove target isolation and unrelated-route preservation fail on baseline.
- [x] Add bounded opt-in policy and current-request-only provider turn with revalidation.
- [x] Gate native authenticated humans, admin parity, attachments, commands, busy/background and egress.
- [x] Run red/green and focused client_context/auth/Slack regression tests, lint/type checks.
- [x] Review incremental diff and baseline preservation; deliver exact config and caveats.

Files: gateway/client_context.py, gateway/client_context_text_only.py, gateway/run_adapters.py,
gateway/platforms/base.py, plugins/platforms/slack/client_context.py, plugins/platforms/slack/adapter.py,
plugins/platforms/slack/participation.py, tests/gateway/test_slack_channel_isolation.py, tests/gateway/test_slack_text_only_delivery.py.
Research and this task record document the approved implementation.
No config, identity, permissions, deployment, Slack sends, Linear, commits or PR changes.
Stop if isolation cannot be enforced without ordinary-agent context or global authorization changes.

Verification: 1,639 passed, 0 failed, 1 skipped across 63 files. New isolation/delivery: 68 passed.
Independent security review: APPROVE, no unresolved critical/important findings.
Ruff and new-module ty passed; 11 existing client_context.py type errors reproduced on baseline.
Evidence and handoff: /private/tmp/codewords-isolation-baseline-evs79ebq/HANDOFF.md.
Activation and native live auth.test binding remain parent-owned and unverified.


# Approved Codewords read-only replacement

Approval: user explicitly requested finish implementation and tests from implementation-brief.md; no further implementation approval is needed.

- [x] Read brief, verified Slack binding, source/rules; preserve local baseline before edits.
- [x] Research transport, native admission, old receipt handling, and reusable scoped adapters.
- [x] Replace route and mechanical consumers; pin workspace/channel; reject retired config and receipts.
- [x] Implement bounded seven-tool stateless loop with resource/policy/run/deadline revalidation.
- [x] Add server-owned integration reads, sanitized outputs and blocked states from verified evidence.
- [x] Preserve and extend ingress/delivery isolation, provider loop, injection, cancellation and SDK tests.
- [x] Run official test runner, focused lint/type checks, adjacent regressions and independent review.
- [x] Save changed-file inventory, exact commands/results, proposed config, rollback and blockers in handoff.

Files: replace gateway/client_context_text_only.py with client_context_codewords.py; new bounded reads backend and optional postgres sibling; mechanically update gateway/client_context.py, run_adapters.py, platforms/base.py, plugins/platforms/slack/{adapter,client_context,participation}.py and existing two isolation/delivery test modules; new loop/backend behavioral tests. This exceeds ten paths only because one existing route crosses native ingress and SDK egress and all consumers must migrate atomically.

KFI: any non-Codewords resource/context request or output; guard with independent dispatcher validation and immutable policy receipts. Stop/re-plan if authenticated identity, fixed binding, read-only DB role, or stale worker fencing cannot be proven. Live integrations that fail remain unavailable and block activation. No changes to existing global registry or unrelated authorization. Rollback uses preserved files and removes only newly created implementation files.

Final local verification: 1,607 unique tests passed across 61 files (60 green files in final regression plus corrected loop fixture rerun: 19/19). Ruff passed; all three new modules ty clean. Existing shared-file type diagnostics reproduce on baseline. Independent review APPROVE after base-adapter stale-source regression fix. Live activation blocked: see Downloads/Jarvis/codewords-read-access/HANDOFF.md.

# Approved parent corrections

- [x] Read correction/probe evidence and current adapters/tests; preserve incremental baseline.
- [ ] Resolve existing connection references and wire fixed GSC/GA4/Meta reads; retain Ads/DB denial.
- [ ] Add bounded in-memory OAuth refresh with safe error codes and request/revocation tests.
- [ ] Limit GTM responses using a fixed live-version fields mask.
- [ ] Set route-only 120-second total deadline; prove timeout/cancellation behavior.
- [ ] Run focused and adjacent official tests, lint/type checks, independent review.
- [ ] Save incremental patch, results, source/config blocker and parent handoff.

Files: Codewords reads/route modules, new Codewords OAuth or connection sibling if needed, corresponding backend/OAuth/route tests, research and task records. No global tool surface changes. Verification: real-import fake HTTP tests exercise fixed URLs, form encoding, projection, identity mismatch, error sanitization and revocation; official runner plus targeted lint/types. KFI: any caller-selected resource or secret escaping the boundary. Stop if no existing connection can be safely pinned; report the specific prerequisite without fallback or fabrication. Parent handles final live smoke and activation; save patch without modifying live configuration or restoring others' work.

Correction completion status:
- [x] GSC and GA4 fixed connections implemented and live verified.
- [x] Updated parent instruction: verified Google Ads binding implemented and live verified; inherited manager explicitly excluded.
- [x] OAuth refresh, compact GTM and route deadline implemented and tested.
- [x] 1,713 tests passed across 64 files; incremental final reads retest 67 passed; production modules lint/type clean.
- [x] Independent read-only review found no blocker in implemented OAuth/GTM/GSC/Ads/timeout paths.
- [ ] Meta runtime connection: resource identities pinned, strict direct Graph transport credential provenance still required from parent. Model receives unavailable; no unsafe fallback.
- [x] Save patch/preservation evidence and handoff, with no activation.

# Approved Meta Pipeboard repair

Approval: meta-repair-brief.md explicitly authorizes implementation and tests now.

- [x] Read brief, gateway rules, lessons and existing dispatcher/transport tests; snapshot touched files.
- [x] Implement fixed Pipeboard MCP handshake, credential fingerprint checks, identity sandwich and daily normalization.
- [x] Wire Meta only; remove obsolete Graph handler and update Meta assertions.
- [x] Exercise real dispatcher with fake HTTP for allowed reads, identity/shape failures, injection, budgets and revocation.
- [x] Run official Codewords/Slack tests, lint/type checks and independent review.
- [x] Save incremental patch, verification evidence and sanitized parent-only smoke script.

Files: gateway/client_context_codewords_meta.py, gateway/client_context_codewords_reads.py, tests/gateway/test_codewords_meta.py, tests/gateway/test_codewords_reads.py, research.md, tasks/todo.md; evidence and smoke under Downloads/Jarvis/codewords-read-access/meta-repair.
Verification: scripts/run_tests.sh with required pythonpath override, existing .venv Ruff/ty, baseline-relative review. No new telemetry; errors return existing sanitized unavailable status. KFI: any foreign-resource request, write, personal context or raw secret/payload output. Stop if a concrete protocol gap prevents bounded reads. Parent handles live smoke, activation and restart.

Meta repair verification: 1,794 tests passed across 65 files, followed by final Meta-only rerun with one added cancellation case: 82/82, for 1,795 distinct passing tests. Ruff/ty and diff whitespace checks pass. Independent review APPROVE. Parent-only nonempty hosted smoke/activation remain unperformed. Evidence: Downloads/Jarvis/codewords-read-access/meta-repair/HANDOFF.md.

# Approved Stripe restoration

Classification: Feature repair. User explicitly authorized implementation and review.
Problem: /v1/account needs permission the dedicated key lacks; parent proved fixed
account subscription routing works. Approach: replace the identity sandwich with the
fixed Stripe-Account header, using the existing guarded transport and credential.
Reject broader account-read permission and model-selected headers or identity.

- [x] Read source, lessons and parent evidence; snapshot incremental baseline.
- [x] Update real-executor tests and prove the new fixed-header contract fails on baseline.
- [x] Pin production account/live mode, remove account endpoint calls, preserve strict failures.
- [x] Run narrow official tests, lint/types, baseline-relative diff and independent review.
- [x] Verify unrelated file preservation and write /tmp/codewords-stripe-activation-result.md.

Functional files: gateway/client_context_codewords_reads.py and
tests/gateway/test_codewords_stripe.py. Records: research.md, tasks/todo.md,
tasks/lessons.md. No new telemetry; existing sanitized unavailable result detects failure.
KFI: any foreign account request or totals released from an incomplete/untrusted scan.
Mitigation: fixed header, strict argument and response validation, per-request guards.
Stop if implementation requires broader authority or a cap increase without evidence.
No config changes, restart, posts, commits or live activation. Parent owns live proof.

Verification: 139 distinct tests passed (Stripe 51, shared reads 67, tool loop 21).
Ruff and ty passed for both functional files; baseline-relative whitespace review
passed. Independent reviewer APPROVE, independently reran Stripe 51/51. Only the
two functional files and these three task records differ from the initial baseline.
Handoff: /tmp/codewords-stripe-activation-result.md. Parent live executor and
limit-10 response-size verification remain pending; no activation performed.

# Approved verified Linear / Customer.io reads

Classification: Feature repair. Current user instruction approves execution.
Problem: verified bindings now exist but dedicated adapters still return unavailable.
Approach: finite Linear read queries with organization per page and team allowlist;
Customer.io GET-only fixed endpoints with exact workspace-set checks before/after.
Do not infer a default workspace selector or production attribution.

- [x] Read parent identities, existing code/rules/lessons; preserve baseline.
- [x] Verify vendor contracts and implement bounded projections and credential sealing.
- [x] Replace gate fixtures with positive and negative real-executor transport tests.
- [x] Run full relevant official tests, lint/types, and independent security review.
- [x] Check unrelated-file hashes; update /tmp/codewords-readonly-expansion-result.json.

Files: gateway/client_context_codewords_dedicated.py,
gateway/client_context_codewords_reads.py, tests/gateway/test_codewords_dedicated.py,
tests/gateway/test_codewords_expansion_security.py; research.md and tasks/todo.md.
Verification: foreign identity, caller injection, pagination, redaction, credential
rotation/deletion and revocation; canonical runner; compare incremental baseline
and main diff. No telemetry added; existing safe unavailable receipts detect errors.
KFI: any foreign-resource or sensitive-content release. Stop and re-plan if vendor
contracts require broader scopes or unverified selectors. No config, restart,
external posts, commits or PRs. Parent owns every live operation test.


# Approved Codewords continuity implementation (2026-09-21)

## Classification
Feature repair. Explicit implementation approval is in the execution brief.

## Problem Statement
Codewords follow-ups lose their earlier question/windows; channel evidence is
unavailable and Stripe endings omit voluntary/involuntary classification.

## Approach
Use fresh bounded native Slack evidence in the isolated loop and extend the fixed
Stripe aggregate. research.md records verified paths. Reject ordinary sessions
(private context) and Slack search/general tools (caller-controlled authority).

## Implementation Steps
- [x] Preserve exact dirty baseline and inspect routes, plans and current contracts.
- [x] Verify itsezmoney and create implementation parent/children with readback, or record exact blocker.
- [x] Prove scoped history/continuity and Stripe classification/delta tests fail on baseline.
- [x] Add fixed-channel bounded history reader and fresh immutable-thread context.
- [x] Restore authenticated deletion handling and fence history revisions/client identity.
- [x] Integrate supplied history tool and continuation guidance in the isolated loop.
- [x] Extend Stripe aggregate with explicit reasons and computed comparable-window deltas.
- [x] Run focused and adjacent integration/isolation/revocation suites, lint and types.
- [x] Obtain independent correctness/security review; fix any blocking findings.
- [x] Create scoped EZ MONEY ship artifact, preservation proof and activation/rollback handoff.

## Files to Modify
Functional source: gateway/client_context_codewords.py,
gateway/client_context_codewords_history.py (new), gateway/client_context_codewords_reads.py,
plugins/platforms/slack/client_context.py.
Tests: tests/gateway/test_codewords_history.py (new), test_codewords_continuity.py (new),
test_codewords_stripe.py, test_codewords_reads.py, test_codewords_tool_loop.py,
test_slack_channel_isolation.py (fixture/schema expectations).
Append-only work notes: research.md, tasks/todo.md. Scoped shipping receipts under
EZ MONEY/config/hermes/patches/. Scope exceeds ten paths only to retain existing
schema/fixture contracts across the security boundary; no global refactor.

## Verification Plan
- [x] Red/green behavioral proofs for continuity, fixed scope and arithmetic.
- [x] Official scripts/run_tests.sh using existing venv, synthetic transport/temp HERMES_HOME.
- [x] Relevant deletion/participation/isolation/credential revocation regressions.
- [x] Scoped Ruff/ty and diff review against saved baseline plus HEAD.
- [x] Preservation hash proof for unrelated dirty files and exact artifact checksums.
CI/live activation is parent-owned; no push or remote CI claimed.

## Tracking & Observability
No telemetry. Bounded result receipts expose timestamps, links, pagination and
unavailable/incomplete states. KFI: any wrong-channel, private-context or stale-authority read/output.

## Risks & Mitigations
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Historical instructions become authority | Medium | High | Data-only packet; fixed dispatcher/resource validation |
| Context survives revocation/edit/deletion | Medium | High | Fresh reads plus revision and existing authority fences |
| Partial counts imply complete churn | Medium | High | Withhold totals/deltas on incomplete scans |
| Existing work is lost | Low | High | Private snapshot and unchanged-file hash proof |

## Out of Scope
Gateway restart/activation, Slack traffic, config/credential/permission/profile changes,
private sessions, unrelated integrations/refactors, upstream PR/push.

## Stop Conditions
Re-plan if exact authenticated identity or revocation cannot be enforced. Report
unavailable integration/tracking honestly; never broaden scope to bypass it.


Continuity review refinement: restoring deletion handling also requires the normal
fresh-direct-mention reopening rule. Include a surgical optional scoped-source check
in plugins/platforms/slack/deletion.py and reuse that helper from scoped dispatch;
do not duplicate tombstone logic or use global sender privileges. Add a native
lifecycle regression. Unavailable history still requires an active participation grant.


Real-SDK review refinement: the existing participation API-call fence rejects all
Codewords methods except final posts and requires the original request task.
Add an exact-request read capability in plugins/platforms/slack/participation.py;
bind fixed client, method, payload, task and inherited native participation. Enter
it inside the SDK coroutine rather than outside wait_for's child task. Keep all
write/deferred/general-tool fences. Test the actual fenced SDK with fake HTTP,
including foreign channel/thread/method and child-task rejection. No unfenced SDK
or primary-client fallback is permitted. This is necessary for real history reads.


Final continuity verification: 74 files / 2,417 passed / zero failures, official
runner with test-only links to the installed Slack SDK/Bolt. New gateway modules
and new/Stripe tests pass ty; all 13 source/test files pass Ruff. The three shared
Slack plugin files retain 13 existing diagnostics; baseline comparison finds no
new type errors. Independent review APPROVE with all concrete findings closed.
77 unrelated baseline files, existing EZ MONEY edit, staged diff and HEAD preserved.
Linear tracking blocked before workspace verification by api.linear.app DNS
(gaierror errno 8); zero mutations/IDs. Approved execution brief remains authority.
Ship artifact: EZ MONEY/config/hermes/patches/codewords-continuity-2026-09-21/.
Source implementation is ready; parent activation and genuine live ingress proof
remain pending. No gateway restart, Slack send, config or credential edit, commit,
push or upstream PR performed.


# Approved additive Codewords UX parity (2026-09-21)

## Classification
Feature repair. User addendum approves immediate scoped implementation.

## Problem and reasoning
Problem: isolated Codewords replies lack processing eyes, natural requester follow-ups and native Slack presentation.
What breaks: broad reactions/history grants or media uploads could bypass task isolation/revocation.
Simpler approach: reuse native lifecycle hooks and addressing predicates; add only exact UI capabilities and bounded continuation receipts.
Constraint: preserve authenticated native target, task, client, deletion and policy checks throughout.
Work type: additive feature repair, not a general session or media platform.

## Approach
Use existing on_processing lifecycle with a Codewords-specific exact eyes capability; allow owned-eyes removal only during terminal cleanup after revoke. Follow-up receipts last 15 minutes from an explicit native summon, at most 8 unmentioned follow-ups, maximum 128 records; no sliding extension or historical grant. Revalidate current native human, requester/thread, policy, deletion epoch and credentials. Static lowercase answer-first prompt plus deterministic Slack rendering permits bullets/bold but no pipe tables, mentions/unfurls/arbitrary links. Charts remain unavailable for the concrete byte-binding/late-file-cleanup blocker in research.md.

## Steps
- [x] Save phase-2 baseline and phase-1 artifact hashes; read current lifecycle and contracts.
- [x] Research narrow additions and media feasibility; record parent tracking/live evidence separately.
- [x] Add failing real native sequence, processing lifecycle and exact SDK payload tests.
- [x] Implement requester receipt/admission/stop with denial and expiry regressions.
- [x] Implement bounded processing eyes and owned terminal cleanup with native lifecycle.
- [x] Implement safe Slack presentation and truthful visual fallback guidance.
- [x] Run canonical focused and adjacent regression suites; lint/types against baseline.
- [x] Independent review; fix in-scope findings and repeat affected checks.
- [x] Package additive patch, forward/reverse hashes, preservation and concise handoff in EZ MONEY.

## Expected files
New Slack codewords_followups.py and codewords_ux.py helpers; surgical spans in client_context.py, participation.py, adapter.py; static prompt/failure marker in gateway/client_context_codewords.py. New native UX/follow-up tests, existing delivery/schema fixture expectation adjustments only as required. Append-only research.md/tasks/todo.md/tasks/lessons.md. No global configuration, permissions, ordinary-session behavior, media capability or CI changes.

## Verification and guardrails
Native entry point sequence: explicit summon -> answer -> same requester unmentioned follow-up -> second answer -> channel-evidence follow-up. Eyes precede work on exact original trigger and are removed on success/failure/timeout/cancel; denied/foreign/bot/revoked requests create no reactions. Cleanup after revoke can remove only owned eyes. Real SDK fake HTTP validates exact payload/client/task and no mention/unfurl or alternative write escape. Negative cases cover another person/thread/channel, other-human addressing, expired/exhausted/changed-policy/client/deletion receipts and stop/re-summon. Official scripts/run_tests.sh only. Preserve phase-1 artifact and all unrelated hashes. KFI: any wrong-target/stale-authority output or unintended ambient response. Stop/re-plan if task capability must become a general mutation or media path. No restart, live Slack traffic, commits/push or duplicate tracking.


Phase-2 review refinement: current adapter channel controls must revoke the new capabilities consistently, so use a Codewords-only _codewords_channel_allowed predicate in native participation and idle invitations. A queued worker rejected before entering the base lifecycle must release only its own current session guard; reuse the existing invalid-admission cleanup at that exact early check. Do not catch downstream lifecycle exceptions globally. Prove queued allowed/ignored revocation leaves no active session/task/admission and a later explicit native summon can run.


Final phase-2 verification: canonical official runner, 76 files / 2,485 passed /
zero failures (23.1s). Independent final five-file run: 165 passed, with 11 source/test
hashes unchanged; verdict APPROVE and all four concrete findings closed. Ruff passes
for all 11 changed source/test files; scoped types pass; shared files retain baseline
diagnostics with zero new issues. Chart publication remains denied for the approved
byte-binding/late-file-cleanup blocker permitted by D. Additive artifact:
EZ MONEY/config/hermes/patches/codewords-ux-parity-2026-09-21/.
Phase-1 artifact hashes are preserved; 92 baseline dirty files saved, 82 unrelated
files untouched, 10 existing files intentionally edited and four added. No restart,
Slack traffic, config/credential changes, duplicate tracking, commit or push. Parent
provided resolved tracking and live read proof; activation/inbound UX proof is pending.


# Approved additive Codewords deterministic chart (2026-09-21)

## Classification
Feature repair, explicitly approved by the phase-3 user brief. No new approval gate.

## Problem and reasoning
Problem: explicit native requests for a Stripe comparison visual still receive a limitation.
What breaks: generic media permissions or model-chosen bytes/destination can violate isolation; late completion can publish after revoke.
Simpler approach: preserve ScopedReply and add one server-owned comparison receipt plus an exact three-stage operation.
Constraint: current native human/task/route/credential/deletion authority at every stage, with only owned-file cleanup allowed after revoke.
Work type: narrow feature repair, not a general artifact platform.

## Approach
Use the actual current validated Stripe read to freeze a reconciled aggregate, render it with installed Pillow, and bind its digest and deterministic summary to the native task/client. The chart send is single-use. Allocation records its owned ID immediately; exact no-redirect bytes and exact completion follow only while authority is current. Bounded in-flight stage settlement precedes owned cleanup on any failure/revoke/cancel. Missing data/render/upload failure yields honest bullets when the request remains authorized. Normal answers keep their existing path. Reject broad generic upload support and model-emitted media directives because neither supplies byte or destination provenance.

## Implementation steps
- [x] Preserve baseline and earlier artifact hashes; research current loop/receipt/SDK paths.
- [x] Add red validation/render tests and real native SDK fake-HTTP chart tests.
- [x] Implement immutable aggregate/render receipt and explicit current-request selection.
- [x] Implement exact upload stages, strict URL/transport, single-use capability and owned cleanup.
- [x] Prove tamper, revoke/cancel/race/failure behavior and honest lifecycle/followups.
- [x] Run full canonical scoped regression and scoped lint/types with baseline comparison.
- [x] Independent correctness/security review; fix and retest until no in-scope findings remain.
- [x] Package incremental patch/current source and test slice, evidence/hashes and exact forward/reverse proof.

## Files
New gateway/client_context_codewords_chart.py and plugins/platforms/slack/codewords_upload.py; surgical changes gateway/client_context_codewords.py, plugins/platforms/slack/client_context.py and participation.py; new chart and upload tests under tests/gateway; append-only research.md, tasks/todo.md and tasks/lessons.md. Reconsider if additional files are necessary.

## Verification and observability
Official scripts/run_tests.sh only with isolated home/credentials and test-only SDK import links. Native admission through real tool executor/provider fake HTTP and fenced Slack SDK. Check exact bytes/hash/length/URL/client/task/file ID/destination/comment and owned cleanup after each revoke boundary. Run deterministic PNG validation/visual inspection, zero-baseline/category reconciliation and injection denial. Logs expose only fixed bounded operation status, never URLs/tokens/rows. Compare shared type diagnostics against exact baseline. KFI: any unsolicited, foreign, stale or duplicate publication, or falsely successful chart delivery. Retain baseline and earlier-package hashes.

## Risks and mitigations
Unknown remote completion is settled within a bounded request and followed by deletion of its allocated ID; changed credentials prohibit using a replacement identity for cleanup and must be reported as an evidence limit. Strict host/transport rejects unsupported upload URLs with honest fallback. Single-use receipts prevent SDK/send retries duplicating a file. No arbitrary provider text enters image/comment.

## Out of scope and stop conditions
No live Slack traffic, restart, config/credential/profile edits, tracking changes, commits/push or ship-worktree edits. Parent owns activation/shipping. Re-plan if a generic media permission, hidden general tools, arbitrary path or cross-task publication would be required. User now requires closing all concrete in-scope findings before handoff, not deferring chart safety issues.


Phase-3 reviewed file refinement: codewords_ux.py must participate in chart terminal cleanup and finalize follow-up success only after owned indicators settle. This is necessary to close chart acceptance/cancellation races, not a general UI expansion. Nine source/test files plus three append-only planning records is the smallest coherent scope; the earlier phase packages remain immutable.


Final phase-3 result: deterministic chart/receipt/upload and owned cleanup implemented. Canonical 79 files / 2,694 passed / 0 failed, Ruff clean, scoped types clean, no new shared-file diagnostics. Independent reviews APPROVE (277 and 281 tests) with all three concrete findings closed. Exact forward/reverse patch proof passes for 12 changed paths; nine current source/test files included. All 89 unrelated baseline files, both earlier 28-file packages, native HEAD/index and existing EZ MONEY edit preserved. Artifact: EZ MONEY/config/hermes/patches/codewords-chart-delivery-2026-09-21/. No live Slack traffic, restart, config/credential edits, ship-worktree edits, staging/commit/push or merge. Parent owns activation and genuine inbound proof.


## Approved parent phrase correction (phase 3 refresh)

Feature repair authorized by the parent review and latest user instruction. Modify only the existing design verb matching and its detector/native response tests; retain all upload/authority source bytes.

- [x] Reproduce exact design request failure with detector and native fake-HTTP tests, alongside negated/quoted/unsupported phrasing.
- [x] Add design to affirmative and late-negation verb rules; run focused canonical tests and lint/types.
- [x] Run full canonical regression with stable source hashes and final independent narrow review.
- [x] Refresh the existing phase-3 package, final evidence/checksums and exact forward/reverse proof; preserve the first two frozen packages and all unrelated files.

Source/test files: gateway/client_context_codewords_chart.py, tests/gateway/test_codewords_chart.py, tests/gateway/test_codewords_chart_delivery.py. Append-only research, plan and lessons record the correction. No fourth package, live traffic, restart, push, other-profile changes or parent worktree edits.

Final parent phrase result: four exact-form detector/native red failures closed by two production regex edits. Focused 141/141, full canonical 2,709/2,709 across 79 files and independent narrow 224/224 all pass with retries disabled. Final review APPROVE, zero in-scope findings; Ruff nine files and scoped ty seven files pass. All nine final source/test hashes agree across full regression and independent review. Existing phase-3 package refreshed in place with current evidence and checksums; forward/reverse proof restores all 12 paths exactly. The 95 unrelated pre-phrase files, 89 unrelated original phase-3 files, both frozen 28-file packages, native HEAD/index and existing EZ MONEY edit remain unchanged. The rhetorical why-don't-you form remains explicitly unsupported and tested; use design a visual of the Stripe comparison. No implementation/review blocker remains; shipping, activation and genuine live inbound proof stay parent-owned.
