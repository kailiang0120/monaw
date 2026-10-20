# Scheduling

Monaw can run local scheduled agent tasks. A scheduled task is a saved prompt plus a schedule.

## What Scheduling Supports

| Schedule Kind | Meaning | Required Field |
| --- | --- | --- |
| `cron` | Runs on a five-field cron expression. | `cronExpr` / `cron_expr` |
| `interval` | Runs every N seconds. | `intervalSeconds` / `interval_seconds` |
| `once` | Runs once at a specific timestamp. | `runAt` / `run_at` |

Schedules store a timezone. If a timezone is invalid, runtime calculation falls back to UTC.

## Where To Use It

Scheduled tasks are available through the scheduling UI and through the hidden scheduling tools. The scheduling skill is always on; its tools stay hidden until tool search surfaces them when the user asks for reminders, recurring work, cron jobs, or automation.

The backend service starts during FastAPI startup. It polls SQLite for due work every 30 seconds.

## Task Fields

| Field | Meaning |
| --- | --- |
| `title` | Short task name. |
| `prompt` | Prompt Monaw runs when the task fires. |
| `scheduleKind` | `cron`, `interval`, or `once`. |
| `cronExpr` | Five-field cron expression for cron schedules. |
| `intervalSeconds` | Interval length for interval schedules. |
| `runAt` | ISO timestamp for one-time schedules. |
| `timezone` | Timezone used for cron and naive timestamps. |
| `enabled` | Whether the task can run. |
| `overlapPolicy` | What to do when the previous run is still active. |
| `reuseConversation` | Whether every run should use one shared scheduled-task conversation instead of creating a fresh chat. |
| `notifyTelegram` | Whether to send a Telegram result message. |
| `telegramChatId` | Telegram chat id to notify. |

## Overlap Policies

| Policy | Behavior |
| --- | --- |
| `skip` | If a previous run is still active, mark this occurrence as skipped and move to the next schedule. |
| `queue` | Accept one pending occurrence for this task, then start it after the previous execution resources are available. Further occurrences are skipped while that pending slot is occupied. |
| `cancel_previous` | Request cancellation of active runs and replace pending work. Start the replacement after the previous execution resources are available. |

Manual "run now" uses replacement semantics and obeys the same capacity checks.
Requesting cancellation does not prove that a native/threaded worker has stopped;
a replacement waits while such a worker remains unfinished.

Default limits are four active executions globally, one execution per task, one
pending occurrence per task, and sixteen pending occurrences globally. Queued
work does not occupy an execution slot. Overflow is recorded as a skipped run
with a reason. A rejected manual request does not cancel existing work.

## Run Lifecycle

When a task fires, Monaw:

1. Atomically accepts the occurrence, stores its input snapshot and unique identity, creates its conversation, and advances the next occurrence. Skipped occurrences have history but no conversation.
2. Acquires an execution slot before changing queued work to running.
3. Runs the accepted prompt through the shared agent runtime, using the accepted principal and restricted permission profile.
4. Stores the bounded final result and updates the task summary once, preserving current configuration and the already-advanced cadence.
5. Discards the temporary input snapshot when the attempt becomes terminal.
6. Optionally sends a Telegram notification after the result commits.

Run-history statuses and task-summary values are distinct:

| Run-history status | Meaning / task summary |
| --- | --- |
| `queued` | Accepted, execution has not started. The API exposes a separate `queued` flag. |
| `running` | An execution slot is reserved. The API exposes `running` independently from queued work. |
| `succeeded` | Completed successfully; task summary is `ok`. |
| `failed` | Execution failed or was incomplete; task summary is `error`. |
| `cancelled` | Cancelled or replaced; distinct from a skipped occurrence. |
| `skipped` | Not executed; `error` records overlap, queue overflow, or resource-limit reason. Legacy summary reason strings are retained. |
| `dead_letter` | Legacy representation for failure-threshold suspension, retained for compatibility. |
| `abandoned` | A running lease expired; its uncertain action is not replayed. |

After 5 consecutive `error` runs, Monaw disables the task.

An overdue one-time occurrence with intact due intent can fire once. A manual run
before its future date does not consume that future occurrence. Legacy cleared
claims with uncertain history are repaired to a future recurring occurrence, or
disabled when no future one-time occurrence exists.

## Changes During a Run

Disabling a task stops future automatic dispatch; it does not stop an already
executing run. Queued work checks that the task still exists and is enabled
before starting. Use cancellation separately when an executing action must stop.

Completion uses the currently saved schedule and timezone. Disabling a task,
changing an interval, or converting a one-time task to a recurring task while a
run is active is preserved when that run finishes. Configuration edits carry an
internal revision; a concurrent stale API update returns `409` and should be
reloaded before saving again.

Each accepted run keeps its prompt, principal and notification configuration.
Editing the destination affects future accepted runs; the Telegram allowlist is
checked again immediately before delivery. Completion is committed once. An
older attempt can retain its own result without overwriting a newer attempt's
task summary. Deleted tasks are not recreated by late completion.

Only the latest executing/completed attempt updates the task summary and failure counter;
later queued or skipped occurrences do not hide its result.
Unrelated configuration edits preserve that counter. A successful latest attempt
resets it; a failed latest attempt increments it and disables future dispatch at
five consecutive failures. Re-enabling alone does not reset the failure counter.

Running attempts renew their leases while owned by the scheduler. Queued work is
durable: a clean shutdown releases it for another scheduler; after an unclean
shutdown another scheduler can adopt its expired lease. Adoption atomically
changes queued to running. Interrupted running work is marked abandoned after
lease expiry and is never automatically replayed. Expired or recovered running
attempts cannot restart or finalize under their old lease.
These ownership checks do not imply that an interrupted native action has
stopped; uncertain effects must be verified before retrying.

## Security Notes

Scheduled tasks are non-interactive source principals. They run with the saved
owner principal and restricted scheduled permission profile snapshot, recover
expired leases on restart, and should not receive broad persistent grants
without local review. See [operations.md](operations.md) for source-specific
restrictions, backup, retention, deletion, and incident guidance.

## Cron Notes

Cron expressions are five-field cron expressions:

```text
minute hour day-of-month month day-of-week
```

Examples:

| Expression | Meaning |
| --- | --- |
| `0 9 * * *` | Every day at 9:00. |
| `*/30 * * * *` | Every 30 minutes. |
| `0 18 * * 1-5` | Weekdays at 18:00. |

Cron schedules are evaluated in the task timezone and stored as UTC next-run timestamps.
Nonexistent local times during a forward clock change are skipped. Ambiguous
local times during a backward clock change run once, using the first occurrence.
Preview and dispatch use the same calculation.

## Interval Notes

Interval schedules require `intervalSeconds` greater than zero.

For a newly created interval task, the initial next run is delayed by
`min(60, intervalSeconds)` seconds. Subsequent occurrences follow a fixed cadence
anchored to that due time. Completion does not introduce an additional delay.
After downtime, missed recurring occurrences coalesce into at most the latest
eligible occurrence; no unbounded historical backlog is generated. History marks
whether coalescing occurred internally.

## Telegram Notifications

If `notifyTelegram` is enabled and `telegramChatId` is set, Monaw sends the task title and result to that Telegram chat after non-skipped runs.

Telegram notification requires the Telegram bot service to be running.

Notifications use the execution outcome: success delivers the result, or an
explicit successful-with-no-output message. Cancelled and skipped runs do not
send notifications. A delivery exception is logged separately with task/run IDs
and exception type; it does not change the completed task outcome or rerun the
agent. Durable notification retry is not implemented.

## API Surface

The backend exposes scheduling endpoints under `/api/scheduled-tasks`:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/scheduled-tasks` | List tasks. |
| `POST /api/scheduled-tasks` | Create a task. |
| `PATCH /api/scheduled-tasks/{task_id}` | Update a task. |
| `DELETE /api/scheduled-tasks/{task_id}` | Delete a task. |
| `POST /api/scheduled-tasks/{task_id}/run` | Accept a manual run and return its run/conversation identifiers. |
| `GET /api/scheduled-tasks/{task_id}/operations/{operation_key}` | Reconcile an accepted manual request for the authenticated control session. |
| `GET /api/scheduled-tasks/{task_id}/runs` | List run history. |
| `POST /api/scheduled-tasks/preview` | Preview the next schedule times. |
| `GET /api/scheduled-tasks/telegram-chats` | List Telegram chats known to the session store. |

For safe manual retries, send the same `Idempotency-Key` header for the same
intended run. A duplicate returns the original run/conversation identifiers
without cancelling or starting anything again. The key is scoped to task and
authenticated control session. A fresh explicit run uses a fresh key. The
desktop client shares pending intentions between the dialog and sidebar and
checks an uncertain operation before retrying acceptance with its original key.
This client cache survives backend reconnects within the current renderer;
reloading the renderer clears it and does not automatically replay requests.

Opaque request identities remain after ordinary run-history retention to prevent
old requests being executed again. Deleting the task removes those identities.
No prompt or result content is retained in that request index.

## Tool Surface

The scheduling skill exposes hidden tools that can be dynamically surfaced:

| Tool | Purpose |
| --- | --- |
| `scheduled_task_create` | Create a cron, interval, or once task. |
| `scheduled_task_list` | List configured tasks. |
| `scheduled_task_delete` | Delete a task by id. |
| `scheduled_task_run_now` | Run a task immediately by id. |

These tools mutate local state and are not parallel-safe.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Task never runs. | Confirm `enabled`, `nextRunAt`, schedule kind fields, and backend service status. |
| Cron task fires at wrong local time. | Check the task timezone. |
| One-time task disappears from future runs. | One-time tasks disable after running or when their scheduled time is in the past. |
| Repeated failures disable task. | Inspect run history; task disables after 5 consecutive errors. |
| Telegram notification does not send. | Confirm Telegram bot is running and `telegramChatId` matches an allowed chat. |
| Run appears skipped. | Check overlap policy and whether a previous run was still active. |
| API says service not running. | Backend scheduler startup failed; check `%USERPROFILE%\.monaw\runtime\backend.log`. |
