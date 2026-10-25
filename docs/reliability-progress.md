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
