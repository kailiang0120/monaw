"""SQLite storage primitives for observability."""

from __future__ import annotations

import sqlite3

OBSERVABILITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS observability_runs (
    run_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    message_id TEXT DEFAULT '',
    source TEXT DEFAULT 'desktop',
    model TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    status TEXT DEFAULT 'running',
    failure_reason TEXT DEFAULT '',
    failure_pattern TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0,
    user_message TEXT DEFAULT '',
    final_output TEXT DEFAULT '',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    image_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    usage_source TEXT DEFAULT 'unknown',
    estimated_cost_usd REAL DEFAULT 0,
    cost_source TEXT DEFAULT 'unknown',
    tool_count INTEGER DEFAULT 0,
    tool_error_count INTEGER DEFAULT 0,
    event_count INTEGER DEFAULT 0,
    metadata_json TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS observability_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT DEFAULT '',
    conversation_id TEXT DEFAULT '',
    message_id TEXT DEFAULT '',
    event_type TEXT NOT NULL,
    level TEXT DEFAULT 'info',
    status TEXT DEFAULT '',
    source TEXT DEFAULT '',
    model TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    tool_name TEXT DEFAULT '',
    error_code TEXT DEFAULT '',
    error_message TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0,
    input_json TEXT DEFAULT '',
    output_json TEXT DEFAULT '',
    tokens_json TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observability_errors (
    error_id TEXT PRIMARY KEY,
    run_id TEXT DEFAULT '',
    conversation_id TEXT DEFAULT '',
    message_id TEXT DEFAULT '',
    level TEXT DEFAULT 'error',
    logger_name TEXT DEFAULT '',
    module TEXT DEFAULT '',
    error_type TEXT DEFAULT '',
    message TEXT DEFAULT '',
    traceback TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observability_replays (
    replay_id TEXT PRIMARY KEY,
    source_run_id TEXT NOT NULL,
    replay_run_id TEXT DEFAULT '',
    conversation_id TEXT DEFAULT '',
    status_change TEXT DEFAULT '',
    duration_delta_ms INTEGER DEFAULT 0,
    token_delta INTEGER DEFAULT 0,
    tool_sequence_diff TEXT DEFAULT '',
    failure_reason_diff TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_runs_started ON observability_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_runs_status ON observability_runs(status);
CREATE INDEX IF NOT EXISTS idx_obs_runs_conversation ON observability_runs(conversation_id);
CREATE INDEX IF NOT EXISTS idx_obs_events_run ON observability_events(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_obs_errors_created ON observability_errors(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_errors_run ON observability_errors(run_id);
"""


def initialize_observability_db(conn: sqlite3.Connection) -> None:
    conn.executescript(OBSERVABILITY_SCHEMA_SQL)
