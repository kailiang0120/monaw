CREATE TABLE IF NOT EXISTS memory_profile_fields (
    field TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    privacy_level TEXT NOT NULL DEFAULT 'normal',
    confidence REAL NOT NULL DEFAULT 1.0,
    review_state TEXT NOT NULL DEFAULT 'new',
    source_conversation_id TEXT DEFAULT '',
    source_message_id INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_candidates (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'fact',
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'fact',
    confidence REAL NOT NULL DEFAULT 0.8,
    importance INTEGER NOT NULL DEFAULT 5,
    status TEXT NOT NULL DEFAULT 'new',
    reason TEXT DEFAULT '',
    source_conversation_id TEXT DEFAULT '',
    source_message_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_candidates_status
ON memory_candidates(status, updated_at);

CREATE TABLE IF NOT EXISTS memory_episodes (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'desktop',
    project TEXT DEFAULT '',
    task_type TEXT DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    decisions_json TEXT NOT NULL DEFAULT '[]',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    errors_json TEXT NOT NULL DEFAULT '[]',
    fixes_json TEXT NOT NULL DEFAULT '[]',
    open_questions_json TEXT NOT NULL DEFAULT '[]',
    follow_ups_json TEXT NOT NULL DEFAULT '[]',
    source_message_start_id INTEGER,
    source_message_end_id INTEGER,
    tool_call_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_episodes_conversation
ON memory_episodes(conversation_id, updated_at);

CREATE TABLE IF NOT EXISTS memory_checkpoints (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'conversation',
    status TEXT NOT NULL DEFAULT 'active',
    conversation_id TEXT DEFAULT '',
    project TEXT DEFAULT '',
    app_name TEXT DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    last_known_state TEXT DEFAULT '',
    next_action TEXT DEFAULT '',
    blocker TEXT DEFAULT '',
    browser_url TEXT DEFAULT '',
    browser_title TEXT DEFAULT '',
    workspace_path TEXT DEFAULT '',
    files_touched_json TEXT NOT NULL DEFAULT '[]',
    commands_run_json TEXT NOT NULL DEFAULT '[]',
    expires_at TEXT DEFAULT '',
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_checkpoints_scope
ON memory_checkpoints(scope, status, updated_at);
