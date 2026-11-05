-- Existing checkpoints cannot be safely backfilled from message ordering.
ALTER TABLE turn_checkpoints ADD COLUMN assistant_message_id INTEGER;
