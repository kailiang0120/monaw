ALTER TABLE scheduled_tasks ADD COLUMN workspace_dir TEXT NOT NULL DEFAULT '';
ALTER TABLE scheduled_tasks ADD COLUMN model_provider TEXT NOT NULL DEFAULT '';
ALTER TABLE scheduled_tasks ADD COLUMN model_name TEXT NOT NULL DEFAULT '';
ALTER TABLE scheduled_tasks ADD COLUMN model_reasoning_effort TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_conversations_activity_cursor
    ON conversations (updated_at DESC, id DESC);

CREATE TABLE scheduled_notifications (
    run_id INTEGER PRIMARY KEY REFERENCES scheduled_task_runs(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_chunk INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_scheduled_notifications_due
    ON scheduled_notifications (status, next_attempt_at);
