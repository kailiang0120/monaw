-- Opaque provider state stays separate from public message text and attachments.
ALTER TABLE messages ADD COLUMN provider_history_json TEXT NOT NULL DEFAULT '';
ALTER TABLE messages_archive ADD COLUMN provider_history_json TEXT NOT NULL DEFAULT '';
