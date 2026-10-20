# Monaw reliability implementation plan

Status: implementation started. See [implementation progress](reliability-progress.md) for delivered changes, verification evidence, and remaining work.

Prepared on 20 October 2026 from the supplied **Monaw Detailed Improvement Plan** and the local source at `ec2bd7df7006ee11469d9ea80f6d1f25aa8fc2eb`. The checkout matches the reviewed baseline and was clean before this document was added. The supplied CI results are historical evidence; tests and CI have not been rerun for this planning pass.

The objective is to earn a reassessment through reliable execution, truthful boundaries, and a usable Windows installation. Completing a checklist does not by itself establish an 8.5/10 score.

## 1. What the source inspection changes about implementation

| Finding in this checkout | Implementation consequence |
| --- | --- |
| `scheduler._finish_run()` passes `succeeded` to a notifier that recognizes `ok`. | Fix the caller-to-notifier contract and test the complete completion path. |
| Completion computes enabled state, next occurrence, and failure count from the run-start row, then commits the run and task independently. | Introduce a transactional finalization operation with explicit configuration and attempt ownership. A fresh read alone is insufficient. |
| `claim_due_scheduled_task()` commits a blank `next_run_at` before a run is created. | Investigate the claim-to-run crash window as part of scheduling recovery; preserve due intent atomically. |
| The per-task limit is checked before overlap policy; queued wrappers count toward inflight limits. | Exercise normal dispatch before redesigning overlap handling, then separate queued occurrences from running resources. |
| Run rows already have leases, idempotency keys, and a partial unique index. | Extend the existing mechanism rather than introducing another job system. Verify lease renewal, queued recovery, and ownership fencing. |
| `_required_scope()` treats all sandbox requests as reads. | Correct authorization before any Docker lookup, pull, probe, or settings write. |
| Run-now generates a fresh random key on every call and immediately requests predecessor cancellation. | Add durable request deduplication and resource-safe replacement before offering retry in the UI. |
| `App.tsx` restores captured schedule lists after failed mutations; rename and run-now suppress errors. | Reconcile by item and request generation; consume the existing structured API errors. |
| MCP configuration has plaintext `env` and `headers`; Electron's vault accepts only fixed provider IDs. MCP startup precedes Electron credential synchronization. | Design server-bound credential references, narrow trusted handoff, and a credentials-ready lifecycle together. |
| Python is pinned to 3.11, required CI uses 3.12, and setup accepts Node 18 despite the README's higher minimum. | Align runtime declarations, setup, CI, and packaged runtime before distribution work. |
| Electron has packaged-resource branches but launches `uv`; package scripts build only the renderer. | Validate backend packaging and launch lifecycle before calling a renderer build an installer. |

Relevant implementation surfaces: [scheduler](../backend/app/agent/scheduler.py), [database](../backend/app/agent/database.py), [bootstrap](../backend/app/agent/db_bootstrap.py), [schedule API](../backend/app/api/routes/scheduled_tasks.py), [control-plane authorization](../backend/app/security/control_plane.py), [App](../frontend/src/App.tsx), [MCP connections](../backend/app/skills/mcp_bridge/connection.py), [Electron main](../frontend/electron/main.js).

## 2. Proposed product and engineering defaults

These are implementation recommendations, not descriptions of existing behavior. Record the final contracts beside their tests before changing dispatch or persisted formats.

| Decision | Recommended contract |
| --- | --- |
| Python | Keep 3.11 as the application runtime. Make it the required Windows verification baseline; keep a focused 3.12 compatibility job if useful. Distinguish dependency metadata's allowed range from versions actually supported and tested. |
| Node | Use Node 22 for development/build CI, with the documented 22.12 minimum. Preserve the documented 20.19+ compatibility line only if the locked dependency engines and checks support it. Do not accept arbitrary newer major versions without an explicit support policy. |
| Disable while running | Stop future automatic dispatch and cancel pending queued occurrences. Let the active run finish; disabling is not a promise that an executing native action has stopped. |
| Edit while running | The active attempt keeps its execution input snapshot. Saved configuration governs future occurrences. A schedule change creates a new scheduling revision/anchor. |
| Notification edits | Destination and content settings are snapshotted for an accepted run; changing them affects future runs. Revalidate the destination allowlist at delivery. Document this clearly. |
| Recurrence | Use fixed cadence with missed occurrences coalesced, rather than building an unbounded offline backlog. Preserve the first-run interval behavior unless an intentional change is documented. |
| Queue | FIFO for accepted due occurrences, initially at most one pending occurrence per task and a finite global cap. Record overflow as a skipped occurrence with a reason. Queued work does not consume an execution slot. |
| Replacement/manual run | Request cancellation, then wait until predecessor execution resources are actually available. An unfinished synchronous worker prevents conflicting replacement. Manual requests obey global limits. |
| Manual retry | Reuse one client-generated idempotency key for the same intended run. A new explicit user run gets a new key. |
| Failure threshold | Keep five consecutive execution failures. An unrelated configuration edit does not reset failure history; re-enabling after automatic suspension is explicit. Old attempts cannot suspend or rewrite the summary of a newer accepted attempt. |
| Chrome policy | Keep the current explicit navigation-target boundary and make its wording accurate. A strict all-content origin policy is a separate design requiring target-aware enforcement, potentially in a separate browsing context. |
| First installer | Target Windows x64; validate Windows 10 and 11 if retaining the README's support claim. Start with a manual update path. Other architectures and automatic updates follow demonstrated support. |
| Destructive actions | Use confirmation for irreversible conversation deletion. Offer undo only if recoverable storage is implemented. |

Before SCH-03 merges, specify cron spring-forward and fall-back examples, how ambiguous local times are resolved, and how offline one-time tasks are handled. Use the same calculation for preview and dispatch. Recommended behavior: skip nonexistent local times, run an ambiguous local occurrence once, and coalesce overdue eligible work to at most one occurrence. Persist enough identity to deduplicate that choice. These semantics require tests; they are not assumed properties of the current cron library.

## 3. Delivery order and review units

Each row is a separate reviewable change or small series. No pull request creation or publishing is part of this planning task.

| Order | Package | Scope / dependency | Completion evidence |
| --- | --- | --- | --- |
| 0 | Reproduction baseline | Record interpreter, lockfiles, commit, exact failing tests, sanitized diagnostics. | Understand the three original failures before changing assertions or refactoring. |
| 1 | SCH-01 notification contract | Canonical outcome mapping; separate delivery failure from execution outcome. | Completion-to-fake-bot tests for success, failure, empty output, and transport failure. |
| 2 | SEC-01 API scope | Method-sensitive sandbox scope plus a small mutation-route inventory. | Read GET allowed; read POST denied before all side effects; write POST validated normally. |
| 3 | SCH-02 completion ownership | Versioned task configuration and transactional, attempt-fenced finalization. | Concurrent edits, deletion, duplicate finalization, stale attempts, and fresh/upgrade database tests. |
| 4a–c | REL-01 execution repairs | Approval, cancellation, and real Docker diagnosis in separate changes where possible. | Original behavioral assertions and surrounding groups pass without weaker controls. |
| 5 | DIST-01 runtime/setup | Python policy and Node version/PATH checks across launchers, metadata, docs, CI. | Supported boundary versions pass; unsupported/stale-PATH cases explain remediation. |
| 6 | SCH-03 dispatch/recovery | Normal-dispatch contract tests first; occurrence/queue redesign only as required. Depends on package 3. | Injected-clock tests for skip, queue, replacement, resource limits, crash/restart, and DST. |
| 7 | UX-01 safe action feedback | Run-now idempotency API first; per-item pending/error/reconciliation UI second. | Lost response does not duplicate work; concurrent row edits survive independent failures. |
| 8 | SEC-02 + SEC-04 truthful boundaries | Effective execution metadata and navigation-policy wording. | Approval, result, diagnostics, settings copy, and docs agree with actual behavior. |
| 9 | QA-01 foundation | Disposable Electron/backend harness and controlled provider/tool fixtures. Start after lifecycle contracts stabilize. | Streaming persistence, allowed/denied action, stop, schedule lifecycle, backend interruption. |
| 10 | SEC-03 MCP credential lifecycle | Vault contract, connection-bound references, trusted handoff, then migration/UI. | Sentinel-secret, rotation, restart, failed migration, cross-server isolation tests. |
| 11 | ARC-01 bounded extractions | Runtime batch/finalization ownership and settings sections. Depends on passing behavioral baseline; coordinate with package 10. | Existing behavior remains intact; extracted boundaries have focused contract tests. |
| 12 | UX-02 + first-run guidance | Shared dialog/listbox behavior, keyboard history, confirmation, capability readiness. | Component checks plus keyboard-only workflow walkthrough. |
| 13 | DIST-02 packaging | Backend artifact proof, packaged launch, installer, then manual upgrade. Depends on runtime policy and migration behavior. | Clean Windows install, shutdown/restart, missing optional dependencies, upgrade/data preservation. |
| 14 | QA-01 expansion + OBS-01 + DOC-01 | Run harness against installer; bounded diagnostic evidence and measured baselines; release documentation. | Exact-candidate verification, installer matrix, secret-free export, measured reference-machine results. |

Stage A comprises packages 0–9: correctness, immediate trust, setup, and application-test foundation. Stage B comprises packages 10–12: credential migration, ownership, and usability. Stage C comprises packages 13–14: installed product and release evidence. The rating reassessment should consider the complete delivered scope, especially installation and recovery.

## 4. Implementation contracts for the highest-risk work

### SCH-01: outcomes and notifications

Use a typed scheduler outcome for execution, with named adapters for task-summary/API compatibility and notification formatting. Keep successful/failed/cancelled/skipped execution distinct from automatic task suspension and delivery status. Preserve existing historical status values through explicit read adapters rather than silently rewriting all history.

For the smallest fix, avoid a broad schema migration: normalize the notification input, define an empty-success message, catch delivery errors outside the authoritative execution commit, and record sanitized delivery evidence. Durable delivery retries can follow as a separate persisted delivery-attempt feature. They must reuse the completed result, never rerun the agent, and must acknowledge uncertain external delivery after a lost Telegram response.

Test `_run_prepared_task()` through completion with a fake bot, including unavailable bot, disallowed destination, formatting/chunking, and send exception. No external Telegram message is needed.

### SCH-02: task configuration versus run ownership

Primary files: `scheduler.py`, `database.py`, `db_bootstrap.py`, a new numbered migration, schedule route models, API types, scheduling tools, and `test_scheduler.py`.

Introduce a configuration revision independent of `updated_at`, since runtime bookkeeping also changes that timestamp. Route and tool configuration writes use an explicit configuration-update API; runtime bookkeeping cannot write arbitrary configuration fields. Snapshot the revision and inputs on an accepted attempt. A separate attempt/occurrence identity fences ownership of the current task summary; a configuration revision alone cannot identify the newest run.

Implement a database finalization method that:

1. Acquires the database lock and a short SQLite write transaction before reading authoritative task/run state. Use direct statements inside it, not existing helpers that commit individually.
2. Verifies task existence, run/task association, nonterminal state, and lease/attempt ownership. Expired ownership must be renewed or rejected consistently; never replay uncertain effects merely to recover a lease.
3. Commits the run's terminal evidence at most once. An older valid run may retain its own result without overwriting a newer attempt's summary.
4. Preserves current configuration, enabled state, and schedule revision. Computes the next eligible occurrence from current configuration under that transaction/version guard. Under fixed cadence, completion must not replace a newer next occurrence established by dispatch.
5. Applies documented one-time and failure-threshold rules atomically, including explicit re-enable and edit precedence. Automatic suspension is a task disposition, not a replacement for the execution outcome.
6. Returns whether finalization was newly applied and what events/delivery work are due. Emits events and performs network delivery only after commit. Duplicate callbacks do not reapply counters, publish duplicate terminal transitions, or resend a locally recorded delivery.

Regression matrix: disable, interval edit, cron edit, timezone edit, once-to-recurring edit, destination edit, deletion, duplicate completion, stale completion after a newer attempt, and threshold crossed during an edit. Add a two-connection interleaving check so the single-process reentrant lock is not mistaken for cross-connection transaction safety. Test migration from the reviewed schema and a fresh database, including repeated initialization.

### SCH-03: occurrence claims, overlap, and restart

Start with failing tests that block the first runner, advance an injected clock, and call `_fire_due_tasks()`. Direct `_run_after_previous()` tests do not prove overlap dispatch works.

Use the existing run table as the occurrence ledger if it can express the contract; add a separate queue table only if needed. Atomically claim an occurrence, create or reuse its unique run record, and advance scheduling intent. Include the schedule revision in occurrence identity so a legitimate edit does not collide with an old occurrence at the same timestamp. Create/reuse the conversation without an independently committed orphan-producing step.

Evaluate overlap policy before generic per-task rejection, but acquire execution capacity before starting work. Keep pending counts separate from executing counts. Persist skipped/overflow reasons. For replacement, serialize cancellation, worker quiescence, and resource acquisition; sharing a conversation must not permit overlapping writes.

On restart, reconcile both queued and running attempts, task enabled/deleted state, future occurrences, leases, and uncertain effects. The current recovery code only handles expired running rows, so queued recovery and the claim-to-run gap need explicit coverage. Verify long runs outliving the current 15-minute lease and define renewal/fencing. Do not equate an expired lease with permission to repeat a mutating operation.

Offline catch-up is bounded: do not instantiate thousands of historical runs. Record coalesced counts/reasons and derive the next cadence point from a stable anchor. Test poll duplication, two claimants, crash after acceptance, disable/delete before queued start, queue overflow, shutdown, manual requests, timezone transitions, and resource-blocked cancellation.

### REL-01: diagnose before extracting

| Failure | First boundary to instrument | Required regression contract |
| --- | --- | --- |
| Auto-approved operation never invokes executor | `execution_gate.py`, `approval_broker.py`, `execution_resume.py`, executor entry/exit and ticket expiry | Exact stored validated payload executes once; denial, expiry, changed policy/payload, malformed review, duplicate resume, and interruption remain safe. |
| Completed parallel sibling becomes cancelled | Callable return → executor result → `_record_call_result()` → cancellation drain → persisted ledger in `turn_loop.py` | Captured result remains complete; uncompleted/stopping work stays distinct; original call IDs and model-order results survive. |
| Real Docker simple command errors | `sandbox/backends/docker.py`: launch, exit status, workdir/mount permission, command, copy phase | Real command and both isolation assertions pass with the pinned image; no explicit-Docker/enforce host fallback. |

The approval regression uses a 10 ms gate timeout. The cancellation test signals from inside the callable before the full executor result is necessarily captured. These are diagnostic leads, not proven causes. Add deterministic barriers at the contract boundary and retain separate true timeout tests. Do not fix the suite by broadly increasing timeouts or redefining a valid approval away.

For Docker, retain bounded sanitized stdout/stderr, exit code, image digest, backend decision, and failure phase. Make the dedicated integration job fail when required Docker/image support is unavailable; keep optional local skips explicit. Validate the image inventory after the successful integration checks. Keep isolation and execution failures distinguishable.

### UX-01: safe reconciliation before retry

Extend the run-now endpoint/service/database to persist a client operation key with the accepted run. Duplicate keys return the original run/conversation without cancelling or starting anything again. Scope keys to the intended task and authenticated caller; reject conflicting reuse. The client retains the key across timeout/reconnect and can reconcile it even after the run finishes. Internal backend acceptance/start recovery must also avoid losing a committed-but-not-started run.

Use `ApiError`/`decodeApiError` from the existing client for schedule and conversation actions. Add per-item operation state with request generations, visible sanitized errors, retained rename input, and pending control states. On failure, reconcile only the affected row or refetch authoritative data; never replace the entire list with a captured snapshot. Suppress late navigation changes after the user switches conversations. A transport error is an uncertain outcome until reconciled, not an automatic invitation to repeat work.

Tests must cover simultaneous edits to different tasks, out-of-order same-task responses, failed deletion, rename failure, stale response after navigation, and an accepted run whose HTTP response is lost.

## 5. Boundaries, credentials, and maintainability

### SEC-01 and SEC-02

Require `settings:write` for sandbox mutation and retain `settings:read` for status. Route-level tests mint restricted tokens and assert zero Docker lookup/subprocess/probe/persistence calls on denial, including invalid credentials. Inventory other mutating routes against their intended existing scopes without introducing a new authorization framework.

Derive one effective-execution presentation from `SandboxDecision`/`SandboxRunMetadata`, including selected backend, workspace, network/filesystem enforcement, write strategy, approval requirement, and reason. Carry that decision consistently into approval, progress, result, and diagnostics. Reclassify at dispatch; a material change must pass the existing authorization flow again.

Host/advisory execution must say its network/filesystem restrictions are not isolated. Docker copy-out, discard, and direct-write modes need distinct wording. Long-lived sessions disclose their host boundary. Do not imply that shell isolation contains desktop automation, the personal browser, or third-party MCP processes.

### SEC-03: custom MCP credentials

Primary files: settings models/store and API schemas, Electron vault/main/preload and renderer typings, `main.py` startup, MCP `connection.py`, settings MCP section, diagnostics redaction, and their tests.

Use stable server identities and explicit references bound to `(server ID, transport, env-or-header field, key)`. Extend the vault through a validated MCP-specific namespace rather than arbitrary renderer-chosen storage keys. Keep nonsecret values ordinary configuration. Preserve omitted secrets; use explicit replace/delete operations; reject masked placeholders as input.

Resolve secrets only into connection-local launch/HTTP state. Do not populate general settings with plaintext or grant every MCP process every credential. Existing `env={**os.environ, **cfg.env}` handling needs an inheritance review so new injected secrets cannot leak to sibling connections. Keep a narrow authenticated Electron-to-backend handoff and a runtime credential store outside general configuration readback.

Connections requiring secrets wait in `credentials_unavailable` until hydration completes. Rotation reconnects with the current value; restart rehydrates before connection; deletion revokes future resolution and applies the documented active-connection teardown policy. Headless development uses an explicit per-server runtime-injection mechanism with equivalent redaction and no plaintext persistence fallback.

Migration sequence: identify fields designated as secrets; encrypt and verify; durably store the scoped reference; atomically remove the legacy plaintext field. Persist recoverable progress and test interruption at each step. Header names can suggest candidates, but arbitrary environment values cannot all be classified as secrets automatically: offer explicit migration selection, make remaining legacy plaintext visible, and never report a connection fully migrated while designated credentials remain plaintext. Do not create plaintext backup/export copies as part of migration.

Check sentinel values across settings JSON, API responses, logs, startup/process errors, diagnostic bundles, and frontend state. Cover two servers using the same field name, rotation, unrelated edits, encryption failure, partial migration, restart, removal, and malicious reference rebinding. Test restore under the actual Windows encryption/user context; an encrypted backup is not automatically portable to another account.

### SEC-04, ARC-01, and UX-02

Rename browser controls to navigation-target policy and document existing-tab, observation, redirect, popup, interaction-triggered navigation, and reconnect behavior. Preserve the separate `browser_fetch` redirect checks. Use controlled origins to verify what is enforced and what remains outside that boundary. Defer strict origin containment until the intended coverage and browser enforcement mechanism are selected.

After REL-01 is green, extract the smallest cohesive runtime responsibility first: tool-batch ownership/result capture is a strong candidate because its cancellation contract is central. Extract run finalization separately if justified. Keep provider adaptation and context-budget refactors out of the same patch. Preserve public event fields, tool identities, permissions, attachment scope, and persistence contracts.

In settings, establish draft-versus-saved ownership in the shell, then extract sandbox and MCP/connection sections one at a time. Keep immediate credential/reconnect effects distinct from drafts. Coordinate with the credential work so extraction does not preserve unsafe whole-settings saves. Reuse existing extracted observability/connection components instead of duplicating them.

Complete shared modal focus containment/restoration, initial focus, Escape handling, dropdown keyboard movement/selection, history keyboard activation, icon labels, and visible focus. Account for dropdown portals when trapping focus. Verify dirty-state prompts and irreversible-delete confirmation through the same keyboard flow.

First-run guidance should reach a harmless successful task: connect model, select workspace, explain permission mode, run example. Show browser/Docker/Telegram/MCP setup only when selected and report readiness from actual checks, not enabled toggles.

## 6. Windows packaging and application verification

Begin with a timeboxed packaging proof: a frozen backend artifact built on Windows with a fixed Python 3.11 toolchain, launched by Electron without `uv`, Git, Python, npm, or Vite installed. Validate native dependencies and dynamic resources, especially computer-use libraries, audio/transcription dependencies, tokenization data, migrations, prompts, and skill resources. If freezing is unsuitable, use an owned relocatable managed-runtime distribution; do not assume copying a developer virtual environment produces a relocatable installation. Record size, licensing, optional model downloads, and startup measurements before selecting the production strategy.

Then add a versioned Windows installer and packaged launch path. Keep mutable data outside installed files; preserve the established data home or migrate explicitly. Provide readiness/authentication checks, one owned backend, safe shutdown/restart, port-conflict reporting without killing unrelated programs, and actionable startup diagnostics. Existing Electron renderer isolation and control-plane authentication remain required.

Use a disposable user-data directory and fixture workspace for all application tests. First build a small controlled provider/tool harness against Electron plus backend, then run it against the packaged candidate. New backend test files must be assigned to `TEST_GROUPS` in `backend/conftest.py` so collection and CI include them.

Required workflows:

1. Start one authenticated backend, connect, stream a response, reopen the persisted conversation.
2. Permit a fixture file mutation once; deny another and verify unchanged content.
3. Complete one sibling, stop another, reopen and inspect persisted outcomes and unfinished workers.
4. Create a schedule, run it, disable while active, finish, restart, and observe no unintended future dispatch.
5. Interrupt the backend and reconcile without duplicate execution.
6. Start MCP, discover tools, hydrate/rotate credentials, reconnect, and handle schema change.
7. Exercise the documented browser policy against controlled origins.
8. Install on clean Windows, then upgrade from a supported previous candidate while preserving conversations, memory, settings, and usable credentials.

Test paths with spaces/non-ASCII characters, missing Chrome/Docker, occupied backend port, repeated launch, backend failure, and interrupted upgrade. Keep native Chrome/Docker/desktop checks and opt-in live provider checks separately identified from deterministic fixture workflows. A fixture pass does not establish every provider's reliability.

Publish checksums, architecture/OS support, migration notes, installation/recovery instructions, and versioned artifacts when release publishing is separately requested. Manual update comes first; automatic update requires artifact integrity/publisher validation, a recoverable migration path, and coordination with active runs.

## 7. Verification, diagnostics, and release gates

Use the existing scripts and workflow as the verification foundation. During implementation, run focused checks after each change, then its affected group. On the final combined candidate run the required full verification once, plus dedicated integration/installer checks. Broaden or repeat only after new changes or unresolved failures.

Representative commands from the repository root:

```powershell
# Focused scheduling and authorization checks; select narrower tests while iterating.
uv --directory backend run --locked pytest app/agent/tests/test_scheduler.py
uv --directory backend run --locked pytest app/agent/tests/test_control_plane_auth.py

# Execution regressions, followed by their surrounding group.
uv --directory backend run --locked pytest app/agent/tests/test_execution_gate.py app/agent/tests/test_turn_loop.py
./scripts/test-backend.ps1 -Group runtime -TimeoutSeconds 1200
./scripts/test-backend.ps1 -Group integrations -TimeoutSeconds 1200

npm --prefix frontend run verify
./scripts/verify.ps1
```

The dedicated Linux Docker job must exercise the real daemon, pinned image, simple command, isolation, and module inventory without silently skipping the required integrations. The installed Python 3.11 baseline must be represented by required Windows checks. Verify the actual executable/version rather than only the workflow label. Ensure every commit in a delivered series remains bootable/typecheckable under the existing per-commit integrity check.

Extend existing run events and observability records rather than adding a second logging pipeline. Correlate request, conversation, run, tool call, approval, schedule occurrence, and delivery using existing IDs. Add configuration revision, backend decision, outcome/reason, timing boundaries, and effect uncertainty where relevant. Bound record size and redact new paths before export. Make exported evidence inspectable before sharing.

Extend `scripts/measure-baseline.ps1` with fixture-driven cold/warm startup, authenticated backend readiness, fixed idle observation, long conversation rendering, representative database growth, and stop-to-worker-quiescence. Record machine/OS, application commit/version, interpreter, dependency baseline, and sample method. Establish measurements first; set regression thresholds afterward. Do not fill missing figures with estimates.

| Gate | Required evidence |
| --- | --- |
| Stage A | Original execution failures resolved; scheduler and authorization regressions pass; safe action reconciliation; effective environment/policy disclosure; runtime/setup aligned; deterministic application harness running. |
| Stage B | MCP migration/hydration/rotation passes with no sentinel leaks; partial migration recoverable; bounded extractions preserve lifecycle behavior; keyboard and first-task walkthrough pass. |
| Stage C | Clean supported Windows install and upgrade pass; packaged deterministic workflows pass; real integration boundaries identified; startup/recovery/storage baselines measured; release evidence names the exact candidate. |

Maintain a short evidence record per package: source commit, changed behavior, focused tests, group/integration result, migrations, and remaining limitation. Do not carry historical green results forward as proof of a new candidate.

## 8. Migration and recovery rules

Use new numbered migrations through the existing runner. Keep fresh schema/bootstrap declarations consistent with upgraded databases; verify repeated initialization. For scheduler changes, preserve historical run evidence and old status interpretation. Deduplicate via existing keys where possible; do not mass-replay old runs.

For every persisted-format change, record old-reader compatibility, backup method, restore test, and downgrade procedure. Use a consistent SQLite backup mechanism with WAL accounted for rather than copying an open database file blindly. Credential migration must retain recoverability without generating new plaintext copies. Packaging must not delete user data during an ordinary update.

Application rollback and user-data rollback are separate operations. If an older binary cannot safely consume the new data, require restoring a verified compatible backup; do not advertise seamless downgrade. Run migration/restore checks with synthetic baseline fixtures, never the user's live Monaw data.

## 9. First implementation session

1. Capture environment and reproduce the three exact failures with bounded sanitized evidence.
2. Add completion-to-notification regression coverage and ship the narrow SCH-01 correction.
3. Add restricted-token/no-side-effect coverage and ship SEC-01.
4. Implement SCH-02's transactional ownership boundary and migration with the edit/stale-attempt matrix.
5. Resolve the execution failures in separate patches as their causes become established, then continue through the delivery table.

Do not begin the broad runtime/settings refactor, automatic updates, strict personal-browser containment, or optional WebMCP expansion ahead of these correctness contracts. WebMCP remains P3 with a separate demonstrated use case.
