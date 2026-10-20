ALTER TABLE scheduled_task_runs ADD COLUMN task_snapshot TEXT NOT NULL DEFAULT '';
ALTER TABLE scheduled_task_runs ADD COLUMN manual INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scheduled_task_runs ADD COLUMN scheduled_for TEXT NOT NULL DEFAULT '';
ALTER TABLE scheduled_task_runs ADD COLUMN coalesced INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scheduled_task_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_scheduled_pending ON scheduled_task_runs(status, id);
CREATE TABLE scheduled_run_requests (
    task_id TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    operation_key TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    conversation_id TEXT NOT NULL,
    PRIMARY KEY (task_id, operation_key)
);
INSERT INTO scheduled_run_requests (task_id, operation_key, run_id, conversation_id)
SELECT task_id, idempotency_key, id, conversation_id FROM scheduled_task_runs
WHERE idempotency_key LIKE 'manual:%';
