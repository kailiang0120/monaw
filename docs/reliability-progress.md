# Reliability implementation progress

Updated: 25 October 2026. Original baseline: `ec2bd7df7006ee11469d9ea80f6d1f25aa8fc2eb`; first four batches checkpointed in `98ec1fc`.

This records five implementation batches against the [implementation plan](reliability-implementation-plan.md). The full release plan remains in progress. The fifth batch is local and uncommitted; this session has not pushed changes, opened a pull request, published a release, or run GitHub-hosted CI.

## Delivered in the first batch

| Work item | Behavior implemented | Evidence |
| --- | --- | --- |
| SCH-01 | Canonical scheduler outcomes map explicitly to task summaries. Successful Telegram notifications carry the result, including a truthful no-output message. Delivery exceptions are recorded separately without raw transport exception text or changing execution success. | Completion-to-fake-bot regression tests reproduced the original mismatch before the fix and now pass. |
| SEC-01 | Sandbox status requires read scope; image mutation requires write scope before Docker lookup, subprocess, probe, or settings access. API documentation uses the same scope selector as enforcement. | Restricted-token/no-side-effect tests, authorized validation, existing authentication tests, generated API-doc check. Mutating-route inventory found no other mutation under the `files:read` prefix; settings/account and diagnostics mutations already select their write/control scopes. |
| SCH-02 completion boundary | Current saved configuration governs completion. One transaction finalizes the run and task state. Duplicate finalization does nothing; old attempts retain their evidence without replacing newer task state. Deletion prevents late task/events recreation. | Disable, interval/cron/timezone, once-to-recurring, stale/duplicate attempts, deletion, identity mismatch, transaction rollback, and two-connection tests. |
| SCH-02 revision/lease support | Migration 0015 adds configuration revisions to tasks and attempts. Configuration updates can reject a stale revision. Owned queued/running work renews its lease; expired/recovered work cannot restart/finalize under the old lease. Queued work rechecks enabled/deleted state before executing its accepted input snapshot. | Fresh/legacy initialization, repeated initialization, revision conflict, live lease renewal, expired/recovered lease, and original scheduler regressions. |
| REL-01 approval boundary | An already-signalled decision is consumed before scheduling a waiter or exhausting its wait budget. Pending waits no longer leave shielded event-wait tasks behind on timeout. Stored operation validation and current-policy replay remain in force. | A zero-wait resolved-approval case failed before the fix. It now passes alongside 10 ms/normal waits, denial, expiry, changed-context and replay tests. |
| REL-01 async cancellation boundary | A returned coroutine result is attached to cancellation/timeout evidence even if its awaiting wrapper has not resumed. Turn-loop persistence can retain that result instead of marking it cancelled. | A deterministic same-loop-turn cancellation test failed before the fix; it now passes, as does the original completed-sibling test and surrounding execution suite. |
| DIST-01 | Setup and launch check Node using the policy in package metadata, validate the executable again after installation, and report unsupported versions/stale PATH. Required Windows backend CI targets pinned Python 3.11; a separate job exercises runtime compatibility on 3.12. Frontend CI covers supported Node 20.19, 22.12 and 24 lines. | Node minimum/unsupported/prerelease tests, actual Node probe, Windows PowerShell helper check, workflow YAML parse, PowerShell syntax checks, full frontend verification, local Python 3.11 and isolated 3.12 results below. |
| Docker verification foundation | Required Docker integration mode fails when the daemon/image is unavailable; local optional mode skips explicitly. The simple-command assertion now includes image, backend, exit status, timeout, reason, and fixture stderr. | Required mode produced two explicit unavailable-Docker errors instead of skips locally. The real command failure itself remains unresolved. |

The legacy `dead_letter` run status is retained for API compatibility in this batch. Notification formatting uses the actual execution outcome. Durable delivery retry and a broader status/history migration have not been added.

## Delivered in the second batch

| Work item | Behavior implemented | Evidence |
| --- | --- | --- |
| SCH-03 acceptance and cadence | A transaction accepts an occurrence, snapshots its input, creates its conversation, and advances cadence. Actual execution starts through a separate atomic queued-to-running transition. Missed recurring occurrences coalesce; polling does not build an unbounded historical backlog. | Normal-dispatch blocked-runner tests reproduced the missing next occurrence before implementation. Competing connections, failed-build rollback/no orphan conversation, repeated polling, cadence preservation and offline coalescing are exercised. |
| SCH-03 overlap and capacity | Skip, bounded queue and cancellation/replacement work through ordinary dispatch. Defaults: four active globally, one active and one pending per task, sixteen pending globally. Queue overflow has an explicit history reason. Native workers still stopping block replacement and consume capacity. | Tests cover all three policies, overflow, disable with pending work, global capacity, resource-safe replacement, and cancellation before the first coroutine turn. Pending work is not counted as an active execution. |
| SCH-03 recovery and DST | Clean shutdown releases unstarted queued work; expired queued ownership can be adopted once. Expired running attempts are abandoned without replay. Legacy blank claims are repaired to future scheduling intent. Cron skips nonexistent local times and uses the first occurrence of ambiguous local times. | Restart/adoption, lease fencing and legacy-intent fixtures; named New York spring-forward/fall-back examples use the same calculation as preview and dispatch. A failure-threshold regression ensures pending/skipped work cannot hide an active run's fifth failure. |
| UX-01 safe run-now | Manual acceptance supports caller/task-scoped `Idempotency-Key` and a reconciliation GET. A stable opaque request index survives run-history pruning. Client callers share a pending intention and reconcile uncertain responses before retrying acceptance with the original key. | API lost-response and cross-caller tests; client lost-response, accepted-result lookup, simultaneous callers, unavailable reconciliation and fresh-intention tests; retention preserves deduplication. |
| UX-01 action feedback | Schedule and conversation mutations show per-item pending/error/success state. Failed mutations do not restore captured whole lists. Structured errors retain bounded references with common credential forms removed. Rename failure preserves drafts. Late run responses do not change a newer navigation choice. Queued and running states are separate in API/UI. | Hook lifecycle/concurrency tests, actual App handler tests for independent mutations and stale navigation, component tests for failed rename drafts, pending controls and explicit check/retry feedback. |
| Retention correction | Fixed an existing `timedelta.isoformat()` error in scheduled-history pruning. Temporary accepted input snapshots are discarded on terminal transitions; skipped runs do not duplicate prompts. Opaque request receipts retain no prompt/output content. | The real retention call failed before the cutoff fix; it now prunes old history while the original request still resolves without execution. Migration 0016 is exercised with populated predecessor-schema rows and repeated initialization. |

Obsolete cleared-claim and queued-wrapper dispatch paths were removed. Their helper-only tests were replaced by stronger normal-dispatch and atomic-acceptance tests. The React change review covered hook lifetime, independent row state, dependent reconciliation requests, accessible feedback and direct imports; complete keyboard/focus work remains in UX-02.

### Second-batch verification

| Invocation | Result |
| --- | --- |
| Full Python 3.11 backend suite | **1,097 passed, 8 skipped**, 63.13 seconds. |
| Python 3.12 isolated `-m 'runtime or integrations'` | **412 passed**, 693 deselected, 31.88 seconds after environment setup. The CI compatibility job now selects these groups too. |
| Frontend `npm run verify` | **118 component/App tests and 20 Electron tests passed**; TypeScript and production renderer build passed. |
| Populated migration and fifth-failure-with-queue checks added after the full run | **2 passed**. |
| Queue/lease/replacement checks after preventing repeated shutdown cancellation | **12 passed**. |
| Workflow YAML and whitespace checks | Passed. |

Docker's real-container check, installed Electron end-to-end behavior and actual Windows installer/upgrade validation remain outside these results. The deterministic browser-facing tests use jsdom. Client pending-intention caching survives backend reconnect within the current renderer, not a renderer reload; no request is automatically replayed on startup. See [scheduling.md](scheduling.md) for the final contracts and boundaries.

## Delivered in the third batch

| Work item | Behavior implemented | Evidence |
| --- | --- | --- |
| SEC-02 effective shell environment | One backend presentation feeds approval tickets/events, resumed notices, execution/session results, saved-settings diagnostics and settings UI. It distinguishes host/advisory execution from Docker, reports host and execution workspaces, shell, image, actual network/filesystem and write behavior, consent requirement, and selection reason. Host network deny and discard/copy-out are explicitly described as unenforced/inapplicable. | Policy-to-approval-to-result/event checks, three Docker write strategies, host/auto/advisory cases, and a real host session start/poll. UI cases cover streaming/resumed approvals, polled toast, expanded results and malformed result data. |
| SEC-02 consent freshness | Stored approval snapshots are compared with current classified environment before bypassing confirmation. Changed backend, workspace, shell, policy or Docker runtime settings re-enter the current permission gate. Final manager reclassification rejects material drift before dispatch. Full Access remains governed by current permissions; explicit Docker/enforce and scheduled restrictions remain fail-closed. | Six changed-environment approval scenarios and a container-setting change injected immediately before dispatch. Existing scope, unattended-run, blocked-command, backend mismatch and approval suites passed. |
| SEC-04 browser boundaries | Browser settings use “Navigation target domains” and state the exact guard scope. Documentation distinguishes explicit target checks from existing tabs, snapshots/screenshots, history navigation, clicks, popups and Chrome redirects; browser_fetch retains its separate redirect guard. Shell isolation is not presented as covering Chrome, desktop automation or external MCP. | Controlled adapter cases establish uninspected operations and returned redirect/popup destinations; existing denied-target and browser_fetch redirect tests pass. No live-Chrome origin containment is claimed. |
| GitHub CI expansion | All seven backend groups remain required on Windows Python 3.11. Python 3.12 now selects runtime, integrations, sandbox and skills. The API job checks generated contracts. Frontend uses a reporting verification command on Node 20.19, 22.13 and 24. Required real-Docker tests remain on Linux. Jobs upload distinct JUnit reports after failures when produced; superseded runs are cancelled. | Full local checks, expanded isolated 3.12 selection, actual Windows CI wrapper/report generation, frontend CI command/report generation, YAML/group checks and generated API-doc check. GitHub-hosted jobs have not run for these local edits. |
| Node minimum correction | Package policy, lockfile, setup/launch helper and CI now require Node 22.13 for the 22.x line, accounting for locked jsdom 29.0.2 rather than only Vite's 22.12 floor. | Boundary and actual PowerShell helper tests pass. A local semver scan found every locked dependency engine compatible with the declared 20.19.0, 22.13.0 and 24.0.0 minima. The other Node versions still require the GitHub matrix run. |

### Third-batch verification

| Invocation | Result |
| --- | --- |
| Full Python 3.11 backend suite, JUnit enabled | **1,122 passed, 8 skipped**, 69.70 seconds. |
| Isolated Python 3.12 `-m 'runtime or integrations or sandbox or skills'`, JUnit enabled | **881 passed, 5 skipped**, 244 deselected, 57.66 seconds after environment setup. |
| Frontend `npm run verify:ci` | **123 UI/App tests and 20 Electron tests passed**; JUnit renderer report, TypeScript and production renderer build passed. |
| Actual Windows CI wrapper, API group and JUnit path | **73 passed, 2 skipped**, 1,055 deselected; report produced. |
| Electron/prerequisite checks after correcting Node minimum | **20 passed**. |
| Final blocked-environment metadata and approval viewport checks | **33 backend and 5 UI tests passed** after the last small changes. |
| Generated API docs, workflow YAML/group coverage and whitespace checks | Passed. |

The skipped real-Docker cases still require a working daemon; CI requires them instead of skipping. The new disclosure checks use controlled Docker adapters; no real-container success, installed Electron workflow, installer/upgrade pass or GitHub-hosted CI result is claimed. Browser boundary tests use a controlled DevTools adapter and jsdom UI, not a live personal browser.

The React review checked the shared presentation component, direct imports, typed/validated result data, existing hook lifetimes and native approval controls. Approval toasts now scroll within the viewport when disclosure details are long. Visual testing in an installed application remains pending.

## Delivered in the fourth batch

| Work item | Behavior implemented | Evidence |
| --- | --- | --- |
| QA-01 source-application foundation | Seven Windows workflows launch the actual Electron main/preload, built renderer and authenticated backend with a private home, workspace, database, Electron/legacy credential directories and Python environment. Controlled provider responses and a barrier tool retain real file execution, approval replay, scheduling and persistence. | Startup/streaming/reopening; approved append once; rejected append unchanged; completed sibling retained after Stop; active schedule disabled through completion/restart; lost Run-now response reconciled without a second acceptance; backend interruption/recovery without repeating the completed file action. All seven passed in one run. |
| Isolated launch and CSP ownership | Source launches can select a validated backend port and bundled renderer. CSP, CORS and IPC trust derive from the selected renderer mode; signed sessions and loopback host restrictions remain. Removed the conflicting fixed-port HTML CSP. | The application reaches its non-default authenticated backend and an actual CSP violation blocks a different loopback target. Renderer Node globals remain unavailable. Unit tests cover port validation and packaged/source renderer selection. |
| Application CI | A separate required Windows job prepares locked Python 3.11 and Node 22.13 dependencies, builds/type-checks the harness, runs all workflows without retries and uploads JUnit/failure artifacts. Vitest excludes application fixtures. | Local invocation of the same command passed; workflow parse, dependency engine minima and backend CI group coverage checks passed. GitHub-hosted execution is still pending publication. |
| Harness lifecycle and evidence | The harness returns from inspector evaluation before requesting native app quit, observes process exit and backend-port closure, and verifies its temporary-directory boundary before cleanup. Failure evidence is restricted to synthetic fixture state and bounded logs. | Deferred real quit resolved an inspector shutdown stall after backend interruption; the final seven-test run completed with no fixture Electron processes remaining. See the temporary cleanup exception below. |

### Fourth-batch verification

| Invocation | Result |
| --- | --- |
| Windows `npm run test:app` | **7 passed, 0 skipped, 0 failures**, 107.74 seconds; built renderer, harness type-check and JUnit passed. |
| Full Python 3.11 backend suite | **1,122 passed, 8 skipped**, 67.76 seconds. |
| Frontend `npm run verify:ci` | **123 UI/App tests and 22 Electron tests passed**; TypeScript, renderer build and renderer JUnit report passed. |
| Generated API docs, workflow/job configuration, dependency engine minima, whitespace checks | Passed. |
| Current Docker daemon probe | Unavailable: Docker Desktop Linux engine pipe not found. No real-container pass claimed. |

[Application testing](application-testing.md) records the exact command, fixture boundaries, per-workflow assertions, reports and remaining checks. This verifies the source application with a built renderer; it does not exercise an installer, packaged resources, upgrades, live providers, real Docker, personal Chrome/native desktop actions or MCP credential rotation/schema changes. The current harness substitutes the provider adapter, not the HTTP SDK wire protocol. Stage A still lacks the outstanding real-Docker evidence; the overall plan remains open.

One directory from an earlier failed teardown remains at `C:\Users\KL\AppData\Local\Temp\monaw-e2e-WW686u`. Automatic approval review rejected the separate cleanup command with only “blocked by policy”; the directory was left in place. The final successful harness run cleaned its own fixtures and left no test Electron process running.

## First-batch verification (historical)

Environment: Windows x64; application Python **3.11.15**; isolated compatibility Python **3.12.11**; Node **22.18.0**; npm **11.7.0**; dependencies resolved from the existing lockfiles. The compatibility run used `--isolated` to preserve the application's existing virtual environment.

| Invocation | Result |
| --- | --- |
| `uv run --locked pytest -q --tb=short` in `backend` | **1,081 passed, 8 skipped**, 59.22 seconds. |
| `uv run --isolated --locked --python 3.12 pytest -q -m runtime --tb=short` in `backend` | **315 passed**, 774 deselected, 41.60 seconds after environment setup. |
| `npm --prefix frontend run verify` | **107 component tests and 20 Electron tests passed**; TypeScript and renderer production build passed. |
| Required Docker integration mode, two integration tests | **Expected prerequisite failure:** daemon/pinned image unavailable. No real-container pass is claimed. |
| Workflow YAML, PowerShell syntax, `git diff --check` | Passed. |

The original CI logs were inspected through GitHub CLI. The exact runtime tests initially passed locally before additional deterministic boundary regressions were added; that initial local pass alone was not treated as a diagnosis. The isolated Python 3.12 runtime pass now includes those regression cases. A new GitHub Actions run, actual Node 20/24 execution, and the real Linux Docker job have not been run on these local changes.

Docker Desktop's daemon was initially unavailable. A startup attempt timed out after 30 seconds and the subsequent daemon probe still failed. The historical Docker failure therefore remains open; the improved diagnostic assertion should expose its result on the next real integration run. Network/root-filesystem isolation has not been weakened.

## Persistence and recovery

Migration **0015** adds `config_revision` columns with default zero. Existing task/run records retain their identifiers and historical status representation. It runs through the existing numbered migration runner; repeated initialization is tested. Application startup will apply it to the selected data directory. No manual migration of the user's live database was requested or performed.

Migration **0016** adds persisted occurrence input/ownership metadata and the opaque manual-request index. Existing task/run results are preserved. Pending snapshots are retained only until terminal transition; request receipts remain after ordinary run-history pruning and are deleted with their task. Older binaries do not dispatch this persisted queue or maintain its request guarantees. No safe downgrade is claimed.

Stop the application and create a consistent SQLite backup, accounting for WAL, before applying the eventual release to important data. Downgrade/data-restore behavior has not been validated as a release workflow. The revision columns are additive, but older binaries do not maintain the new concurrency contract; do not treat that as a verified safe downgrade.

## Next implementation boundaries

1. Focused runtime/settings extractions and keyboard/focus/first-run improvements. SEC-03 credential storage and the controlled MCP lifecycle workflow are delivered below; third-party interoperability and cross-user credential restore remain separate checks.
2. Windows backend packaging/installer, packaged/installed application and upgrade checks, diagnostic export and measured baselines.
3. Expand the application harness for packaged candidates; run real Docker and GitHub-hosted CI and collect release evidence.

Stage A and the overall reliability release are not complete. No score reassessment is claimed yet.

## Delivered in the fifth batch

The checkout already contained uncommitted browser-extension work at the start of this session. It was preserved. The results below validate the combined checkout; this batch's implementation concerns custom MCP credentials and the application workflow.

| Work item | Behavior implemented | Evidence |
| --- | --- | --- |
| SEC-03 identity and storage | Stable server identities; references bound to server/transport/field/key; MCP-only OS-encrypted vault namespace; no generic renderer key selection or stored-secret read API. Ordinary settings preserve omitted references and hidden legacy credentials, reject masked inputs/new obvious secret fields, and reject reference rebinding and case-insensitive field collisions. | Model/API tests; two-server vault isolation and rotation tests; actual Windows encryption and decrypted restart in the application workflow. |
| SEC-03 migration | Explicit per-field selection, including arbitrary environment names. Encrypt and verify the stored record before atomic plaintext removal. Encrypted prepared records retain progress; version/digest checks reject changed legacy input; an interrupted or lost commit response reconciles safely. Unselected legacy fields remain marked plaintext. | Encryption/storage/verification failures, interruption before commit, response loss after commit, changed binding/version/digest, atomic replace failure, and unrelated whole-settings save tests. No live developer settings were migrated. |
| SEC-03 hydration and revocation | Dedicated main-process-only scope, absent from renderer/development tokens. Connection-local values, filtered child environment, unavailable state before hydration, affected-manager retirement/reconnect on rotation, explicit revocation, server-removal pruning, and stale callback fencing. Headless launchers use an explicit authenticated runtime snapshot with no persistence fallback. | Scope-denial/no-read tests, missing-hydration/no-launch, two-server separation, HTTP local-header injection, rotation/teardown, removed-server callbacks, real local subprocess transport, and restart workflow. |
| SEC-03 disclosure and redaction | Separate immediate credential controls and saved configuration drafts; per-field migration choices and generic retry-safe feedback. Known current/previous credentials redact diagnostics and observability. Credential-bearing stderr uses bounded memory instead of a raw temporary file. | Component lifecycle/pending/error tests, real subprocess stderr sentinel test, application settings/store/diagnostic/backend-log checks. Ordinary nonsecret configuration remains plaintext by design. |
| QA-01 MCP lifecycle and CI | Eighth workflow uses real Windows OS encryption and real local MCP SDK transport. Approval invokes discovered tools, rotation takes effect, actual restart rehydrates, reconnect discovers/invokes a changed schema, deletion prevents a new subprocess. Reconnect invalidates old mappings and cached tool registration. Existing Windows application job runs the expanded suite; new backend tests belong to the required API group and Electron tests use the existing glob. | Focused application workflow passed. Final combined verification is recorded below. Third-party MCP services, cross-user restore and packaged installation remain outside this evidence. |

See [MCP credential storage and recovery](mcp-credentials.md) and [application testing](application-testing.md) for the exact behavior and limitations. Older binaries cannot hydrate the new references; copying the encrypted vault across Windows users/machines is not a verified recovery path.

### Fifth-batch verification

| Invocation | Result |
| --- | --- |
| Full locked Python 3.11 backend suite with JUnit | **1,187 passed, 8 skipped**, 95.96 seconds. |
| Isolated locked Python 3.12, `api or runtime or integrations or sandbox or skills`, with JUnit | **1,019 passed, 7 skipped, 169 deselected**, 104.76 seconds. This local selection includes the new API credential tests beyond the existing compatibility-job selection. |
| Frontend `npm run verify:ci` | **130 UI/App tests and 32 Electron tests passed**; TypeScript, renderer build and JUnit passed. |
| Windows `npm run test:app` | **8 passed, 0 failures**, approximately 2.3 minutes; renderer build, harness type-check and JUnit passed. Includes the actual OS-encrypted local MCP lifecycle. |
| Generated API documentation, workflow parse/application job command, whitespace | Passed. New backend tests are classified in the required API group. |

The final application run cleaned its own fixtures and left no harness Electron process running. The previously recorded cleanup exception is historical; it was not retried by this session. Real Docker, GitHub-hosted CI, installer/upgrade, live-provider/third-party MCP interoperability and cross-user credential restore remain unverified. No additional release score is claimed.

### Clipboard follow-up, 25 October

The extension-folder Copy handler used the browser clipboard API, which the desktop permission policy denies, and silently swallowed the error. Copy now uses a bounded, sender-validated, write-only Electron IPC bridge in the desktop app; browser development retains the browser API. Failures show an accessible message with manual keyboard-copy instructions. Clipboard reads are not exposed to the renderer. A desktop restart is needed to load the new main/preload bridge.

Three regression cases cover desktop, browser, and failed copying, including spaces and non-ASCII paths. The desktop and failure-feedback cases failed before the fix; all **29 settings component tests** now pass. **32 Electron tests**, renderer/harness TypeScript, and the renderer build pass. A focused real Electron application workflow passes (**1 passed**, 14.0 seconds), asserting the actual OS clipboard after clicking Copy through real authenticated setup. Only setup's external Explorer/Chrome launches are suppressed in the disposable fixture; Chrome extension installation is not tested or claimed. The existing application CI command now discovers nine workflows. The earlier eight-workflow/backend results above remain historical and were not rerun for this clipboard-only change.

## ARC-01, first extraction: tool-batch ownership

Updated: 30 October 2026. Local and uncommitted.

| Work item | Behavior implemented | Evidence |
| --- | --- | --- |
| ARC-01 tool-batch ownership | `harness/tool_batch.py` owns the turn's started calls and their invocation tasks: registration with a stopped placeholder, result mirroring, ordered parallel/serial batch execution, escaped-error isolation, cancel/drain, cancellation marking and the persistable snapshot. The turn loop keeps event emission, observability, gates, review and result processing, and passes them in as callbacks. Exception handlers call the ledger directly instead of looking up nested helpers through `locals()`. Ledger keys use a per-turn sequence; the previous `call_id:len(active)` key could repeat for a reused call ID after an earlier call finished. | Seven contract tests cover placeholder persistence, distinct keys, pending gates not counting as completion, model-order results, escaped-error isolation, a completed sibling retained across cancellation with one observability report per call, and serial-barrier draining. The existing turn-loop sibling, gate and cancellation tests pass unchanged. |

Public event fields, tool identities, permissions, attachment scope and persisted tool-call shape are unchanged. Run finalization was not extracted in this change; settings sections remain the next ARC-01 step.

### Verification

| Invocation | Result |
| --- | --- |
| Full locked Python 3.11 backend suite before the change | **1,159 passed, 8 skipped**, 70.88 seconds. |
| Full locked Python 3.11 backend suite after the change | **1,166 passed, 8 skipped**, 69.32 seconds. |
| `scripts/check-imports.py` | 234 modules compile and import; `/health` served. |
| `git diff --check` | Passed. |

Frontend, application workflows, Python 3.12, real Docker and GitHub-hosted CI were not run for this backend-only change.

## ARC-01, second extraction: settings draft ownership and sections

Updated: 30 October 2026.

| Work item | Behavior implemented | Evidence |
| --- | --- | --- |
| Draft-versus-saved ownership | `useSettingsDraft` owns the draft, the last saved copy it is compared with, edited connection keys and the save lifecycle. The previous JSON-signature baseline, which an MCP credential refresh patched by parsing it, is replaced by a saved settings object. Telegram allowlists live in the draft rather than separate state. Immediate MCP credential operations still reconcile only their metadata into both copies. | Six hook tests: dirty tracking, trimmed allowlists, browser key payload limited to edited keys, failed save, key failure after a successful settings save and its retry, credential reconciliation preserving unrelated edits. |
| Save failure handling | A rejected settings save previously escaped the click handler and showed nothing. It now keeps the draft and reports the reason in the footer as an alert. When settings save but writing edited Electron keys fails, the saved settings become the baseline immediately, so a retry carries the new settings version instead of being rejected as stale; only the keys stay unsaved. | Shell test of a rejected save (alert, edit kept, close asks to discard); hook test of the key-failure retry. |
| Sandbox section | `SandboxSettings` renders the sandbox page from the draft's sandbox slice and the saved-settings status, and owns Docker image resolution state. | Four component tests: slice-only edits, enforce-without-Docker warning, resolved image pinned into the draft with status reported, failed resolution keeps the typed image. Shell test saves a sandbox edit. |
| MCP section | `McpSettings` renders the MCP tab, server cards, list/key-value editors and diagnostics from the draft's MCP slice. It owns template, advanced-panel and reconnect state; live diagnostics stay in the shell because saves and refreshes reload them. Credential controls stay disabled while the draft has unsaved changes or a save is running. | Two component tests: add/edit/remove with duplicate-name feedback, reconnect reporting the server diagnostic. The existing Tools/MCP shell test and MCP credential tests pass unchanged. |

`SettingsModal.tsx` went from 2,604 to about 1,630 lines. The settings request payload, settings version checks, credential IPC and visible copy (apart from the two new save-failure messages) are unchanged. Remaining large pages (connections, browser, model, identity) can follow the same pattern; UX-02 focus work builds on this shell.

### Verification

| Invocation | Result |
| --- | --- |
| Frontend `npm run verify` before the change | **132 UI/App tests and 32 Electron tests passed**; TypeScript and renderer build passed. |
| Frontend `npm run verify` after the change | **146 UI/App tests and 32 Electron tests passed**; TypeScript and renderer build passed. |
| `tsc --noUnusedLocals` on the changed settings files | No unused locals. |
| Windows `npm run test:app` | **8 passed, 1 failed**, 3.2 minutes. The failure is the clipboard workflow: it clicks a "Monaw extension" button that `cc9e109` replaced with a status cell, so it fails before reaching settings code changed here. Fixed separately. A first attempt from the sandboxed Git Bash shell could not start any backend because `uv` was denied its cache directory; the run above used PowerShell. |
| `git diff --check` | Passed. |

## ARC-01, third extraction: run finalization

Updated: 31 October 2026.

The turn loop ends a turn in four places: a pause because an earlier tool is still stopping, normal completion, user cancellation and an internal error. Each persisted the turn, closed the observed run and (except cancellation) emitted `done`, and three of them repeated the tool-error count. `harness/run_outcome.py` now provides the turn status, the `done` payload and observed-run closing used by all four. The payloads and observability arguments are unchanged field for field: the early pause still reports no tool counts, and only normal completion includes attachments, duration and incomplete markers. Persistence and run-state transitions stay in the turn loop because they depend on its state. This completes the ARC-01 runtime extractions the plan called for; provider adaptation and context budgets were deliberately left alone.

| Invocation | Result |
| --- | --- |
| Run-outcome contract tests | **5 passed**: error counting, pause conditions, payload contents per exit, incomplete reasons, tool counts reported only when known. |
| Full locked Python 3.11 backend suite | **1,171 passed, 8 skipped**, 65.04 seconds. |
| `scripts/check-imports.py` | 236 modules compile and import; `/health` served. |

## UX-02, first part: keyboard use, dialogs and confirmations

Updated: 31 October 2026.

| Work item | Behavior implemented | Evidence |
| --- | --- | --- |
| Shared dialog behavior | `useModalFocus` gives every modal (settings, scheduled task, run history, image preview) initial focus, Tab and Shift+Tab kept inside the dialog, Escape routed to the dialog's own close logic, and focus returned to the opener. Only the topmost dialog handles keys, decided by containment so nested dialogs that mount together behave. Portal menus keep their own Tab and Escape. Controls inside a dialog that use Escape themselves (the timezone search) mark it handled, so the dialog no longer also closes. Run history now closes on Escape. | Five hook tests: initial focus and return, Tab wrapping past disabled controls, stray focus brought back, handled Escape ignored, innermost dialog only. |
| Unsaved-change prompts | Settings and the scheduled-task dialog share `useDiscardPrompt`: asking moves focus to Keep editing, Escape at the prompt keeps editing, and focus returns to where the user was. The scheduled-task dialog previously closed on Escape or Cancel and discarded edits; it now asks first. | Settings and scheduled-task dialog tests drive both prompts from the keyboard. |
| Dropdown | Arrow keys open the menu on the selected option; Up/Down wrap, Home/End jump; choosing, Escape or Tab close it and return focus to the trigger. Escape closes only the menu, not an enclosing dialog. | Five Dropdown tests. |
| History and irreversible deletes | A conversation row is a real button (`aria-current` for the open one) and F2 renames it; Enter or Escape in the rename box returns focus to the row. Deleting a conversation or a scheduled task asks first with an inline confirmation that starts on Cancel; Escape cancels and returns focus to the delete control. Previously both deleted on one click. The scheduled-task actions menu now uses menu semantics, arrow/Home/End movement, Escape/Tab/outside-click closing and focus return. | Five keyboard tests for the sidebar and task menu; the existing action-feedback test now queries menu items by their role. |
| Visible focus and labels | Dropdown options, settings navigation, the context-usage button and the sidebar delete control show a focus indicator. The scheduled section toggle reports `aria-expanded`. A scan of icon-containing buttons found none without an accessible name. | CSS and class changes; covered visually only by the component tests' focus assertions. |

The Electron workflows that open the task actions menu now select its items as menu items.

| Invocation | Result |
| --- | --- |
| Frontend `npm run verify` | **166 UI/App tests and 32 Electron tests passed**; TypeScript and renderer build passed. |
| Windows `npm run test:app` (PowerShell) | **9 passed**, 2.4 minutes, including the corrected clipboard workflow. |

Not covered: a manual screen-reader pass and a keyboard-only walkthrough in the installed app. ObservabilityPanel's run-detail test failed once under full-suite load and passed in four isolated runs and the final full run; it is timing-sensitive.

## UX-02, second part: first-run guidance

Updated: 31 October 2026.

An empty new chat now shows a "Get started" guide instead of only "Ready." when the app is not ready for a first task, or before the first conversation exists.

| Work item | Behavior implemented | Evidence |
| --- | --- | --- |
| Required steps | Connect a model (checks the credential the selected provider needs: the OpenAI key, the Google key, or the signed-in OpenAI account, which is reported as checking until the account check returns), the working folder (default workspace or the chosen folder), and the current permission mode with a link to review it. | Readiness tests per provider, including that a Google key does not make the OpenAI provider ready. |
| First task | "Run example" sends a read-only prompt ("look at the files in my workspace folder ... do not change anything") and is disabled until a model is connected and while the backend is starting or a turn is running. | Guide component tests. |
| Optional capabilities | Listed under "Turned on" only once selected, with state from live checks: browser control from the extension connection; Docker only when the isolation mode requires a container (Automatic mode's opportunistic Docker is not listed), from the sandbox backend check; Telegram from the running bot and its startup error; MCP from connected and unhealthy server counts. Each links to the settings page that resolves it. | Readiness tests for every capability state. |
| Visibility | Always shown in an empty new chat while no model is connected; otherwise only until the first conversation exists, and it can be hidden once a model is connected (remembered per browser profile). Failed checks stay "checking" and never block the chat. | `shouldShowGuide` tests. |
| Settings deep links | Settings can open on a given page. The app bar's permission shortcut now opens Permissions instead of the Model page. | Exercised through the guide's settings links. |

| Invocation | Result |
| --- | --- |
| Frontend `npm run verify` | **179 UI/App tests and 32 Electron tests passed**; TypeScript and renderer build passed. |
| Windows `npm run test:app` (PowerShell) | **9 passed**, 2.5 minutes. |

Not checked: the guide's appearance in the running app and with a screen reader. UX-02 is complete apart from those manual passes.

## DIST-02: packaging proof and installer candidate

Updated: 31 October 2026.

| Work item | Behavior implemented | Evidence |
| --- | --- | --- |
| Frozen backend | `backend/monaw_backend.py` and `backend/packaging/monaw-backend.spec` build `monaw-backend.exe` with PyInstaller (new `packaging` dependency group). The executable also accepts `-m` and `-c` because the backend starts the built-in browser server and the detached Chrome launcher with `sys.executable`. The app package ships as plain files for disk-loaded skills, migrations and prompts. | With only the Windows folders on `PATH`: `/health` in 1.3–1.7 s; authenticated settings and diagnostics; all built-in skills loaded; browser server started through `-m` and connected with 20 tools; `-c` executed. |
| Packaged launch path | `electron/backend-launch.cjs` runs the frozen backend from `resources/backend` when present and otherwise keeps the uv launch; Electron skips looking for uv when frozen and points the backend at the bundled extension. | Three launch tests; the security test now checks the loopback host through the launch module. Packaged app without development tools on `PATH`: healthy backend in 4 s on first start, authenticated renderer calls, browser server running, no Monaw processes left after kill. |
| Installer | electron-builder NSIS target: per-user, choosable folder, user data kept on uninstall. `scripts/package-windows.ps1` builds both stages. | 220 MB installer built in 87 s; 755 MB installed (backend 381 MB). |
| User data outside the install | Prerequisite commit moved user-created skills to `~/.monaw/skills`, so nothing is written into the install folder. | See the skill commit and [packaging.md](packaging.md). |

The installer was built but not installed: installing writes Start-menu entries and an uninstall key to the current user profile, and the plan places install and upgrade checks on a clean Windows machine. Release blockers recorded in [packaging.md](packaging.md): the GPLv3 ffmpeg build bundled by `imageio-ffmpeg`, unsigned executables, and the missing third-party notice file. Clean install, upgrade, repeated launch, port conflict and missing-dependency checks remain for QA-01 expansion against an installed candidate.

The source-app workflows still pass with the new launch module (**9 passed**, 2.5 minutes), and Electron unit tests pass (**35**).

## QA-01 expansion, OBS-01 and DOC-01

Updated: 31 October 2026.

| Work item | Behavior implemented | Evidence |
| --- | --- | --- |
| Packaged-app checks | `npm run test:packaged` drives the packaged app with only the Windows folders on `PATH`, from a Monaw home whose path has spaces and non-ASCII characters: first run and guide, settings across restart, second launch, occupied port, backend that stops while starting. The source workflows cannot run frozen: they substitute the model provider through the Python path, and the packaged build has no test hooks. | Five packaged checks; results for the candidate below. |
| Startup robustness found by those checks | A second launch used to start a second backend on the same port with a different control secret; Monaw now holds a single-instance lock and focuses the running window. An occupied port or a backend exiting before it was ready left the window waiting; both now show an error naming the port or the log, are recorded in `backend.log`, and never stop the other program. | Electron unit tests for port detection and messages; the packaged checks. Source workflows still pass (**9 passed**, 2.6 minutes). |
| Settings save with stored keys | While testing, saving settings in an isolated profile surfaced a credential re-apply failure; the new save handling reported it instead of failing silently. Its cause was the test profile lacking Windows known folders, fixed in the harness. | Packaged settings check. |
| Support report (OBS-01) | Settings > Observability > Support report prepares versions, packaging, service status, a configuration shape without values, and up to 50 recent errors. It is redacted with the existing observability redaction, bounded to 512 KB (oldest log lines, then oldest errors dropped first), shown in full before it can be saved, and includes the backend log only in support mode. | Five backend tests (no setting values, redaction of keys and paths, log only in support mode, size bound keeping newest entries) and two UI tests. |
| Measured baselines (OBS-01) | `scripts/measure-baseline.ps1` measures the packaged app's cold and warm start to a healthy backend, idle API requests per minute from the access log, and fresh database size, and records the commit and Electron version. Long-conversation rendering, representative database growth and stop-to-quiescence are recorded as not measured rather than estimated. | Candidate figures below. |
| Release documentation (DOC-01) | [Installation](installation.md) covers requirements, checksum verification, install, data location, manual upgrade with backup, uninstall and startup failures; [packaging](packaging.md) covers building, layout, measurements, licensing and what is unverified; operations and the README link both and list the user skills folder. | Link check over the changed documents. |

### Candidate `fdf05ae`

The candidate was built from commit `fdf05aea5f7d592b150f7e5901e02e390a811561` with `scripts/package-windows.ps1` (154 seconds for backend, app and installer). Documentation edits made while it built do not change the app.

| Invocation | Result |
| --- | --- |
| Installer | `Monaw-Setup-1.0.0-x64.exe`, 220.4 MB, SHA-256 `F7FB6CC5922A479DF52D25E0D4E7675E646915F8B8B3A6B01622D12656D81C7B`. Unsigned; not installed on this machine. |
| `npm run test:packaged` | **5 passed**, 24.8 seconds. |
| `scripts/measure-baseline.ps1 -SkipTests` | Cold start to healthy backend **1.75 s**, warm **1.61 s**; **0** idle API requests per minute over 60 seconds; fresh database **4,096 bytes**. Windows 11 (10.0.26300), 16 logical processors, Python 3.11.15, Node 22.23.4, Electron 41.3.0. Test timings were skipped because the suites had just run. |
| Full backend suite | **1,178 passed, 8 skipped**, 65.76 seconds. |
| Frontend `npm run verify` | **181 UI/App tests and 37 Electron tests passed**; TypeScript and renderer build passed. |
| Source `npm run test:app` | **9 passed**, 2.6 minutes. |

During startup the renderer's stored-key sync can reach the backend before it listens and logs `ECONNREFUSED`; it retries and succeeds, as before this work.

## Real Docker run, 31 October

Docker Desktop was running (engine 29.5.2, Linux containers), so the three steps of CI's Linux Docker job were run locally on Windows with Python 3.11.15. GitHub-hosted CI still has not run.

| Step | Result |
| --- | --- |
| Pull the pinned image `python:3.12-slim@sha256:2c941e86…3c4a` | Pulled. |
| `MONAW_REQUIRE_DOCKER_TESTS=1` pytest over `test_sandbox_docker.py` and `test_sandbox_image_inventory.py` | **26 passed**, none skipped. The real simple-command and network/root-isolation tests pass, so the historical Docker failure from the first batch is resolved. |
| Inventory check against the offline module table | Failed first: the table listed `nis`, which the image cannot import (`ModuleNotFoundError`). `nis` was removed from the table and its test; the check now finds 204 importable modules and nothing claimed that is absent. `test_sandbox.py` (**145 passed**) and the Docker suites pass again. |

No containers from the run were left behind.

## First GitHub-hosted CI run, 31 October

Pushing `9c5285f` ran CI on GitHub ([run 41170820654](https://github.com/kailiang0120/monaw/actions/runs/41170820654)). Eleven of fourteen jobs passed. The three failures had also failed on 25 October; each is now reproduced and fixed locally:

| Job | Cause | Fix and local evidence |
| --- | --- | --- |
| Docker sandbox (Linux) | The container ran as root with all capabilities dropped, so it could not use a mounted folder owned by another host user (pytest's `0700` temp folder; a normal `755` project folder could not be written either). Docker Desktop on Windows does not enforce host ownership, so it passed here. | Mounted runs on POSIX hosts use the host's `uid:gid`. Reproduced in the Docker Desktop Linux VM: the current flags give `Permission denied`; with `--user` the read and write succeed. 204 sandbox tests pass with real Docker required. |
| Frontend (Node 20.19.0) | All 181 Vitest tests passed, then `node --test electron/*.test.cjs` failed with "Could not find": nothing expands the glob on Windows, and Node 20 does not expand it itself. | `scripts/run-electron-tests.cjs` lists the files; all 37 Electron tests run. Not yet run on Node 20 locally. |
| Electron and backend workflows (Windows) | Playwright forces colour in its workers, so `uv cache dir` and `uv python find` returned paths wrapped in ANSI escape codes. The backend's uv rejected that cache path. | The harness calls uv with `--color never`. With `FORCE_COLOR=1` one workflow failed the same way before the fix; after it, all **9 passed** (2.9 minutes). |

With these fixes, [run 41172096702](https://github.com/kailiang0120/monaw/actions/runs/41172096702) on `bdbd993` passed all **14 jobs**, including the real Linux Docker job, Node 20.19.0 and the Windows application workflows.

## Status of the plan

All fifteen packages have an implementation. What is still open needs resources or decisions outside this machine:

- **Stage A:** complete. The real Docker checks pass locally and on GitHub, and GitHub-hosted CI passes every job on `bdbd993`.
- **Stage B:** a manual keyboard-only and screen-reader walkthrough of the installed app.
- **Stage C:** a clean Windows install and an upgrade between two installed versions; code signing; third-party notices; and a decision on the GPLv3 ffmpeg build bundled by `imageio-ffmpeg`. Long-conversation rendering, representative database growth and stop-to-quiescence baselines need a packaged fixture and are not measured.

No rating reassessment is claimed.
