-- Persist Run control request claims and exact successful HTTP responses.
-- An orphaned pending/uncertain row is never replayed; the client must inspect
-- the durable Run state before issuing a new command with a new key.
CREATE TABLE run_command_idempotency (
    principal_hash TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('pause', 'resume', 'stop', 'resolve')),
    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    resource_id TEXT NOT NULL DEFAULT '',
    idempotency_key_hash TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'completed', 'uncertain')),
    response_json TEXT,
    claim_owner TEXT,
    claim_lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (principal_hash, action, run_id, resource_id, idempotency_key_hash),
    CHECK (
        (state = 'completed' AND response_json IS NOT NULL)
        OR (state IN ('pending', 'uncertain') AND response_json IS NULL)
    )
);

CREATE INDEX idx_run_command_idempotency_run
ON run_command_idempotency(run_id, updated_at);
