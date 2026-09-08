# EZ-826 local extension

## Research and classification

Feature, locally authorized by the implementation request. Existing research.md,
tasks/ and temporary fixtures belong to the previous work and remain unchanged.
The independent acceptance review requires preserving the pre-provider seam in
handle_turn. The normal runner resolves source toolsets but also restores identity,
memory and sessions; only its toolset resolver is reusable here. The pilot already
provides descriptor-relative hash-bound records, explicit runtime and output guards.

Architecture reasoning:
1. Problem: permitted retrieval and follow-up questions need bounded capabilities.
2. What breaks: ordinary agent reuse imports unrelated context and global executors.
3. Simpler path: two local record readers, a bounded dedicated provider loop.
4. Constraint: grants must remain valid at every read, continuation and release.
5. Classification: feature with a security boundary, no activation in this task.

## Plan

- [x] Extend snapshot policy for explicit tool policy and authorized decision history.
- [x] Add executor-level scoped readers and isolated bounded conversation history.
- [x] Extend explicit SDK completion to support a bounded tool loop at the same seam.
- [x] Add separate candidate/review source refresh artifacts without activation.
- [x] Exercise real gateway imports and SDK payloads with synthetic fixtures.
- [x] Run official tests, lint and available type checks; review and fix findings.
- [x] Document capability, omissions and evidence; prepare scoped files for permitted packaging.

## Files and verification

gateway/client_context.py, gateway/client_context_policy.py, new topical
client_context_turn.py and client_context_refresh.py; two matching test files;
this document and docs/client-context-implementation.md. Targeted regression tests
use scripts/run_tests.sh. Review diff against HEAD and local origin/main.

## Constraints, risks and stop conditions

No global tool registry execution, remote analytics, transcript search, memory,
fallback agent, activation, config edits or external mutation. Tool configuration
can only narrow a static function allowlist. Generation/snapshot changes invalidate
history. Provider/tool failure discards pending history and returns generic errors.
Candidate content is never approval. Stop and redesign if source/account binding
or pre-provider revalidation cannot be enforced. Guardrail: zero unauthorized bytes
or side effects; no new outbound telemetry.

## Verification

Official 20-file regression set: 648 passed, zero failures, file retries disabled.
Scoped suites: 131 existing, 79 turn/executor/history, 29 reviewed refresh tests.
Ruff, compileall and diff whitespace checks pass. `ty` is absent; static type
checking was attempted but is not claimed. Independent review approved after
history downgrade, exact pre-request validation and history-lifecycle repairs.
Exact bounded conversation replay preserves the provider prefix; turn/byte limits
reset the whole scoped conversation before a new request rather than rewriting it.
Implementation is reviewed against the initial worktree HEAD. The local origin/main
comparison includes substantial preexisting upstream divergence; no rebase or
unrelated reconciliation belongs to this task.
