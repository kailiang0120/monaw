CREATE TABLE run_operations (
    operation_id TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    sequence_no INTEGER NOT NULL CHECK (sequence_no >= 1),
    tool_name TEXT NOT NULL,
    operation_kind TEXT NOT NULL CHECK (
        operation_kind IN (
            'file_move',
            'file_write',
            'file_delete',
            'file_copy',
            'file_mkdir',
            'file_mutation',
            'undo',
            'shell_command',
            'browser_action',
            'mcp_call',
            'tool_call',
            'other'
        )
    ),
    effect_class TEXT NOT NULL CHECK (
        effect_class IN ('read_only', 'verifiable_write', 'opaque_effect')
    ),
    status TEXT NOT NULL CHECK (
        status IN (
            'prepared',
            'authorized',
            'executing',
            'verified',
            'failed',
            'cancelled',
            'uncertain',
            'needs_review',
            'returned'
        )
    ),
    reason_code TEXT,
    related_operation_id TEXT,
    input_summary_json TEXT NOT NULL DEFAULT '{}',
    precondition_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    snapshot_ref TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, sequence_no),
    UNIQUE (operation_id, run_id),
    FOREIGN KEY (run_id, attempt)
        REFERENCES run_attempts(run_id, attempt) ON DELETE CASCADE,
    FOREIGN KEY (related_operation_id, run_id)
        REFERENCES run_operations(operation_id, run_id),
    CHECK (related_operation_id IS NULL OR related_operation_id != operation_id),
    CHECK (status != 'returned' OR effect_class = 'opaque_effect')
);

CREATE INDEX idx_run_operations_run_status_sequence
ON run_operations(run_id, status, sequence_no);

CREATE INDEX idx_run_operations_run_attempt_sequence
ON run_operations(run_id, attempt, sequence_no);
