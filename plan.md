# Monaw Agent — Refinement Plan

## Context

Monaw is a local-first Windows desktop agent (FastAPI + Electron/React, ~43k backend / ~16k frontend LOC). The security design is thoughtful and test coverage is broad (60+ backend test files, grouped in CI). The problem is not missing features — it is that **many subsystems are declared far more completely than they are wired**. Settings expose switches that no code reads, modes that have no runner, and diagnostics that the API blanks before they reach the screen. That is the source of the discomfort behind this request: "the MCP is not sure whether working or not", "the skill names are messy".

The exploration turned up something more urgent than naming, though: **on a fresh install the agent cannot reliably run a shell command at all.**

| Area | Declared | Actual |
|---|---|---|
| Shell exec | sandboxed execution with approval | `\bformat\b` is a blocked pattern, so `Get-Process \| Format-Table` and `git log --format=%H` are **hard-blocked with no approval path**; the default workspace isn't a permitted root, so the first `exec` needs an access grant *and then* an approval — and the chained second gate is dropped on the floor |
| Sandbox | 8 modes, strong Docker isolation, `auto` default | Docker is unavailable out of the box (default image is a tag, not a digest) → `auto` silently degrades to advisory host execution; and when Docker *does* run it **mounts nothing**, so the container cannot see the workspace. `wsl` mode can never execute anything |
| Memory | curated long-term memory with provenance | **39,747** audit files on this machine backing **1.9 KB** of usable memory; retrieval injects on importance alone; a GET mutates memory state |
| Skills | `tier`, `always`, `enabled_by_default` metadata | `tier: internal` is silently coerced to `recommended`; `always:` is fully implemented and used by nothing; the UI shows raw ids (`exec`, `core`, `mcp-bridge`) |
| MCP | diagnostics with stderr tail, PID, executable | all blanked by the API before reaching the UI — five permanently empty rows; no health polling; saving settings silently wipes `trusted_tools` |

**Intended outcome:** the agent can do ordinary work on a clean install; every switch in Settings corresponds to something real; every failure has a visible reason; local data stops growing without bound; and the skills list reads as capabilities rather than module names.

**Decisions taken for this plan:**
- Make Docker genuinely work — pin a digest, resolve/pre-pull from the UI, surface real isolation status.
- Prune the memory audit trail *and* stop the bleeding: move it to SQLite, drop per-turn events, add retention, discard the 39,747 existing files.
- Hide built-in skills from the UI and add human display metadata.
- **No migration burden — there are no users yet.** Breaking changes to `settings.json`, the DB, and the memory folder are fine; prefer a clean reset over migration scaffolding. Remove `wsl` from settings; delete the stale DB backup.

---

## Phase 1 — Make shell execution work

**Goal:** a clean install can run `Get-ChildItem`, `Format-Table`, and `pip install` without dead ends. This phase is first because everything else is cosmetic if the agent can't execute.

### 1a. Fix the `format` false positive (one-line, highest impact)

[`policy.py:46`](backend/app/agent/sandbox/policy.py:46) is `r"\bformat\b"`, case-insensitive, in `_BLOCKED_PATTERNS`. Verified matches: `Get-Process | Format-Table`, `git log --format=%H`, `Get-Date -Format o`, `docker ps --format json`. Blocked commands get `reason_code="command_blocked"` at [`policy.py:91`](backend/app/agent/sandbox/policy.py:91) with **no approval path** — everyday PowerShell on the primary target platform is simply refused.

Replace with a pattern that matches the actual disk-formatting commands (`format.com`, `format <drive>:`, `Format-Volume`, `diskpart`), and add negative-case tests. `_BLOCKED_PATTERNS` has no negative tests today at all; one assertion would have caught this.

### 1b. Make the default workspace usable

`_effective_workdir` defaults to `WORKSPACE_DIR` = `~/.monaw/workspace` ([exec/tools.py:201](backend/app/skills/exec/tools.py:201)), but `DEFAULT_FOLDER_RULES = []` ([settings_store.py:40](backend/app/agent/settings_store.py:40)) and `_is_trusted_runtime_path` covers only `RUNTIME_DIR` and the browser dirs ([controller_policy.py:569](backend/app/agent/controller_policy.py:569)) — **not** the workspace. So on a fresh install every `exec` first returns `pending_access_grant` for the agent's own default working directory.

Seed the default workspace as a permitted root: add it to `_is_trusted_runtime_path`, or ship a `DEFAULT_FOLDER_RULES` entry for `AGENT_WORKSPACE_DIR` with read/write and no confirmation. This is the agent's own sandbox home — treating it as foreign territory buys no safety and costs every first command.

### 1c. Handle chained gates

`exec` legitimately needs two gates on first use (access grant, then approval). After a grant resolves, `_resume_access_grant` re-invokes the tool without `_bypass_gate` ([execution_gate.py:219](backend/app/agent/execution_gate.py:219)), so `exec_tool` returns `pending_approval` — and the turn loop assigns it straight to `tool_output` without re-checking pending status:

```python
tool_output = resolved_output or json.dumps({"status": "error", "error": "Resolution lost."})
status = self._tool_output_status(tool_output, status)   # turn_loop.py:1949-1953
```

The second ticket is created, shown to the model as an error, and left pending until it expires. `handle_pending_tool_output` ([execution_gate.py:41](backend/app/agent/execution_gate.py:41)) has the same single-hop limit.

Loop the gate: after a resume, re-inspect the output for `pending_approval` / `pending_access_grant` and wait again, with a small hop cap (2–3) to prevent cycles. 1b reduces how often this fires; 1c is what makes it correct.

### 1d. Close the `exec_write_stdin` policy bypass

`exec_poll`, `exec_write_stdin`, and `exec_stop` ([exec/tools.py:500-516](backend/app/skills/exec/tools.py:500)) perform **no policy check at all**. Approve one interactive `powershell` session and every subsequent command can be fed through stdin — `classify_command`, `resolve_permission`, and the approval broker are never consulted again. This defeats the entire gating chain for the cost of one approval.

- Run `classify_command` + `resolve_permission` on each `exec_write_stdin` payload, and scope the session's approval to the command it was granted for.
- Add a session lifetime cap. `exec_start` passes `timeout=0` ([exec/tools.py:397](backend/app/skills/exec/tools.py:397)) and the registry never applies one; `cleanup_stale` ([sessions.py:201](backend/app/agent/sandbox/sessions.py:201)) has no production caller, so a model-started session outlives the turn indefinitely.
- Also fix: `_session_payload` pops the session as soon as `exit_code is not None` ([sessions.py:136](backend/app/agent/sandbox/sessions.py:136)), so final output is readable exactly once — a second `exec_poll` returns `unknown_command`.

### 1e. Kill process trees, honestly

`local_direct`'s timeout calls `proc.kill()` ([local_direct.py:168](backend/app/agent/sandbox/backends/local_direct.py:168)), which kills only the shell — children are orphaned on Windows. `local_restricted` adds `CREATE_NEW_PROCESS_GROUP` + `taskkill /F /T` and that is the *entire* difference between the two backends. Meanwhile `use_job_object`, `strip_environment`, and `kill_process_tree_on_timeout` ([settings_store.py:277](backend/app/agent/settings_store.py:277)) are **never read** — there is no Job Object, despite `use_job_object: True` being the default and shown in the UI.

Use `taskkill /F /T` in `local_direct` too, then **delete the three inert settings** rather than leaving switches that do nothing. `docs/sandboxing.md:17` claims advisory mode has "resource controls" — it has none; correct the doc.

Cancellation has the same shape: `exec` runs in a `ThreadPoolExecutor` ([tool_executor.py:173](backend/app/agent/harness/tool_executor.py:173)), so cancelling the awaiting coroutine kills neither the OS process nor the thread. A cancelled or timed-out turn leaves PowerShell running. Wire cancellation through to the subprocess.

---

## Phase 2 — Sandbox: make the modes real

**Goal:** `auto` delivers real isolation on a normal Windows machine with Docker Desktop, and every mode in the UI has a runner behind it.

### 2a. Make Docker reachable — and useful

Two separate problems, both required:

**It can't start.** [`probe_docker`](backend/app/agent/sandbox/capabilities.py:46) requires `name@sha256:<64 hex>`, but the shipped default is the tag `python:3.12-slim` ([settings_store.py:268](backend/app/agent/settings_store.py:268), mirrored at [settingsConfig.ts:257](frontend/src/features/settings/settingsConfig.ts:257)). So `docker.available` is `False` before Docker is even probed. Consequences: `auto` degrades to advisory host execution + an approval prompt; `enforce` blocks everything; and any command matching `_UNTRUSTED_PATTERNS` ([policy.py:18](backend/app/agent/sandbox/policy.py:18) — `pip install`, `npm install`, `curl | sh`) is **unconditionally blocked with no approval path**.

- Ship a pinned digest as the default.
- Add a Settings action that resolves a tag to a digest (`docker image inspect --format '{{index .RepoDigests 0}}'`) and pre-pulls it, writing the digest back to settings. This is what makes the pinning policy usable by a non-expert — keep the policy, make it actionable.
- Report `docker_image_not_pinned` as an actionable state ("image not pinned — resolve now"), not a bare capability failure.
- Give untrusted commands an approval path when strong isolation is unavailable, instead of a dead end.

**It can't do anything.** [`docker.py:33-67`](backend/app/agent/sandbox/backends/docker.py:33) builds genuinely hardened argv (`--network none --cap-drop ALL --read-only --pids-limit --memory --cpus --security-opt no-new-privileges --workdir /workspace`) but **mounts nothing** — `/workspace` is empty and `request.workdir` is ignored. A container that cannot see the user's files cannot do the user's work.

The machinery for this already exists and is already tested: [`path_policy.py`](backend/app/agent/sandbox/path_policy.py) has `create_run_workspace`, `copy_in_file`, `collect_copy_out`, `cleanup_old_runs`, `ensure_allowed_path`, and `SandboxPathPolicy` — **all with zero production callers** (only `write_artifact_manifest` is used). Wire it:

- Bind-mount the effective workdir at `/workspace` when it is under `allowed_bind_roots` (default: the agent workspace), read-write for `direct_rw`, read-only + `collect_copy_out` for `copy_out`.
- That makes `default_write_strategy` ([settings_store.py:293](backend/app/agent/settings_store.py:293)) and its UI dropdown "Files created by commands" ([SettingsModal.tsx:1932](frontend/src/components/SettingsModal.tsx:1932)) mean something — today `write_strategy` is carried in metadata and never acted on.
- Then `allowed_bind_roots` / `blocked_bind_roots` become live too. Fix the default mismatch: [schemas.py:321](backend/app/schemas.py:321) defaults `blocked_bind_roots` to `[]` while [settings_store.py:295](backend/app/agent/settings_store.py:295) defaults it to `DEFAULT_BLOCKED_ROOTS`.

**Two traps when pinning the digest:**
1. `test_sandbox_docker.py:167` skips based on the *default* image but then runs against `python@sha256:aaaa…` (64 `a`s). Once a real digest is the default, this test stops skipping and fails against a nonexistent image. Fix it in the same change.
2. Capability probing is **per-call and uncached** — `build_sandbox_decision` constructs a fresh `SandboxPolicy` and `exec_tool` a fresh `SandboxManager`, each running `probe_capabilities` ([exec/tools.py:61](backend/app/skills/exec/tools.py:61), [:351](backend/app/skills/exec/tools.py:351)). With the tag default both short-circuit cheaply; **with a real digest every exec pays 2 docker subprocess spawns with 3s timeouts.** Cache the probe (TTL, invalidated on settings change) as part of this change, or pinning the digest makes every command slower. It also removes a TOCTOU where policy and manager probe different answers.

### 2b. Delete the phantom `wsl` mode

`wsl` is in the type literals ([models.py:7](backend/app/agent/sandbox/models.py:7)), probed ([capabilities.py:77](backend/app/agent/sandbox/capabilities.py:77)), accepted by the schema ([settings_store.py:290](backend/app/agent/settings_store.py:290), [schemas.py:316](backend/app/schemas.py:316)), selected by [`policy._select_backend:196`](backend/app/agent/sandbox/policy.py:196), and offered in the UI ([SettingsModal.tsx:1958](frontend/src/components/SettingsModal.tsx:1958)) — but there is no runner, and `policy.decide` rejects it anyway at [:137](backend/app/agent/sandbox/policy.py:137) because `security_label == "medium" != "strong"`. **`mode="wsl"` can never execute anything.** Remove it everywhere, including `SandboxWslSettings` and `probe_wsl`.

### 2c. Collapse the two backend selectors

[`SandboxPolicy._select_backend`](backend/app/agent/sandbox/policy.py:189) is what runs in production. [`SandboxManager._select_backend`](backend/app/agent/sandbox/manager.py:76) is only reachable when `request.backend` is empty, which never happens because [exec/tools.py:345](backend/app/skills/exec/tools.py:345) always sets it. They **disagree** on `auto`: policy falls back to `local_restricted`; manager returns `unavailable`.

Worse, most of `test_sandbox_manager.py` asserts the *manager's* stricter behavior — so the suite describes a system that does not run. Make the policy the single owner of selection, have the manager execute a decision rather than re-derive one, and retarget those tests at the policy.

### 2d. Stop losing sandbox metadata in the audit log

[exec/tools.py:73-92](backend/app/skills/exec/tools.py:73) builds rich `sandbox_meta`, but the runners rebuild metadata from `SandboxRunMetadata` ([models.py:50](backend/app/agent/sandbox/models.py:50)), which has none of those fields. On every **successful** run the audit records `None`:

```python
"sandbox_trust_class": result.sandbox.get("trust_class"),            # tools.py:365 → None
"sandbox_required_isolation": result.sandbox.get("required_isolation"),
"sandbox_network_enforcement": result.sandbox.get("network_enforcement"),
"sandbox_filesystem_policy": result.sandbox.get("filesystem_policy"),
```

The blocked path preserves them, so only success is blind. `test_approval_preview_matches_execution_decision` ([test_sandbox_manager.py:193](backend/app/agent/tests/test_sandbox_manager.py:193)) claims to verify preview/execution parity but substitutes a `FakeManager` returning `sandbox=dict(request.env_metadata)` — preserving exactly the keys the real runners drop. Carry the fields through `SandboxRunMetadata` and make that test use the real runner.

### 2e. Surface isolation status honestly

Add a Sandbox status line in Settings naming the selected backend, whether isolation is `strong`/`advisory`/`none`, and the `reason_code` when it doesn't match what the mode implies — all fields `SandboxDecision` already carries. Then delete the settings that read nothing rather than displaying them: `require_strong_for_untrusted`, `extra_images`, `max_workspace_mb`, `network.allow_domains`, plus the three from 1e. `project_write` profile is never produced or consumed — remove it too.

Rewrite [docs/sandboxing.md](docs/sandboxing.md) (35 lines): it omits `wsl`, omits the four session tools entirely, claims all runners clamp execution time (sessions don't), and names the tag-vs-digest problem at `:33` without saying **that tag is the shipped default**.

---

## Phase 3 — Memory: stop the bleeding

**Goal:** memory stops growing without bound, stops mutating itself on reads, and stops spending tokens on duplicated context.

### 3a. Move the audit trail to SQLite and drop the folder

[`_audit`](backend/app/agent/long_term_memory.py:2294) writes one markdown file per event — including per-turn `INJECT`, `CANDIDATE`, `REJECT_CANDIDATE` — and [`audit_log`](backend/app/agent/long_term_memory.py:2268) does `sorted(glob("*.md"), reverse=True)` over the whole directory on every call. `stats()` calls it with `limit=500` ([:1613](backend/app/agent/long_term_memory.py:1613)), and `MemorySettingsPanel` refreshes stats on mount, tab change, filter change, window focus, and visibility change ([:176](frontend/src/features/settings/MemorySettingsPanel.tsx:176)).

- Add migration `0011_memory_audit.sql`: `memory_audit(id, action, reason, memory_id, source_conversation_id, candidate_content, created_at)`, indexed on `created_at DESC` and `(source_conversation_id, created_at)`. Follow the style in [`migrations/`](backend/app/agent/migrations) and register in [`runner.py`](backend/app/agent/migrations/runner.py).
- Rewrite `_audit` as an insert; rewrite `audit_log` as an indexed `SELECT … LIMIT ?`.
- Stop recording per-turn `INJECT` / `CANDIDATE` / `REJECT_CANDIDATE` as rows — make them counters. Keep provenance events (`CREATE_SECTION`, `UPDATE`, `MERGE_INTO_SECTION`, `DELETE`, `CURATE`, `PROFILE_UPDATE`).
- Delete `memory/audit/` on first start, guarded by `_approved_root` ([data_lifecycle.py:37](backend/app/agent/data_lifecycle.py:37)).
- **Delete audit rows when their memory is deleted.** Today each audit file embeds up to 1000 chars of content (verified: a PII email address survives deletion), contradicting the complete-deletion promise in [docs/operations.md](docs/operations.md).

### 3b. Close the retention holes

[`enforce_runtime_retention`](backend/app/agent/data_lifecycle.py:160) touches no memory store, and [`_diagnostic_roots`](backend/app/agent/data_lifecycle.py:148) omits several growing directories. `sandbox.preserve_artifacts_days` is exposed in the UI and read by nothing; `cleanup_old_runs` is never called; `delete_runtime_data()` ("delete my data") **leaves command output on disk**.

- Add `RUNTIME_DIR / "exec"` (572 files here), `RUNTIME_DIR / "sandbox"` (697), `RUNTIME_DIR / "policy"` to `_diagnostic_roots()`, driving max age from `preserve_artifacts_days`.
- Add pruning for `memory/short-term/`, `.system/curated/`, `memory_candidates` (no cap or expiry today), `memory_episodes`, `memory_checkpoints`, `memory_audit`.
- Give `messages_archive` an `ON DELETE CASCADE` FK — [`delete_conversation`](backend/app/agent/database.py:121) orphans its rows, unlike `conversation_compactions`.
- Run retention **periodically**, not only at startup ([main.py:38](backend/app/main.py:38)); reuse [`scheduler.py`](backend/app/agent/scheduler.py).
- Delete the stale `agent.before-test-history-cleanup.*.db` (13.4 MB beside the live 13.5 MB `agent.db`) and stop leaving unmanaged backups in the runtime folder.
- Bound `job_manager._jobs` ([:129](backend/app/agent/job_manager.py:129)) — never evicted today.

### 3c. Stop retrieval from mutating state

[routes/conversations.py:281](backend/app/api/routes/conversations.py:281) — a **GET** for context usage calls `build_long_term_memory_context` → `build_prompt` → `mark_used()`, rewriting markdown, bumping `use_count`/`updated_at`, and writing an audit event. Opening a conversation skews retrieval.

- Add `mark_used: bool = True` to `build_prompt`; pass `False` from the context-usage endpoint and the retrieval debugger.
- `mark_used` sets `updated_at = _now()` ([:1192](backend/app/agent/long_term_memory.py:1192)), which is the field `recency_score` reads ([:981](backend/app/agent/long_term_memory.py:981)). Split them: use `last_used_at` for usage, keep `updated_at` for edits. One change removes three coupled pathologies — permanent recency ≈ 1.0 for anything ever injected, the staleness test at [:2218](backend/app/agent/long_term_memory.py:2218) never firing, and the inflation loop (`use_count >= 10` → `importance += 1` each pass → every preference converges to 10).
- Also: `GET /api/memories/{id}` publishes `memory.changed` with `action: "updated"` ([routes/memories.py:216](backend/app/api/routes/memories.py:216)) — a read triggers UI refresh storms.
- Batch the counter writes. `mark_used` → `_write_record` → `_remove_section_by_id(include_archived=True)` loads all 12 category files and invalidates the record cache each time: on the order of 70+ reads and 12+ writes per turn just to increment counters.

### 3d. Fix retrieval relevance

[`_score_document`](backend/app/agent/long_term_memory.py:992) grants up to `0.25` from non-lexical terms (recency + importance + use) while default `min_relevance_score` is `0.15`. A fresh, important, frequently-used memory clears the floor **with zero query overlap**, so ~`retrieval_limit` memories are injected every turn regardless of relevance. (There are no embeddings anywhere in the repo — retrieval is pure lexical scoring; `test_memory_hybrid_search.py` is misnamed and its own test names say `test_lexical_search_*`.)

- Gate the non-lexical bonuses on `token_score > 0 or exact_score > 0`.
- Normalize `token_score` by document length — a 300-word section currently outranks a precise 8-word one.
- Stop `exact_score` matching the concatenated `f"{category} {kind} {content}"` haystack ([:953](backend/app/agent/long_term_memory.py:953)): a query containing "fact" gets a 0.35 bonus on every fact memory.
- Add the missing regression test: a zero-overlap memory must be **excluded** at the default threshold.

### 3e. Stop paying twice for the same context

[`build_llm_messages`](backend/app/agent/memory_manager.py:272) emits `Earlier conversation summary:`, then — because `set_task_goal` is called with the raw user message every turn ([turn_loop.py:1070](backend/app/agent/turn_loop.py:1070)) — always appends `get_context()`, which repeats that summary **and** the last 5 messages already present as real history. The current user message appears three times.

- Make `get_context()` additive only: Active Goal + Remaining Steps + last tool results.
- Emit `## Active Goal` only when it differs from the current turn.
- Cap `conversations.summary`, which grows monotonically via `state.summary = f"{new}\n\n{old}"` ([memory_manager.py:828](backend/app/agent/memory_manager.py:828)) and is injected verbatim every turn.
- After `/compact`, fold the orphaned `conversations.summary` into the compaction row instead of silently dropping it ([:299](backend/app/agent/memory_manager.py:299)).

### 3f. Fix or delete the auto-compaction pipeline

`ensure_context_fits` ([memory_manager.py:757](backend/app/agent/memory_manager.py:757)) can spin: `_compact` mutates `state.recent_messages`, but the next iteration's `estimate_context_tokens` → `build_llm_messages` **re-reads history from SQLite**, discarding the compression — while `_compact` keeps reporting success because [`compress_context`](backend/app/agent/context_compression.py:196) unconditionally appends `"boundary_protection"`. One LLM call per iteration until `max_turn_seconds` (1800s). Each pass also replaces `conversations.summary`, which is *prepended*, so measured usage can grow.

Also: `_repair_integrity` ([:141](backend/app/agent/context_compression.py:141)) is a `pass` body that still reports `"integrity_repair"`; Phase 1 pruning matches `"Tool result for {name}: ..."` ([:89](backend/app/agent/context_compression.py:89)), a shape `turn_loop.py` never produces, so it never fires — and neither does the anti-thrash early exit that depends on it.

**`context_compression.py` has no test file at all.** Write one covering loop termination *before* changing behavior. Then make `_compact` persist through the path `build_llm_messages` reads, exit when measured usage doesn't improve, and delete the two phantom phases.

### 3g. Retire the inert memory layers

Three of the four tables from `0008_memory_operational_layers.sql` are write-only:
- `memory_profile_fields` — written only by `PATCH /api/memories/profile/{field}`, never injected into any prompt. A parallel identity store to `IdentitySettings`, which *is* injected ([runtime.py:85](backend/app/agent/runtime.py:85)).
- `memory_episodes` — `decisions_json`, `fixes_json`, `open_questions_json`, `follow_ups_json` are hardcoded `'[]'` at the insert site ([:1844](backend/app/agent/long_term_memory.py:1844)).
- `memory_checkpoints` — 19 columns, of which 10 (`browser_url`, `workspace_path`, `files_touched`, `commands_run`, `expires_at`, …) are never written by any caller.

Recommended: keep `memory_episodes` trimmed to the columns actually written; delete `memory_profile_fields` and the unwritten checkpoint columns, since `IdentitySettings` already covers profile and nothing consumes checkpoints. Either way remove the read-only "Profile" / "Current Context" panels in `MemorySettingsPanel.tsx` that present inert data as live.

Also dead or inconsistent here: `short-term/` session summaries are write-only (every read path defaults `include_short_term=False`); `restore_message` ([database.py:589](backend/app/agent/database.py:589)) and `closeMemorySession` ([memories.ts:114](frontend/src/lib/api/memories.ts:114)) have zero callers — yet `archive_old_messages` caps history at 50 with no path back; `_memory_files(include_archived=...)` ignores its own parameter ([:410](backend/app/agent/long_term_memory.py:410)); `0010` re-adds indexes `0008` already created; `stats()` returns top-level `fact`/`reflection` meaning *kind* while `categories.fact` means *category* ([:1633](backend/app/agent/long_term_memory.py:1633)).

### 3h. Stop auto-learn from manufacturing wrong memories

The extractor ([:1884](backend/app/agent/long_term_memory.py:1884)) is 15 regexes. Verified failures:
- `r"\b(don't|do not|never|avoid)\s+(.+?)"` turns *"don't forget to check the log file"* into *"The user wants the assistant to avoid forget to check the log file."*
- A bare `use …` makes *"use the venv python for this one script"* a permanent **workflow** memory.
- Any *"always …"* is stored at importance 8 — straight into the query-independent identity tier.
- `_should_skip_learning` ([:2316](backend/app/agent/long_term_memory.py:2316)) whitelists `\buse\b` and `\bi'?m\b` as "high signal", bypassing the length, code-block, and sensitivity guards for a large share of ordinary messages.

Narrow to patterns that produce grammatical output (`i prefer`, `my name is`, `call me`, `please always`), drop `use`/`i'm` from the bypass, and add negative tests — *"don't forget X"* must not be stored.

Note the real trigger gap: LLM curation only runs via `catch_up_unprocessed_sessions(max_sessions=3)` at startup ([runtime.py:138](backend/app/agent/runtime.py:138)), so any conversation that leaves the top 3 before the next restart is **never curated**. Either wire a genuine session-close hook (`closeMemorySession` already exists, uncalled) or stop advertising session curation in [docs/memory.md:119](docs/memory.md).

Minor: memory tool schemas still offer the removed `"style"` category and omit `reflection` from `searchable_categories` ([skills/memory/tools.py:123](backend/app/skills/memory/tools.py:123)); a single malformed JSON meta comment drops **every** memory in that category file from retrieval ([memory_repository.py:136](backend/app/agent/memory_repository.py:136)) — skip the section, not the file; `_tokenized_record_cache` never evicts ([:964](backend/app/agent/long_term_memory.py:964)).

---

## Phase 4 — Skills: capabilities, not module names

**Goal:** Settings → Skills lists things a user recognizes as features; infrastructure is on and invisible.

### 4a. Make `tier: internal` work

[`_skill_tier`](backend/app/agent/skill_loader.py:113) coerces anything outside `{recommended, optional}` to `recommended`, so `mcp_bridge`'s `tier: internal` is inert — it only *appears* hidden because `mcp_bridge` is excluded from discovery entirely by the hardcoded `BUILT_IN_FEATURE_SLUGS = {"mcp_bridge"}` ([:24](backend/app/agent/skill_loader.py:24)).

- Accept `internal` as a real tier. Mark `core`, `exec`, `filesystem`, `memory`, `mcp-bridge` as `tier: internal` **and** `always: true` — the `always` flag is already fully implemented and honored ([:105](backend/app/agent/skill_loader.py:105), [SettingsModal.tsx:1346](frontend/src/components/SettingsModal.tsx:1346)) and used by nothing today.
- Filter `internal` out of the Settings toggle list, leaving `browser-use`, `computer-use`, `scheduling`, `skill-creator` as the user-facing set.
- Retire `BUILT_IN_FEATURE_SLUGS` in favour of the tier. That constant is a sledgehammer: it removes the skill from discovery, which is why `mcp_bridge/SKILL.md`'s entire body never reaches the model and why an MCP bridge import failure is log-only ([:328](backend/app/agent/skill_loader.py:328)).

### 4b. Add display metadata

Row labels are the raw backend name ([SettingsModal.tsx:1335](frontend/src/components/SettingsModal.tsx:1335)) — users read `core`, `exec`, `filesystem`, `mcp-bridge`. Descriptions come from two competing sources: the frontend override map `SKILL_GUIDANCE` ([settingsConfig.ts:338](frontend/src/features/settings/settingsConfig.ts:338)) for four skills, raw engineer prose from SKILL.md for the rest — and `SKILL_GUIDANCE` still contains `background-check`, a skill that does not exist.

Add `display_name` and a one-line user-facing `summary` to SKILL.md frontmatter, then thread them through the six-hop chain — **every hop currently drops unknown fields**:

1. frontmatter → [`_split_frontmatter`](backend/app/agent/skill_loader.py:47)
2. [`SkillSpec`](backend/app/agent/skill_loader.py:28) — `slots=True`, fields must be declared
3. [`available_skill_payload`](backend/app/agent/skill_loader.py:337) — the choke point
4. [`SkillDescriptorPayload`](backend/app/schemas.py:252) — **omitting a field here silently drops it via FastAPI's `response_model`**
5. [`SkillDescriptor`](frontend/src/lib/api/types.ts:207)
6. render sites in `SettingsModal.tsx`

Then delete `SKILL_GUIDANCE` so descriptions have one authoritative source; fix the expanded row rendering `skill.description` twice ([:1357](frontend/src/components/SettingsModal.tsx:1357), [:1361](frontend/src/components/SettingsModal.tsx:1361)); and add `load_error` to `SkillDescriptorPayload` — the backend returns it and the frontend renders it ([:1366](frontend/src/components/SettingsModal.tsx:1366)), but the schema drops it in between, so a skill's import traceback never reaches the user. `formatUnavailableReason` ([settingsConfig.ts:445](frontend/src/features/settings/settingsConfig.ts:445)) also doesn't handle `load_error`.

`skill_creator._skill_markdown` ([skill_creator/tools.py:98](backend/app/skills/skill_creator/tools.py:98)) hardcodes the frontmatter keys it writes — add the new fields there so agent-created skills carry them.

### 4c. One source of truth for the skill list

Four hand-maintained, mutually inconsistent lists exist today:
- `skill_loader.BUILT_IN_FEATURE_SLUGS` — folder slugs
- `settings_store.DEFAULT_SKILLS` ([:42](backend/app/agent/settings_store.py:42)) — includes the phantom `background-check`, omits `scheduling` (patched in by a hardcoded `| {"scheduling"}` at [:396](backend/app/agent/settings_store.py:396))
- `skill_creator._PROTECTED_SKILL_NAMES` ([:26](backend/app/skills/skill_creator/tools.py:26)) — 9 entries, differs from both
- plus `REMOVED_SKILLS` and an unused `KNOWN_SKILLS`

Derive all of them from discovery at runtime. Drop `background-check` everywhere — it is also the ghost key sitting in the live `settings.json`, alongside a mix of both naming conventions.

### 4d. Naming

Canonical ids are already kebab-case via [`_normalize_skill_name`](backend/app/agent/skill_loader.py:60); the dual identity (folder `browser_use` vs name `browser-use`) is a maintenance hazard but not user-visible once display names exist. Once `exec` and `core` become `internal`, the worst names disappear from the UI — `exec` is a bare verb that collides with the tool named `exec` inside it, and `core` is a grab-bag of calculator + clock + volume + brightness + display config + web search.

One genuine capability is hidden in that grab-bag: `web_search`, registered only when `tavily_api_key` is set ([core/tools.py:66](backend/app/skills/core/tools.py:66)). Split it into its own user-facing skill so the user can see whether web search is on.

### 4e. Move per-skill prompt policy into the skills

[`skill_prompt.py`](backend/app/agent/skill_prompt.py) hardcodes `_browser_tool_policy` ([:11](backend/app/agent/skill_prompt.py:11)) and `_computer_use_tool_policy` ([:60](backend/app/agent/skill_prompt.py:60)) keyed off tool-name presence, duplicating ~80% of `browser_use/SKILL.md` verbatim — two copies that will drift. It also hardcodes MCP-Chrome guidance ([:46](backend/app/agent/skill_prompt.py:46)) for a *user-configured* server. Move the text into the owning SKILL.md files, and fix the misleading fallback `"- No optional skills enabled."` ([:88](backend/app/agent/skill_prompt.py:88)) — the list contains all enabled skills.

---

## Phase 5 — MCP: make "is it working?" answerable

### 5a. Un-blank the diagnostics

[`_safe_mcp_entry`](backend/app/api/routes/diagnostics.py:33) blanks `command`, `cwd`, `url`, `resolved_executable`, `stderr_tail` and empties `args`, `pid`, `remote_tool_names`. Meanwhile `McpDiagnostics` ([SettingsModal.tsx:2618](frontend/src/components/SettingsModal.tsx:2618)) renders rows for Command, Working directory, Executable, Process ID and a `<pre>` for stderr — **five permanently empty rows**. The stderr tail is collected into an 8 KB `_TailBuffer` ([connection.py:72](backend/app/skills/mcp_bridge/connection.py:72)) and then discarded before any human can see it, and it is the single most useful failure signal for a stdio server.

This backend is loopback-only and authenticated per request, and these are the user's own config values. Surface `stderr_tail`, `resolved_executable`, `pid`, `command`, `args` to the authenticated local UI; keep redaction for `env` values and `headers`, which can hold secrets. Update `test_mcp_runtime_routes.py:90`, which asserts the blanking today.

### 5b. Add a real health signal

There is no periodic health check — state changes only reactively, on a tool call or refresh. A stdio child that dies quietly leaves `connected=True` until the next call. There is also **no `mcp.*` UI event**: diagnostics refresh only when `settings.changed` fires or the modal mounts, so "Connected — 4 tools" can be arbitrarily stale.

- Add a liveness probe (process/transport check, or periodic `list_tools`) that moves `connected → unhealthy` when the child is gone.
- Publish `mcp.changed` on state transitions via the existing `publish_ui_event`, and subscribe in the MCP panel.
- Start enabled servers at backend boot instead of lazily inside `AgentRuntime.__init__` → `load_tools` ([runtime.py:133](backend/app/agent/runtime.py:133)). Today the first chat turn blocks up to `startup_timeout_ms` **per server**, and failures produce only a log line. Also stop discarding the status list returned by `restart_enabled_mcp_servers` ([routes/settings.py:207](backend/app/api/routes/settings.py:207)).

### 5c. Fix the trusted-tools data loss

`MCPServerConfigPayload` ([schemas.py:187](backend/app/schemas.py:187)) omits `trusted_tools` and `tool_risk_overrides`, `SettingsUpdate` is `extra="forbid"`, and [`merge_agent_settings`](backend/app/agent/settings_store.py:692) assigns lists wholesale — so **saving MCP settings from the UI silently wipes both**. `trusted_tools` is the only mechanism that lets a reflected MCP tool run without an approval prompt, and there is no UI to set it. Add both to the payload and types, add UI for `trusted_tools`, add a round-trip test.

### 5d. Correct the docs

[docs/mcp.md:133](docs/mcp.md) claims "Read-only reflected tools can run directly." False — [`registry.py:208`](backend/app/skills/mcp_bridge/registry.py:208) returns `requires_approval: True, reason: "untrusted_read_tool"`, and there is a test named `test_read_like_mcp_tool_requires_approval_without_explicit_trust`. The Server Fields table also omits the two fields that actually control approval, and `:112`/`:154` promise diagnostics the API strips.

---

## Phase 6 — Approval & gating correctness

Not user-visible until it bites, but each of these silently corrupts the security story.

- **Permanent path grants overwrite everything.** `_persist_permanent_grant` → `update_permitted_roots` ([controller_policy.py:434](backend/app/agent/controller_policy.py:434)) **regenerates the entire `path_rules` list** as `read=True, write=True, delete=True`. Granting "always" for one folder upgrades it to full RW+delete regardless of what was asked, and wipes `require_confirmation` and read-only granularity on every pre-existing rule. [docs/permissions.md:124](docs/permissions.md) says only "permanent path grants update permitted roots".
- **Resume mislabels outcomes.** [execution_resume.py:70](backend/app/agent/execution_resume.py:70) treats only `status == "error"` as failure, so a resumed tool returning `blocked`, `denied`, or `pending_*` gets `mark_applied()` — the approval log records actions as APPLIED that never ran.
- **Grant denial signalling is inconsistent.** `resolve_grant` denies `session`/`always` for non-interactive tickets ([access_grant_broker.py:232](backend/app/agent/access_grant_broker.py:232)) but the route signals the *requested* decision ([access_grants.py:53](backend/app/api/routes/access_grants.py:53)), so the gate sees a non-deny value and then fails with "Access grant invalidated: ticket is denied" instead of a clean denial.
- **Non-interactive runs have no approval story.** Scheduler and Telegram set `interactive=False` ([scheduler.py:77](backend/app/agent/scheduler.py:77), [telegram/agent_bridge.py:88](backend/app/integrations/telegram/agent_bridge.py:88)); nothing auto-denies, so a scheduled task touching `exec` blocks the full 600 s then errors with "Permission request timed out after 10 minutes." Auto-deny (or pre-authorize via profile) instead of stalling.
- **Ticket supersede can strand a waiter.** `create_ticket` supersedes a matching pending ticket and pops it from the pending index ([approval_broker.py:277](backend/app/agent/approval_broker.py:277)) without signalling its resume event — an in-flight waiter hangs until timeout.
- **`ToolPolicy` approval tables are decorative.** `ToolPolicyDecision.requires_approval` is computed ([harness/tool_policy.py:127](backend/app/agent/harness/tool_policy.py:127)) and never read; `turn_loop` consumes only `.allowed`, `.risk`, `.metadata`. `_last_results` is written ([turn_loop.py:2149](backend/app/agent/turn_loop.py:2149)) and never read. Either wire it or delete `_HIGH_RISK_TOOLS`.
- `execution_gate.py:139` hardcodes "timed out after 10 minutes" even when a custom timeout is passed.
- [docs/permissions.md:62](docs/permissions.md) omits seven entries from `BLOCKED_PROCESSES` (`cmd.exe`, `powershell.exe`, `pwsh.exe`, `wt.exe`, …) — and note the conceptual inconsistency that `powershell.exe` is an un-allowlistable app while `exec` launches PowerShell as its default shell.

---

## Phase 7 — Hygiene and dead code

- **`/skill creator` is a no-op.** [runtime.py:226](backend/app/agent/runtime.py:226) intercepts it and returns a canned string that changes no state, while `InputBar.tsx:32` advertises it. Make it enable the skill or remove the command.
- **Startup blocks on memory.** `catch_up_unprocessed_sessions` runs synchronously in `AgentRuntime.__init__` ([runtime.py:138](backend/app/agent/runtime.py:138)) — the first request after any settings change waits on LLM curation. Move it off the construction path.
- **Unbounded runtime cache.** `_runtimes` ([runtime.py:269](backend/app/agent/runtime.py:269)) gains an entry per settings permutation. Bound it (LRU 2–3) and retire evictions.
- **Layering leak.** `self.memory._db` reached into from `runtime.py:205-214`; add a `MemoryManager` method.
- **Dead code to delete:** `_legacy_resolve_permission` ([controller_policy.py:624](backend/app/agent/controller_policy.py:624)) — ~260 unreachable lines that duplicate `resolve_permission` with *different* semantics; `ActionType.POWERSHELL` / `.REGISTRY` (never passed); `DOMAINS` / `EXECUTION_MODES` ([tool_registry.py:9](backend/app/agent/tool_registry.py:9)) never referenced; `sanction_background_check` in `_NEVER_PARALLEL` ([:62](backend/app/agent/tool_registry.py:62)); `replace_where` ([:272](backend/app/agent/tool_registry.py:272)) duplicating `register()`'s normalization; `IterationBudget.fork`/`merge_child`/`refund`/`is_stalled` (stall detection is unimplemented; `set_current_tool` writes state nobody reads); `SandboxMount`/`SandboxLimits`/`SandboxSessionWriteRequest`/`SandboxSessionStopRequest`; `login_capable` in diagnostics (sniffs for `"chrome"` in the name, never read); `skill_enabled`/`skill_available` duplicate aliases of `feature_*`.
- **`AgentRunStateMachine`** is driven correctly but its state is never emitted, persisted, or asserted on — write-only instrumentation. Emit it or drop it.
- **Three conflicting iteration defaults:** `IterationBudget.max_iterations=90`, `TurnLoop.MAX_ITERATIONS=15`, `LLMSettings.max_iterations_per_turn=40` (the one actually used). Keep one.
- `job_manager.transition` treats `PAUSED` as terminal ([:30](backend/app/agent/job_manager.py:30)), so a paused job can never resume; `dropped_subscriber_events` is counted and never surfaced.
- **Duplication in exec:** `_write_artifact` exists in both [exec/tools.py:33](backend/app/skills/exec/tools.py:33) and [local_direct.py:184](backend/app/agent/sandbox/backends/local_direct.py:184); `sessions.py` and `local_restricted.py` each import five private underscore helpers from `local_direct` — move the shared helpers to a neutral module.
- **`save_output_to` is a lie.** The schema advertises a path ([exec/tools.py:585](backend/app/skills/exec/tools.py:585)) but all three runners use it as a boolean trigger and always write to `RUNTIME/exec/`. Honor it or drop it from the schema.
- **`elevated` is dead surface.** Advertised at [exec/tools.py:581](backend/app/skills/exec/tools.py:581) but blocked unconditionally at [policy.py:88](backend/app/agent/sandbox/policy.py:88), making the `risk_level="high"` branch at [:158](backend/app/skills/exec/tools.py:158) unreachable.
- **`exec/SKILL.md` is 15 lines** and documents none of the sandbox modes, blocked patterns, session tools, `return_mode`, or artifacts.
- **Branding leftovers:** `runtime.py` ("OpenClaw-style agent runtime"), `skill_loader.py`, `turn_loop.py`, `browser_use/SKILL.md`. Note `metadata.openclaw.requires` is **load-bearing** ([skill_loader.py:76](backend/app/agent/skill_loader.py:76)) — renaming it means updating every SKILL.md that uses it.
- `skill_creator/` is the only skill dir without `__init__.py`; it works only because `_load_skill_module` synthesizes a `sys.modules` entry.
- [docs/development.md:184](docs/development.md) lists 8 built-in skills, omitting `skill_creator`.

---

## Sequencing

Ordered by value per unit of risk. Phases 1–2 change what the agent can do today; the rest is consolidation.

1. **Phase 1** — `format` pattern, workspace as permitted root, chained gates, stdin bypass, process-tree kill. Small, high-impact, mostly independent. **1a is a one-line fix worth doing immediately.**
2. **Phase 2** — Docker digest + workspace mounting + probe caching, delete `wsl`, collapse selectors, metadata carry-through, honest status.
3. **Phase 3a–3c** — audit to SQLite, retention holes, stop GET mutations. Self-contained and immediately measurable.
4. **Phase 4** — internal tier, display metadata, single skill list. Mechanical once the payload chain is threaded.
5. **Phase 5** — MCP diagnostics, health events, trusted-tools fix.
6. **Phase 3d–3h, 6, 7** — retrieval quality, prompt dedup, compaction, gating correctness, cleanup.

Do not attempt 3f (compaction) before its missing test file exists. Do not pin the Docker digest (2a) without the probe cache and the `test_sandbox_docker.py` fix in the same change.

---

## Verification

Run the affected group first, then the full suite:

```bash
powershell -File scripts/test-backend.ps1 -Group sandbox
```

```bash
powershell -File scripts/test-backend.ps1 -Group memory
```

```bash
powershell -File scripts/verify.ps1
```

`verify.ps1` runs every backend group plus frontend Vitest, `tsc`, and the renderer production build. New backend test files **must** be assigned to exactly one group in [backend/conftest.py](backend/conftest.py) or CI will not run them. Regenerate the route reference with [scripts/generate-api-docs.py](scripts/generate-api-docs.py) if routes change — [docs/api.md](docs/api.md) is checked by tests.

**Tests to add — none of these paths are covered today:**
- `_BLOCKED_PATTERNS` negatives: `Get-Process | Format-Table`, `git log --format=%H`, `docker ps --format json` must all be allowed.
- The **real** `resolve_permission` path for exec. Every existing exec test monkeypatches it (`test_exec_tool.py:17,42,96,125,166`; `test_sandbox_manager.py:21`), so the "first exec always asks for a workspace grant" behavior is invisible to CI.
- Chained grant → approval resolves to an execution, not a stranded ticket.
- `exec_write_stdin` is policy-checked; sessions expire.
- Docker: a container can read and write the mounted workspace; `copy_out` collects artifacts.
- Success-path metadata: `trust_class` / `required_isolation` / `network_enforcement` / `filesystem_policy` are non-null in the audit record, using the real runner rather than `FakeManager`.
- `context_compression.py` — no test file exists; cover loop termination first.
- Retrieval: a zero-overlap memory is excluded at the default threshold; `mark_used` does not advance `updated_at`; the context-usage GET does not mutate memory.
- Auto-learn negatives: *"don't forget to check the log file"* is not stored.
- MCP: `trusted_tools` survives a settings save; a dead stdio child transitions to `unhealthy`.
- Skills: `tier: internal` is preserved (not coerced); internal skills are absent from the UI payload but present in the tool registry.

**Manual end-to-end on Windows:**

```bash
powershell -File start.bat
```

1. **Exec on a clean profile** (move `~/.monaw` aside first): ask the agent to run `Get-Process | Format-Table Name, Id`. It should execute — today it is hard-blocked. Then `Get-ChildItem` in the default workspace with no access-grant prompt, and `pip install requests` reaching an approval prompt rather than a dead end.
2. **Sandbox**: Settings → Sandbox shows the backend and isolation strength; run the resolve-digest action and confirm `auto` flips to strong isolation; confirm a Docker-run command can read a file in the workspace and its output lands back on the host. Confirm `wsl` is gone from the mode list. Time 10 consecutive `exec` calls to confirm the probe cache holds.
3. **Skills**: `core`, `exec`, `filesystem`, `memory`, `mcp-bridge` are absent from the list; remaining rows show human names and one-line summaries; the agent can still run shell and filesystem tools.
4. **MCP**: add the Filesystem template with a deliberately wrong command; the card shows a failure state **with stderr**; fix it and confirm reconnect. Save settings and confirm `trusted_tools` survives.
5. **Memory**: confirm `%USERPROFILE%\.monaw\memory\audit\` is gone; hold a few turns and confirm no new audit files appear and the memory panel loads without the multi-second stall from globbing 40k files; open a conversation twice and confirm `use_count` does not change.
6. **Startup**: restart and confirm the first chat turn is not blocked by memory catch-up or MCP server startup.