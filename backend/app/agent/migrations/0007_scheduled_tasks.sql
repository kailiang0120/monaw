CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    schedule_kind TEXT NOT NULL,
    cron_expr TEXT DEFAULT '',
    interval_seconds INTEGER DEFAULT 0,
    run_at TEXT DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    enabled INTEGER NOT NULL DEFAULT 1,
    overlap_policy TEXT NOT NULL DEFAULT 'skip',
    notify_telegram INTEGER NOT NULL DEFAULT 0,
    telegram_chat_id TEXT DEFAULT '',
    last_run_at TEXT DEFAULT '',
    last_run_status TEXT DEFAULT '',
    last_run_conversation_id TEXT DEFAULT '',
    next_run_at TEXT DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_enabled_next
ON scheduled_tasks(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS scheduled_task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    final_text TEXT DEFAULT '',
    error TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_task
ON scheduled_task_runs(task_id, id DESC);
