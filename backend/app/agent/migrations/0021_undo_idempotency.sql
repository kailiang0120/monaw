-- One inverse operation per verified source effect. The per-principal key
-- table binds retries to the exact source operation and request payload.
CREATE UNIQUE INDEX idx_run_operations_undo_source
ON run_operations(related_operation_id)
WHERE operation_kind = 'undo'
  AND related_operation_id IS NOT NULL
  AND status NOT IN ('failed', 'cancelled');

CREATE TABLE run_undo_idempotency (
    principal_hash TEXT NOT NULL,
    idempotency_key_hash TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    undo_operation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (principal_hash, idempotency_key_hash),
    UNIQUE (undo_operation_id),
    FOREIGN KEY (undo_operation_id, run_id)
        REFERENCES run_operations(operation_id, run_id) ON DELETE CASCADE
);

CREATE INDEX idx_run_undo_idempotency_run
ON run_undo_idempotency(run_id, created_at, undo_operation_id);
