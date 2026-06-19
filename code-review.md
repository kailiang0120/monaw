# Monaw Code Review and Implementation Plan

## Purpose

This document converts the repository review into an implementation roadmap.
The priorities are:

1. Protect privileged local-control boundaries.
2. Make sandbox and approval behavior fail closed.
3. Reduce coupling in the agent runtime and capability modules.
4. Improve background-task reliability and data lifecycle controls.
5. Remove avoidable frontend polling and render work.

The review covered the backend API, agent runtime, sandbox and policy layers,
skills, persistence, scheduler, Telegram integration, Electron shell, frontend,
tests, startup scripts, and operational documentation.

No code changes described below should be combined into one large rewrite.
Implement the work as small pull requests with explicit security and behavioral
tests.

## Priority Summary

| Priority | Workstream | Main risk addressed |
| --- | --- | --- |
| P0 | Authenticate the local API and restrict CORS | Any local web page or process can reach privileged endpoints |
| P0 | Remove renderer access to plaintext secrets | Renderer compromise exposes provider and integration credentials |
| P0 | Make sandbox fallback fail closed | Untrusted commands can degrade to host execution |
| P0 | Gate diagnostics, observability, approvals, and grants | Sensitive state and privileged decisions are remotely actionable |
| P1 | Disable runtime self-modification by default | The agent can create or modify executable skills |
| P1 | Replace MCP risk inference with explicit trust policy | Tool names and self-declared annotations decide approval behavior |
| P1 | Add source-aware policy for scheduled and Telegram runs | Alternate execution channels inherit interactive-user authority |
| P1 | Scope attachment access and remove path leakage | File identifiers and prompts expose durable absolute paths |
| P1 | Persist scheduler execution state | Restarts can lose in-flight and queued execution semantics |
| P2 | Split agent and capability monoliths | Large modules mix policy, orchestration, I/O, and presentation |
| P2 | Add database and observability lifecycle management | Runtime data can grow indefinitely and retain sensitive content |
| P2 | Replace frontend polling with event-driven invalidation | Repeated requests and render work scale with idle application time |
| P3 | Align scripts and documentation with actual guarantees | Operators can mistake compatibility behavior for isolation |

## Implementation Rules

- Treat the backend API as a privileged control plane even when it binds only to
  loopback.
- Authorization must be enforced in the backend. Hiding controls in the UI is
  not a security boundary.
- Approval, access-grant, sandbox, and source-policy decisions must be computed
  from structured data and recorded in the audit trail.
- A missing security dependency or unavailable strong sandbox must deny the
  operation when the requested operation requires that protection.
- Avoid introducing generic Electron IPC methods. Expose narrow, typed
  operations and validate the sender for every privileged handler.
- Add regression tests in the same pull request as each behavior change.
- Preserve existing user data through explicit migrations. Do not silently
  discard settings, schedules, approvals, memories, or credentials.

## Phase 0: Baseline and Safety Net

Status: implemented and locally verified on June 19, 2026.

Initial local baseline:

- Backend: 507 tests passed in 53.931 seconds.
- Frontend: 42 tests passed in 9.799 seconds.
- TypeScript: type checking passed in 4.974 seconds.
- Renderer: production build completed in 2.2 seconds.
- Renderer output: 1,772,401 bytes before compression.

Startup time, idle request rate, and representative database growth remain
pending until provider-independent application harnesses are added.

### Goal

Establish a reliable test and measurement baseline before changing trust
boundaries.

### Changes

1. Split the backend test suite into named groups:
   `api`, `runtime`, `policy`, `sandbox`, `skills`, `memory`, and
   `integrations`.
2. Add a test command with deterministic timeouts and per-test timing output.
3. Add frontend unit-test, type-check, and production-build commands to the
   root development workflow.
4. Record baseline values for:
   - backend test duration;
   - frontend bundle size;
   - application startup time;
   - idle API request rate;
   - database size after a representative conversation;
   - memory and observability retention growth.
5. Add a CI job that fails on test timeout instead of allowing an indefinitely
   stalled suite.

### Affected areas

- `backend/app/agent/tests/`
- `frontend/src/**/*.test.ts`
- `frontend/src/**/*.test.tsx`
- root and frontend package scripts
- development documentation

### Definition of done

- Every test group can run independently.
- The full suite completes within a documented timeout.
- CI runs backend tests, frontend tests, type checking, and a production build.
- Baseline performance results are recorded in the pull request.

## Phase 1: Secure the Local Control Plane

Status: implemented on June 19, 2026. Authentication, scoped authorization,
exact CORS defaults, loopback enforcement in Electron, request-size limits,
request IDs, and authenticated frontend requests are covered by regression
tests.

Verification note: Phase 1 API, Electron, and frontend tests pass. The complete
backend suite currently has one unrelated environment-dependent failure in
`test_computer_functions_act_approval_payload_contains_batch`: persisted local
settings disable screen fallback, so the test blocks before its mocked approval
path. The production policy was not weakened to make that test pass.

### Finding

The FastAPI application exposes settings mutation, approvals, access grants,
diagnostics, observability, file access, scheduled tasks, and agent execution
without an authentication dependency. CORS accepts local web origins and
credentials while allowing all methods and headers.

Relevant code:

- `backend/app/main.py:79`
- `backend/app/config.py:36`
- `backend/app/api/routes/settings.py:102`
- `backend/app/api/routes/approvals.py:15`
- `backend/app/api/routes/access_grants.py:13`
- `backend/app/api/routes/diagnostics.py:32`
- `backend/app/api/routes/observability.py:12`

### Required design

Use a random per-install control-plane secret and short-lived application
sessions:

1. The Electron main process owns the long-lived secret.
2. The backend receives the secret through a protected startup channel.
3. The renderer receives only a short-lived session token scoped to the current
   application instance.
4. Every `/api` route requires authentication by default.
5. Sensitive route groups require additional scopes:
   - `agent:run`
   - `settings:read`
   - `settings:write`
   - `approval:resolve`
   - `diagnostics:read`
   - `diagnostics:control`
   - `files:read`
6. The unauthenticated surface is limited to a minimal health endpoint that
   exposes no paths, settings, or version-sensitive diagnostics.

### Backend changes

1. Add an authentication module containing:
   - token parsing;
   - constant-time secret comparison;
   - session expiry;
   - scope validation;
   - uniform `401` and `403` responses;
   - rate limiting for failed authentication.
2. Attach authentication at the `/api` router level.
3. Add explicit scope dependencies to settings mutation, approval resolution,
   access-grant resolution, diagnostics controls, replay, debug-bundle export,
   scheduled-task mutation, and file endpoints.
4. Bind to `127.0.0.1` by default and reject non-loopback host configuration
   unless an explicit unsafe-development flag is enabled.
5. Replace permissive CORS defaults with the exact production renderer origin.
   Development origins must be opt-in and port-specific.
6. Set `allow_credentials=False` unless cookie authentication is intentionally
   introduced. Do not combine wildcard methods and headers with broad origins.
7. Add request IDs and structured security audit events for authentication and
   authorization failures.
8. Add maximum request-body sizes for chat, uploads, settings, and debug
   operations.

### Electron and frontend changes

1. Generate or load the control-plane secret in Electron main.
2. Validate the IPC sender using the existing trusted-renderer checks before
   issuing a renderer session.
3. Replace direct unauthenticated fetches with one API client that injects the
   session token and handles `401` by requesting a new session.
4. Do not persist short-lived session tokens in local storage, session storage,
   or logs.
5. Ensure browser-only development mode requires an explicit developer token.

### Tests

- Every privileged endpoint rejects a missing token.
- An expired token is rejected.
- A token with `settings:read` cannot mutate settings or resolve approvals.
- Requests from unapproved CORS origins are rejected.
- Health checks do not reveal runtime paths or configuration.
- Electron IPC rejects calls from an untrusted frame or web contents.
- Authentication values are absent from logs and observability exports.

### Definition of done

- No privileged API route is reachable anonymously.
- CORS is an exact allowlist.
- The desktop application can recover from backend restart without persisting a
  renderer-readable long-lived secret.

## Phase 2: Protect Provider and Integration Secrets

Status: implemented on June 19, 2026. Provider keys and the Telegram bot token
use OS-backed Electron encryption, legacy plaintext values are migrated with
round-trip verification, generic secret-reading IPC is removed, and renderer
code receives boolean credential status only.

### Finding

Provider API keys and the Telegram token are stored through `electron-store`
and can be read by the renderer through generic key-value IPC.

Relevant code:

- `frontend/electron/main.js:474`
- `frontend/electron/main.js:483`
- `frontend/electron/preload.js:6`
- `frontend/src/lib/apiKeySync.ts:13`

### Required changes

1. Store secrets using Electron `safeStorage` or a maintained OS credential
   vault integration.
2. Keep decryption and plaintext values in Electron main only.
3. Replace `storeGet`, `storeSet`, and `storeDelete` for secret keys with named
   operations:
   - `credentialStatus`
   - `setCredential`
   - `deleteCredential`
   - `applyCredentialsToBackend`
4. Return only presence and masked metadata to the renderer.
5. Send credentials to the backend over the authenticated loopback channel
   without returning their values to frontend JavaScript.
6. Redact credential-shaped fields before settings responses are serialized.
   Use separate input and output schemas so secrets cannot be returned by
   accident.
7. Migrate legacy plaintext values:
   - read once in Electron main;
   - encrypt into the new store;
   - verify the encrypted value can be decrypted;
   - remove the legacy value;
   - record only migration status, never the secret.
8. Clear decrypted buffers and references as soon as practical.

### Tests

- Renderer APIs never return plaintext credentials.
- Legacy migration preserves each configured provider.
- Failed migration leaves the original value intact and reports a recoverable
  error.
- Settings and diagnostics responses contain only masked secret state.
- Logs, debug bundles, approval payloads, and exception messages redact secret
  values.

### Definition of done

- A renderer-side script cannot retrieve stored API keys or bot tokens through
  the preload API.
- Plaintext secrets are not stored on disk by Monaw.

## Phase 3: Fail-Closed Sandbox Enforcement

### Finding

`auto` mode can select `local_direct`, and disabled modes explicitly run on the
host. `local_restricted` is advisory and does not provide filesystem or network
isolation.

Relevant code:

- `backend/app/agent/sandbox/policy.py:91`
- `backend/app/agent/sandbox/policy.py:187`
- `backend/app/agent/sandbox/manager.py:44`
- `backend/app/agent/sandbox/manager.py:80`
- `docs/sandboxing.md`

### Required design

Separate compatibility execution from sandbox execution:

- `enforce`: strong isolation is required; unavailable isolation blocks.
- `auto`: untrusted commands require strong isolation; trusted host-required
  commands may use an explicitly approved host runner.
- `host`: explicit unsandboxed host execution with approval and a visible
  warning.
- `off`: shell execution is disabled, not silently redirected to host
  execution.

Do not label `local_restricted` as a sandbox. Treat it as a host runner with
resource controls.

### Required changes

1. Replace backend selection based only on mode and availability with a policy
   result containing:
   - command trust class;
   - required isolation strength;
   - selected backend;
   - network policy;
   - filesystem policy;
   - reason code;
   - whether explicit user approval is required.
2. Block untrusted commands when Docker or another strong backend is
   unavailable.
3. Require a separate host-execution approval for any transition from an
   isolated plan to `local_direct` or `local_restricted`.
4. Remove implicit compatibility fallback from `auto`.
5. Add startup capability diagnostics that clearly distinguish:
   `strong`, `advisory`, and `none`.
6. Enforce network and filesystem claims in the backend. A policy result must
   never claim a restriction that the selected runner cannot enforce.
7. Pin Docker image digests and validate configured images.
8. Limit process count, memory, CPU, output bytes, execution time, artifact
   count, and artifact size for every backend.
9. Maintain a minimal environment allowlist and continue rejecting
   secret-looking explicit environment variables.
10. Record selected backend and effective restrictions in audit events.

### Tests

- Untrusted commands never execute with `local_direct`.
- Missing Docker in `enforce` and untrusted `auto` modes returns
  `sandbox_backend_unavailable`.
- A host-required command needs explicit host-execution approval.
- Network-denied Docker commands cannot reach external or host-loopback
  services.
- Filesystem restrictions are verified with real integration tests.
- Timeout and output limits terminate process trees and cap stored output.

### Definition of done

- No unavailable isolation requirement silently degrades to host execution.
- UI labels and documentation accurately describe enforced guarantees.

## Phase 4: Lock Down Administrative and Diagnostic APIs

### Finding

Diagnostics and observability expose runtime paths, MCP process details,
backend logs, run contents, replay controls, and debug exports. Approval and
access-grant endpoints can authorize high-impact actions.

Relevant code:

- `backend/app/api/routes/diagnostics.py:86`
- `backend/app/api/routes/diagnostics.py:180`
- `backend/app/api/routes/observability.py:34`
- `backend/app/api/routes/observability.py:47`
- `backend/app/api/routes/observability.py:52`
- `backend/app/api/routes/observability.py:125`
- `backend/app/agent/observability/recorder.py`

### Required changes

1. Split read-only summaries from privileged controls.
2. Require `diagnostics:control` for MCP reconnect, browser reset, replay, and
   debug-bundle export.
3. Bind approval and grant tickets to:
   - authenticated application session;
   - originating conversation;
   - originating execution source;
   - payload hash;
   - expiry time.
4. Revalidate the ticket and original payload immediately before execution.
5. Reject already consumed, expired, mismatched, or superseded tickets.
6. Remove absolute paths, process command lines, environment values, prompt
   bodies, and raw tool outputs from normal diagnostics.
7. Add an explicit, time-limited support mode for detailed debug exports.
8. Apply centralized recursive redaction before persistence and again before
   export.
9. Add retention limits by age and total storage size.
10. Prevent replay from reusing prior approvals. Replayed runs must pass current
    policy and create new tickets.

### Tests

- A ticket from one session or conversation cannot approve another run.
- A changed payload invalidates its prior approval.
- Replay cannot inherit approval state.
- Debug bundles contain no configured test secrets or absolute user paths.
- Retention removes expired records and does not break current runs.

### Definition of done

- Diagnostic read access cannot trigger runtime actions.
- Approval decisions are bound to an exact action and caller context.
- Support exports are redacted and time bounded.

## Phase 5: High-Risk Capability Hardening

### 5.1 Skill Creator

#### Finding

`skill-creator` is enabled by default and writes executable skill source into
the runtime skill tree.

Relevant code:

- `backend/app/agent/settings_store.py:47`
- `backend/app/agent/settings_store.py:382`
- `backend/app/skills/skill_creator/tools.py:52`
- `backend/app/skills/skill_creator/tools.py:224`

#### Changes

1. Disable the skill by default.
2. Require an administrator scope and explicit approval for every create,
   update, rename, or delete action.
3. Write generated skills to a staging directory.
4. Parse and validate metadata, file names, file count, and total bytes.
5. Reject symlinks, junctions, traversal, hidden executable payloads, and
   writes outside the designated skill root.
6. Run static checks before activation.
7. Activate through an atomic directory swap and preserve a rollback copy.
8. Do not auto-load a generated skill in the same turn that created it.
9. Record author, source conversation, approval, content hash, and activation
   time.

#### Tests

- The default configuration does not register skill-creator tools.
- Traversal, symlink, alternate data stream, and reserved-name attempts fail.
- Partial writes never leave an active skill.
- Activation requires a new runtime load boundary.

### 5.2 MCP Bridge

#### Finding

MCP tool approval is inferred from tool names, descriptions, and
self-declared annotations.

Relevant code:

- `backend/app/skills/mcp_bridge/registry.py:130`
- `backend/app/skills/mcp_bridge/registry.py:144`
- `backend/app/skills/mcp_bridge/tools.py:58`

#### Changes

1. Default every reflected MCP tool to approval-required.
2. Add per-server trust configuration with explicit tool allowlists and risk
   classes.
3. Treat MCP annotations as descriptive input, not authoritative policy.
4. Require approval for unknown tools, schema changes, server refresh, and
   reconnect.
5. Snapshot the reflected tool schema and bind approvals to its hash.
6. Validate argument size, nesting depth, scalar lengths, and JSON schema
   before invoking a server.
7. Add per-server concurrency, timeout, response-size, and restart limits.
8. Redact server command lines, credentials, stderr, and environment details in
   normal diagnostics.

#### Tests

- Misleading names such as `read_delete_all` cannot bypass approval.
- A changed tool schema invalidates an existing approval.
- Untrusted annotations cannot downgrade risk.
- Oversized arguments and responses are rejected.
- A failing MCP server cannot block unrelated servers or the agent loop.

### 5.3 Exec, Filesystem, Browser, and Computer Use

#### Changes

1. Use a shared capability-policy interface instead of duplicating permission
   logic in each tool.
2. Canonicalize filesystem paths once and carry a typed resolved-path object
   through policy and execution.
3. Recheck paths immediately before mutation to reduce time-of-check/time-of-use
   races.
4. Reject symlink and junction escapes for protected roots.
5. Make browser outbound access deny-by-default for automated and scheduled
   sources. An empty domain list must not mean unrestricted access.
6. Apply DNS resolution and redirect checks at every browser fetch hop to
   prevent SSRF and rebinding bypasses.
7. Require explicit approval for downloads, uploads, clipboard access,
   credential entry, external-protocol launches, and file chooser interaction.
8. Bind computer-use batches to the exact visible target window and invalidate
   approval when focus or window identity changes.
9. Add operation budgets for repeated clicks, typing, screenshots, DOM
   captures, subprocesses, and filesystem traversal.

#### Tests

- Path policy covers symlink, junction, case, UNC, device path, and race cases.
- Browser redirects cannot reach loopback, link-local, or private networks.
- Scheduled browser runs cannot use unrestricted outbound access.
- Computer-use approval cannot be reused after the target window changes.
- Large directory and DOM operations stop at configured budgets.

### 5.4 Memory and Scheduling Skills

#### Changes

1. Treat stored memory and scheduled prompts as untrusted persisted input.
2. Pass retrieved memory through injection detection and source labeling.
3. Never let memory content alter tool policy, system instructions, or approval
   state.
4. Require confirmation before storing secret-like or highly sensitive
   content.
5. Add per-schedule permission profiles and maximum run budgets.
6. Prevent scheduling tools from creating tasks with greater authority than the
   current caller.

## Phase 6: Source-Aware Execution Policy

### Finding

Scheduled tasks and Telegram invoke the same agent runtime as the interactive
desktop flow. Their source is not a first-class input to capability policy.

Relevant code:

- `backend/app/agent/scheduler.py:275`
- `backend/app/integrations/telegram/agent_bridge.py:191`
- `backend/app/integrations/telegram/bridge.py`

### Required design

Introduce an `ExecutionPrincipal` and `ExecutionSource` in `RunContext`:

```text
source: desktop | scheduler | telegram | replay | api
principal_id: authenticated local session, Telegram user, or task owner
conversation_id: current conversation
permission_profile_id: immutable profile snapshot
interactive: whether synchronous approval is possible
```

Every policy decision, ticket, audit event, memory write, file access, MCP call,
and sandbox request must receive this context.

### Required changes

1. Desktop runs use the authenticated application session.
2. Telegram runs use the verified Telegram user and chat identity, not only the
   bot-wide configuration.
3. Scheduled tasks persist their owner and a restricted permission snapshot.
4. Replay runs use a dedicated source and receive no inherited approvals.
5. Default non-interactive policy denies:
   - host shell execution;
   - arbitrary filesystem mutation;
   - computer use;
   - skill creation;
   - unrestricted browser access;
   - new persistent access grants.
6. Add rate limits per Telegram user, chat, scheduled task, and source.
7. Add explicit audit labels and source filters in observability.
8. Require local confirmation for Telegram approval callbacks that grant
   permanent access.

### Tests

- A Telegram user cannot act under another user's session or conversation.
- A scheduled task cannot escalate beyond its saved permission profile.
- Changing global settings does not silently broaden an existing scheduled
  task without re-authorization.
- Replay and API sources cannot consume desktop approval tickets.

### Definition of done

- Every agent run has an authenticated or system-owned principal.
- Policy behavior differs intentionally by source and is covered by tests.

## Phase 7: Scheduler Reliability and Idempotency

### Finding

The scheduler polls SQLite, tracks active work in memory, and cancels in-flight
tasks during shutdown. Queue and running state are not fully durable.

Relevant code:

- `backend/app/agent/scheduler.py:68`
- `backend/app/agent/scheduler.py:80`
- `backend/app/agent/scheduler.py:139`
- `backend/app/agent/scheduler.py:250`
- `backend/app/api/routes/scheduled_tasks.py`

### Required changes

1. Add durable run states:
   `claimed`, `queued`, `running`, `succeeded`, `failed`, `cancelled`,
   `abandoned`.
2. Store a lease owner and lease expiry for claimed work.
3. Recover expired leases on startup.
4. Persist queue order for `queue` overlap policy.
5. Give each scheduled occurrence a deterministic idempotency key.
6. Make run creation and due-time advancement one database transaction.
7. Reconcile orphaned conversations and runs after crashes.
8. Apply per-task and global concurrency limits.
9. Add exponential backoff with a maximum retry count and a dead-letter state.
10. Store truncated structured outputs instead of concatenating every streamed
    token in memory.
11. Add shutdown grace periods before marking a run abandoned.
12. Expose scheduler health without exposing prompt or path contents.

### Tests

- Restart during `claimed`, `queued`, and `running` states recovers correctly.
- Two scheduler instances cannot execute the same occurrence.
- Queue order survives restart.
- `skip` and `cancel_previous` remain deterministic under concurrent ticks.
- Large streamed output stays within memory and database limits.

### Definition of done

- At-least-once execution is explicit and duplicate effects are controlled by
  occurrence idempotency keys.
- Restart does not silently lose queued work.

## Phase 8: Attachment and Local File Access

### Finding

Attachment prompts include absolute paths, while a global persisted registry
maps attachment IDs to local files.

Relevant code:

- `backend/app/api/routes/chat.py:17`
- `backend/app/api/routes/chat.py:34`
- `backend/app/agent/response_attachments.py:20`
- `backend/app/agent/response_attachments.py:99`
- `backend/app/api/routes/files.py:13`

### Required changes

1. Replace absolute paths in chat prompts with opaque attachment handles.
2. Store attachment metadata by authenticated session and conversation.
3. Use random IDs rather than stable path-derived IDs.
4. Add expiry, maximum registry size, and cleanup.
5. Authorize every download and preview against session, conversation, and
   attachment ownership.
6. Revalidate the target file and permitted root when it is opened.
7. Prevent registry entries for directories, devices, sockets, and disallowed
   roots.
8. Stream files with size limits and safe content-disposition handling.
9. Generate previews in an isolated worker and cap decode dimensions.
10. Avoid returning absolute paths in API responses, logs, or debug exports.

### Tests

- An attachment ID from another session or conversation is rejected.
- Expired and deleted files return a safe not-found response.
- Registry growth is bounded.
- Path replacement, symlink changes, and unsupported file types are rejected.
- Preview generation handles malformed and oversized media safely.

## Phase 9: Backend Architecture Refactoring

### Finding

Several modules combine too many responsibilities:

| Module | Approximate size | Coupled responsibilities |
| --- | ---: | --- |
| `agent/turn_loop.py` | 2,666 lines | orchestration, approval resume, events, observability, retries, tool flow |
| `agent/long_term_memory.py` | 2,518 lines | storage, retrieval, ranking, archive, formatting, reconciliation |
| `skills/browser_use/tools.py` | 3,007 lines | tool schemas, validation, navigation, fetch, DOM actions, diagnostics |
| `skills/computer_use/window_ops.py` | 2,288 lines | discovery, matching, state, UI operations |
| `agent/llm_client.py` | 1,360 lines | provider adapters, schemas, retries, streaming, normalization |
| `agent/observability/recorder.py` | 1,250 lines | logging, persistence, querying, replay, export |
| `agent/controller_policy.py` | 1,151 lines | normalization, permission rules, grants, action decisions |
| `agent/database.py` | 1,086 lines | schema creation, migrations, and repositories |

### Target architecture

Use explicit application services and typed ports:

```text
API / Electron / Telegram / Scheduler
                |
          AgentRunService
                |
   RunStateMachine + PolicyService
                |
    ToolExecutor + Capability Ports
                |
  Persistence / LLM / Sandbox Adapters
```

### Turn loop changes

1. Extract a typed run-state machine with explicit states:
   `initializing`, `model_call`, `tool_execution`, `waiting_approval`,
   `waiting_access`, `finalizing`, `completed`, `failed`, `cancelled`.
2. Move approval waiting and resume behavior into an `ExecutionGateService`.
3. Move event construction into a typed event publisher.
4. Move observability calls behind an interface.
5. Keep iteration and token budgets in a dedicated budget object.
6. Make cancellation a first-class state transition.
7. Remove provider-specific response handling from orchestration.
8. Add state-transition tests rather than relying only on long end-to-end turn
   tests.

### LLM client changes

1. Define one provider adapter interface.
2. Keep provider request/response conversion in separate modules.
3. Centralize retry classification and idempotency behavior.
4. Stream through bounded queues to provide backpressure.
5. Avoid retaining duplicate full prompt and response copies.
6. Validate model capability and schema compatibility before the request.
7. Track usage and latency through the observability interface.

### Database changes

1. Separate schema bootstrap, migration runner, and repositories.
2. Make migrations forward-only, transactional where SQLite permits, and
   covered by upgrade tests from every supported schema version.
3. Add foreign keys and indexes for observed query patterns.
4. Configure busy timeout and WAL deliberately.
5. Keep transactions short and avoid synchronous database calls inside hot
   async loops where they can block the event loop.
6. Add integrity checks and a documented backup/restore path.

### Memory changes

1. Split ingestion, retrieval, ranking, consolidation, archival, and rendering.
2. Define a storage repository independent of retrieval policy.
3. Apply bounded candidate sets before expensive ranking.
4. Batch embedding and database operations.
5. Store provenance, sensitivity, source principal, and retention class.
6. Prevent cross-principal retrieval unless explicitly configured.
7. Add deletion and compaction jobs with resumable checkpoints.

### Capability module changes

1. Keep tool registration and JSON schema definitions separate from execution.
2. Put validation in typed request models.
3. Put policy checks in the shared policy service.
4. Put external I/O behind adapter interfaces.
5. Return one structured tool result shape with stable reason codes.
6. Set per-tool timeout, output, concurrency, and retry budgets.

### Definition of done

- The run state machine can be tested without a live provider or database.
- Provider, policy, persistence, and capability implementations are replaceable
  through narrow interfaces.
- No new module should repeat permission or approval policy logic.

## Phase 10: API Contracts and Error Handling

### Finding

Some routes accept weakly typed dictionaries, and frontend clients handle
errors inconsistently.

Relevant code:

- `backend/app/api/routes/settings.py:279`
- `frontend/src/lib/api/client.ts`
- `frontend/src/lib/api/settings.ts`

### Required changes

1. Define strict Pydantic input and output models for every endpoint.
2. Reject unknown fields on security-sensitive inputs.
3. Add optimistic concurrency versions to settings and policy updates.
4. Standardize errors:

```json
{
  "code": "stable_reason_code",
  "message": "safe user-facing message",
  "request_id": "opaque-id",
  "details": {}
}
```

5. Do not return raw exception messages to clients.
6. Add API client helpers for timeout, cancellation, error decoding, and auth.
7. Apply endpoint-specific pagination and hard maximum limits.
8. Version breaking API changes.
9. Generate or validate frontend types from the backend API schema.

### Tests

- Unknown fields and invalid enum values fail with stable errors.
- Concurrent settings edits detect stale versions.
- Frontend surfaces mutation failures instead of silently keeping stale state.
- Pagination limits cannot be bypassed.

## Phase 11: Frontend and Electron Performance

### Finding

The application polls conversations, schedules, approvals, usage, and memory.
Scheduled-task equality serializes complete objects. Assistant content is
reformatted in a render-sensitive path.

Relevant code:

- `frontend/src/App.tsx:51`
- `frontend/src/App.tsx:157`
- `frontend/src/components/ApprovalToast.tsx:52`
- `frontend/src/components/InputBar.tsx:133`
- `frontend/src/features/settings/MemorySettingsPanel.tsx:180`
- `frontend/src/lib/formatAgentResponse.ts:8`
- `frontend/src/components/MessageBubble.tsx`

### Required changes

1. Add a single authenticated server event channel for:
   - conversation changes;
   - scheduled-task changes;
   - approval and access-grant tickets;
   - usage changes;
   - backend readiness.
2. Use events to invalidate local queries instead of polling every feature.
3. Keep a low-frequency recovery poll only if the event channel disconnects.
4. Normalize entities by ID and compare revision/version fields instead of
   `JSON.stringify`.
5. Cache formatted assistant content by message ID and content revision.
6. Batch streamed token updates to animation frames or a bounded interval.
7. Virtualize long conversation and observability lists.
8. Abort stale requests when switching conversations or closing settings.
9. Lazy-load settings, diagnostics, memory debugging, and scheduling panels.
10. Ensure timers are centralized and paused when the window is hidden.

### Electron hardening included in this phase

1. Set `sandbox: true` if compatibility tests permit it.
2. Validate the sender for every IPC handler, including store migration and
   dialogs.
3. Freeze the preload API surface and validate every argument.
4. Reject navigation, new windows, and external protocols except through the
   existing explicit URL validation path.
5. Narrow the Content Security Policy and remove development exceptions from
   production builds.

### Tests and measurements

- Idle authenticated API traffic approaches zero while events are connected.
- Streaming remains responsive for long messages.
- Switching conversations does not apply stale responses.
- Production Electron tests confirm navigation and IPC restrictions.
- Bundle size and startup time do not regress beyond agreed budgets.

## Phase 12: Observability, Privacy, and Retention

### Required changes

1. Classify fields as public metadata, operational data, sensitive content, or
   secret.
2. Default to recording metadata and reason codes, not full prompts and tool
   outputs.
3. Make detailed content capture an explicit support setting with expiry.
4. Add age and size retention for:
   - observability runs;
   - backend logs;
   - debug bundles;
   - approval history;
   - attachment registry;
   - browser and MCP diagnostics;
   - scheduled-task output.
5. Encrypt sensitive retained content at rest where it must be preserved.
6. Add a user-visible data deletion operation and verify deletion across all
   stores.
7. Prevent debug export from following symlinks or including files outside
   approved runtime roots.
8. Add metrics for dropped events, queue depth, database latency, model latency,
   tool latency, and redaction failures.

### Definition of done

- The default runtime does not retain full sensitive conversations in multiple
  observability copies.
- Storage growth is bounded and visible.
- Export and deletion behavior are covered by integration tests.

## Phase 13: Scripts, Deployment, and Documentation

### Required changes

1. Update startup scripts to:
   - bind the backend to loopback;
   - pass control-plane credentials securely;
   - verify runtime directory permissions;
   - fail when required security configuration is missing;
   - avoid printing credentials or full environment data.
2. Document that the API is a privileged local control plane and must not be
   exposed through port forwarding, reverse proxies, tunnels, or permissive
   firewall rules.
3. Update sandbox documentation so `local_direct` and `local_restricted` are
   clearly identified as host execution.
4. Document source-specific restrictions for Telegram and scheduled tasks.
5. Document credential storage and migration.
6. Add incident guidance for revoking API keys, Telegram tokens, control-plane
   tokens, approvals, and persistent grants.
7. Add backup, restore, retention, and complete-data-deletion instructions.
8. Keep endpoint documentation generated from, or checked against, actual
   route definitions.

### Tests

- Startup fails safely when the control-plane secret cannot be established.
- Packaged and development launchers use the same security model.
- Documentation examples pass automated smoke tests where practical.

## Module-by-Module Completion Checklist

### System entrypoints and configuration

- [ ] Loopback-only default binding
- [ ] Authenticated API startup
- [ ] Exact CORS allowlist
- [ ] Request-size and rate limits
- [ ] Secure startup-script secret handling
- [ ] Accurate deployment documentation

### Backend API

- [ ] Authentication and scope dependency on every privileged route
- [ ] Strict request and response schemas
- [ ] Standard error envelope
- [ ] Pagination and payload limits
- [ ] Session-bound files, approvals, and grants
- [ ] Redacted diagnostics and observability

### Agent runtime

- [ ] Typed run state machine
- [ ] Source and principal in run context
- [ ] Central execution-gate service
- [ ] Bounded streaming queues
- [ ] Explicit cancellation and timeout behavior
- [ ] Provider adapters separated from orchestration

### Security and control subsystems

- [ ] Fail-closed sandbox selection
- [ ] Exact-action approval binding
- [ ] Access-grant expiry and ownership
- [ ] Central recursive redaction
- [ ] Audit records include source, principal, policy, and backend
- [ ] Prompt injection cannot modify policy state

### Exec skill

- [ ] Strong isolation for untrusted commands
- [ ] Explicit host-execution mode
- [ ] Environment, output, process, and time limits
- [ ] No implicit fallback

### Filesystem skill

- [ ] Canonical typed paths
- [ ] Symlink and junction defense
- [ ] TOCTOU-resistant mutation checks
- [ ] Traversal and size budgets

### Browser-use skill

- [ ] Deny-by-default domain policy for non-interactive sources
- [ ] DNS and redirect SSRF checks
- [ ] Download, upload, and external-protocol approvals
- [ ] DOM, screenshot, and response budgets

### Computer-use skill

- [ ] Approval bound to target window identity
- [ ] Focus-change invalidation
- [ ] Action-batch limits
- [ ] Clipboard and credential-entry restrictions

### MCP bridge

- [ ] Per-server trust policy
- [ ] Default approval requirement
- [ ] Schema-hash approval binding
- [ ] Argument, response, timeout, and concurrency limits

### Memory

- [ ] Principal and provenance isolation
- [ ] Sensitive-content classification
- [ ] Injection-resistant retrieval
- [ ] Retention and deletion
- [ ] Bounded and batched retrieval pipeline

### Scheduling

- [ ] Restricted saved permission profile
- [ ] Durable queue and leases
- [ ] Restart recovery
- [ ] Idempotency keys and retry policy
- [ ] Global and per-task concurrency limits

### Telegram

- [ ] User and chat principal propagation
- [ ] Per-user and per-chat rate limits
- [ ] Restricted default capability profile
- [ ] Local confirmation for persistent grants
- [ ] Token stored outside renderer-accessible storage

### Skill creator

- [ ] Disabled by default
- [ ] Administrator-only activation
- [ ] Staged validation and atomic install
- [ ] Audit trail and rollback
- [ ] No same-turn auto-loading

### Electron

- [ ] OS-backed credential storage
- [ ] Narrow sender-validated IPC
- [ ] Renderer sandbox enabled where supported
- [ ] Navigation and external URL restrictions
- [ ] Production CSP verification

### Frontend

- [ ] Authenticated centralized API client
- [ ] Event-driven invalidation
- [ ] Stable entity revisions
- [ ] Stream update batching
- [ ] Long-list virtualization
- [ ] Consistent visible error handling

### Persistence and operations

- [ ] Transactional migration tests
- [ ] Foreign keys and query indexes
- [ ] WAL and busy-timeout policy
- [ ] Retention jobs
- [ ] Backup, restore, and integrity checks

## Recommended Pull Request Sequence

1. Add baseline test groups and CI timeouts.
2. Introduce API authentication primitives without changing frontend behavior.
3. Wire Electron session authentication and enforce it on all API routes.
4. Tighten CORS and loopback binding.
5. Move credentials to OS-backed storage and migrate legacy values.
6. Gate and redact diagnostics, observability, approvals, grants, and files.
7. Change sandbox modes and remove implicit host fallback.
8. Add execution source and principal to `RunContext`.
9. Apply source-aware policy to scheduler, Telegram, replay, and API runs.
10. Harden MCP reflected-tool policy.
11. Disable and stage skill-creator.
12. Scope attachments to sessions and conversations.
13. Add durable scheduler leases, queue state, and recovery.
14. Introduce event-driven frontend invalidation.
15. Extract the run state machine and execution-gate service.
16. Split LLM, memory, database, browser, and observability monoliths
    incrementally.
17. Add retention, deletion, backup, and restore workflows.
18. Complete documentation and packaged-app security verification.

## Release Gates

Do not consider the remediation complete until all of the following are true:

- Anonymous requests cannot invoke or inspect privileged API behavior.
- Renderer JavaScript cannot retrieve stored credentials.
- Untrusted execution cannot fall back to an unenforced host runner.
- Approval and access-grant decisions are bound to exact callers and payloads.
- Scheduled, Telegram, replay, and API runs have explicit restricted principals.
- MCP tools cannot self-classify into a lower-risk policy.
- Attachment access is scoped, expiring, and path-redacted.
- Scheduler recovery and duplicate-execution behavior are tested.
- Observability and runtime data have enforced retention limits.
- Idle frontend polling has been replaced or reduced to recovery behavior.
- Security-sensitive tests run in CI and packaged Electron behavior is tested.

## Verification Strategy

For each pull request:

1. Add unit tests for policy and validation decisions.
2. Add integration tests at trust boundaries.
3. Add a regression test reproducing the original risky behavior.
4. Run the relevant backend test group.
5. Run frontend tests, type checking, and production build when frontend or
   Electron code changes.
6. Measure performance when changing polling, streaming, memory retrieval,
   scheduling, database access, or large-list rendering.
7. Review logs and debug exports with seeded fake secrets and paths.
8. Record migration and rollback instructions in the pull request.

The security work should be reviewed before large architecture refactors. Once
the trust boundaries are enforced and covered by tests, the architecture work
can proceed without accidentally preserving unsafe compatibility behavior.
