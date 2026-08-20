CREATE TABLE IF NOT EXISTS memory_audit (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    memory_id TEXT,
    source_conversation_id TEXT NOT NULL DEFAULT '',
    candidate_content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_audit_created
ON memory_audit(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_audit_conversation_created
ON memory_audit(source_conversation_id, created_at);

CREATE INDEX IF NOT EXISTS idx_memory_audit_memory
ON memory_audit(memory_id, created_at);

CREATE TABLE IF NOT EXISTS memory_audit_counters (
    action TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);
