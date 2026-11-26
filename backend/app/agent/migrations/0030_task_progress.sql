-- Retain task outcomes independently of individual model turns and tool batches.
CREATE TABLE task_progress (
    task_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX task_progress_active ON task_progress(conversation_id) WHERE active = 1;
