CREATE TABLE agent_runs (
    run_id TEXT NOT NULL PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    workspace_dir TEXT NOT NULL DEFAULT '',
    execution_source TEXT NOT NULL,
    owner_principal_id TEXT,
    control_session_id TEXT,
    owner_instance_id TEXT,
    lease_expires_at TEXT,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    status TEXT NOT NULL CHECK (
        status IN (
            'running',
            'waiting_approval',
            'pausing',
            'paused',
            'interrupted',
            'needs_review',
            'completed',
            'failed',
            'cancelled'
        )
    ),
    reason_code TEXT,
    turn_id TEXT,
    recovery_input_ref TEXT,
    last_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    CHECK (
        (owner_instance_id IS NULL AND lease_expires_at IS NULL)
        OR (
            owner_instance_id IS NOT NULL
            AND length(trim(owner_instance_id)) > 0
            AND lease_expires_at IS NOT NULL
            AND length(trim(lease_expires_at)) > 0
        )
    )
);

CREATE INDEX idx_agent_runs_conversation_created
ON agent_runs(conversation_id, created_at DESC, run_id);

CREATE INDEX idx_agent_runs_owner_session_created
ON agent_runs(owner_principal_id, control_session_id, created_at DESC);

CREATE INDEX idx_agent_runs_status_updated
ON agent_runs(status, updated_at DESC, run_id);

CREATE INDEX idx_agent_runs_recovery_lease
ON agent_runs(status, lease_expires_at, owner_instance_id);

CREATE UNIQUE INDEX idx_agent_runs_turn_id
ON agent_runs(turn_id)
WHERE turn_id IS NOT NULL;

CREATE TABLE run_attempts (
    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    turn_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, attempt)
);
