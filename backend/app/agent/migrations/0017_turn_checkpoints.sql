CREATE TABLE turn_checkpoints (
    turn_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_message_ids TEXT NOT NULL
);
