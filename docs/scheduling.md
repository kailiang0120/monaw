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

Scheduled tasks are available through the scheduling UI and through the hidden scheduling tools. The scheduling skill is optional and can be surfaced by tool search when the user asks for reminders, recurring work, cron jobs, or automation.

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
| `queue` | Wait for the previous run, then run this occurrence. Cancelling the queued wrapper does not cancel the active predecessor. |
| `cancel_previous` | Cancel active runs for the same task and start the new occurrence. |

Manual "run now" cancels active runs for that task before starting the manual run.

## Run Lifecycle

When a task fires, Monaw:

1. Creates a conversation for the run, or reuses `sched_<task>_shared` when `reuseConversation` is enabled.
2. Creates a run history row.
3. Runs the saved prompt through the same agent runtime used by desktop chat.
4. Streams the final text into run history.
5. Computes the next run.
6. Updates task state, last run status, and consecutive failure count.
7. Optionally sends a Telegram notification.

Run statuses include:

| Status | Meaning |
| --- | --- |
| `ok` | The run completed successfully. |
| `error` | The run failed or ended incomplete. |
| `skipped` | The run was skipped due to overlap or cancellation. |
| `disabled_after_failures` | The task was disabled after repeated errors. |

After 5 consecutive `error` runs, Monaw disables the task.

One-time tasks disable themselves after they run or after their scheduled time can no longer fire.

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

## Interval Notes

Interval schedules require `intervalSeconds` greater than zero.

For a newly created interval task, the initial next run is delayed by `min(60, intervalSeconds)` seconds. After that, next runs are calculated from the last run time.

## Telegram Notifications

If `notifyTelegram` is enabled and `telegramChatId` is set, Monaw sends the task title and result to that Telegram chat after non-skipped runs.

Telegram notification requires the Telegram bot service to be running.

## API Surface

The backend exposes scheduling endpoints under `/api/scheduled-tasks`:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/scheduled-tasks` | List tasks. |
| `POST /api/scheduled-tasks` | Create a task. |
| `PATCH /api/scheduled-tasks/{task_id}` | Update a task. |
| `DELETE /api/scheduled-tasks/{task_id}` | Delete a task. |
| `POST /api/scheduled-tasks/{task_id}/run` | Run a task immediately. |
| `GET /api/scheduled-tasks/{task_id}/runs` | List run history. |
| `POST /api/scheduled-tasks/preview` | Preview the next schedule times. |
| `GET /api/scheduled-tasks/telegram-chats` | List Telegram chats known to the session store. |

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
