-- Wall-clock timestamps cannot establish whether a result was checkpointed.
-- Leave legacy values unknown: never infer authority to resume opaque effects.
ALTER TABLE turn_checkpoints ADD COLUMN operation_result_seq INTEGER;
ALTER TABLE run_operations ADD COLUMN returned_event_seq INTEGER;
