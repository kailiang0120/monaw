ALTER TABLE scheduled_tasks ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scheduled_task_runs ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 0;
