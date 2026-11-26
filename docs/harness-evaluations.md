# Task outcomes and harness evaluations

The harness retains whole-task outcomes separately from the execution plan for
one tool batch. Migration 0030 stores the active task, its original goal, pending
and failed outcomes, and references to returned tool evidence in SQLite. Continue
and database reopen restore that state. Older tasks remain attached to their
conversation; deleting the conversation also deletes its task records.

## Completion and evidence

The turn-local `task_plan` tool declares outcomes with stable IDs and acceptance
criteria. Partial updates merge by ID; omitting an outcome does not remove it.
The current task context is included outside summarized conversation history.
The original goal is saved before the first model request, even without a plan.
See [conversation compaction](context-compaction.md) for protected failure history,
restart behavior, and explicit budget limits.
One completion repair is allowed before an unfinished task pauses with
`task_outcomes_incomplete`. A normal completed answer needs no extra verification
model request.

`file_change` outcomes require successful evidence from the native file verifier
for every target. Paths come from the authenticated operation snapshot; copying
a file proves the destination change, not a change to its source. Tool-supplied
verification labels and invented evidence IDs cannot establish that proof. A
later recorded mutation reopens a completed outcome for the same path and
invalidates older evidence. Referenced results and the latest known mutation for
each path survive pruning.

Reads and checks use `general`; delivery uses `answer`. An unfinished outcome's
mistaken kind can be corrected with a reason while retaining its criteria and
targets. The correction is recorded. This avoids forcing repeated file edits
just to repair a planning mistake. Existing work cannot become an `answer`
outcome, including through an intermediate kind; add a separate delivery outcome.
Answer-only outcomes cannot claim file targets. Shell, MCP, and other effects without native
file verification use general outcomes and independent checks. This is task
tracking, not another permission system or proof of arbitrary semantic correctness.
The model still judges whether results satisfy the requested outcome. Changes
made outside recorded native operations are not continuously monitored.

`waiting_user` and `blocked` require an explanation. The current reply can finish
while the unfinished task remains available to the next turn. Inspecting its
saved evidence does not clear that state. An explicit replacement request can
start a new task; a resumed run cannot silently replace its original goal.
Revision and active-task checks reject stale writers.

Answer completion and the final history checkpoint commit in the same SQLite
transaction. A required task write failure rolls back both and reports an error.
One idempotent error-checkpoint attempt preserves the failed turn when storage
allows it, without repeating tools or marking pending answer outcomes done.

Each task has at most 32 outcomes and a 256 KB stored snapshot. It retains 64
recent unreferenced tool results plus referenced evidence and latest mutations.
Only eight recent results are injected automatically into model context; calling
`task_plan` with `steps=[]` returns the saved evidence. Stored evidence includes
digests and identifiers, not another copy of file contents.

## Unchanged-result loops

The progress monitor compares read and search results after removing known
transport timestamps and request metadata. Alternating between already-seen
observations does not count as new information. Rephrasing an empty search also
does not reset progress. Internal plan calls compare the resulting task state,
not variations in their input arguments.

Three consecutive unchanged rounds produce one recovery hint; six pause the turn
with `no_progress`. Trusted polling tools such as `exec_poll` get six rounds
before the hint and twelve before pausing. New information or an actual action
resets the stall count. User steering resets the observation window. Existing
permission, repeat-action, token, time, and iteration guards still apply. The
cache holds 128 observations per turn, so these are bounded heuristics rather
than universal detection of unproductive behavior.

## Run the six-task suite

From the repository root, after installing backend dependencies:

```powershell
uv run --directory backend --locked python ../scripts/evaluate-harness.py
```

The default fixture model makes no provider requests. It emits tool calls through
the real turn loop, permissions, native file tools, SQLite, and durable operation
verifier. The grader examines resulting files and task state independently of the
model's final claim. It runs in the existing runtime CI group; it adds no CI job.

| Case | Required result |
| --- | --- |
| `files` | Three exact files, read back after writing, all outcomes completed, no duplicate writes. |
| `missing_information` | Wait without creating a file, then finish the original task when the user supplies its value. |
| `tool_failure` | Handle a real missing-file read error and produce the requested fallback file. |
| `pause_resume` | Pause after a returned write, reopen SQLite, resume the saved attempt, and finish without repeating writes. |
| `long_context` | Compact oversized history while retaining outcomes and finishing the exact file task. |
| `no_progress` | Stop a deliberately unchanged lookup loop with the progress guard. A live model may instead stop and explain the blocker itself. |

Select cases and an output destination with:

```powershell
uv run --directory backend --locked python ../scripts/evaluate-harness.py --cases files pause_resume --output ../harness-results.json
```

For an explicitly authorized live trial, supply `GOOGLE_API_KEY` or
`GEMINI_API_KEY` through the process environment, then run:

```powershell
uv run --directory backend --locked python ../scripts/evaluate-harness.py --provider gemini
```

The default Gemini model is `gemini-flash-lite-latest`. OpenAI trials require
`OPENAI_API_KEY` and `--provider openai`; this runner permits only `gpt-6-luna`.
Live trials incur provider usage. CI never selects a live provider.

Each invocation creates a disposable `monaw-harness-eval-*` profile under system
Temp. Tools are restricted to that trial's workspace and a fixed empty lookup
fixture; shell execution, browsers, MCP, and external search are not exposed.
The credentials stay in memory. Reports include synthetic workspace locations
for inspection, never API keys. Profiles remain available after execution so
failed task state and native operation evidence can be examined.

JSON results contain per-case checks, turn outcomes, task-directed model requests,
compaction/helper requests, redundant calls, tool calls, token usage, and elapsed
time. Token totals include compaction and failed-request accounting from the real
turn ledger; `estimated_requests` identifies estimates when usage is unavailable.
Fixture tokens are `null`. Task calls and summary calls are counted separately
even when a provider's summary helper delegates to its tool-capable API. A failed
check produces a nonzero exit status. These small trials catch regressions; they
do not establish a general success rate or replace the desktop and provider
contract suites.
