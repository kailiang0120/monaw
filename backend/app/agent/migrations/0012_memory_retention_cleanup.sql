DROP TABLE IF EXISTS memory_profile_fields;

DELETE FROM memory_episodes
WHERE conversation_id NOT IN (SELECT id FROM conversations);

CREATE TABLE memory_episodes_v2 (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    channel TEXT NOT NULL DEFAULT 'desktop',
    summary TEXT NOT NULL DEFAULT '',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    errors_json TEXT NOT NULL DEFAULT '[]',
    source_message_start_id INTEGER,
    source_message_end_id INTEGER,
    tool_call_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO memory_episodes_v2 (
    id, conversation_id, channel, summary, artifacts_json, errors_json,
    source_message_start_id, source_message_end_id, tool_call_ids_json,
    created_at, updated_at
)
SELECT
    id, conversation_id, channel, summary, artifacts_json, errors_json,
    source_message_start_id, source_message_end_id, tool_call_ids_json,
    created_at, updated_at
FROM memory_episodes;

DROP TABLE memory_episodes;
ALTER TABLE memory_episodes_v2 RENAME TO memory_episodes;

CREATE INDEX IF NOT EXISTS idx_memory_episodes_conversation
ON memory_episodes(conversation_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_memory_episodes_updated
ON memory_episodes(updated_at DESC, id);

CREATE TABLE memory_checkpoints_v2 (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'conversation',
    status TEXT NOT NULL DEFAULT 'active',
    conversation_id TEXT DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    last_known_state TEXT DEFAULT '',
    next_action TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO memory_checkpoints_v2 (
    id, scope, status, conversation_id, goal, last_known_state, next_action,
    created_at, updated_at
)
SELECT
    id, scope, status, conversation_id, goal, last_known_state, next_action,
    created_at, updated_at
FROM memory_checkpoints;

DROP TABLE memory_checkpoints;
ALTER TABLE memory_checkpoints_v2 RENAME TO memory_checkpoints;

CREATE INDEX IF NOT EXISTS idx_memory_checkpoints_scope
ON memory_checkpoints(scope, status, updated_at);

CREATE INDEX IF NOT EXISTS idx_memory_checkpoints_status_updated
ON memory_checkpoints(status, updated_at DESC, id);

DELETE FROM messages_archive
WHERE conversation_id NOT IN (SELECT id FROM conversations);

CREATE TABLE messages_archive_v2 (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    thinking TEXT DEFAULT '',
    status TEXT NOT NULL,
    response_duration_ms INTEGER,
    attachments_json TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    archived_at TEXT NOT NULL
);

INSERT INTO messages_archive_v2 (
    id, conversation_id, role, content, thinking, status,
    response_duration_ms, attachments_json, created_at, archived_at
)
SELECT
    id, conversation_id, role, content, thinking, status,
    NULL, '', created_at, archived_at
FROM messages_archive;

DROP TABLE messages_archive;
ALTER TABLE messages_archive_v2 RENAME TO messages_archive;

CREATE INDEX IF NOT EXISTS idx_messages_archive_conv
ON messages_archive(conversation_id, id);
