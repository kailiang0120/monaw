CREATE TABLE IF NOT EXISTS messages_archive (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    thinking TEXT DEFAULT '',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    archived_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_archive_conv
ON messages_archive(conversation_id, id);
