"""SQLite schema bootstrap and forward-compatible upgrades."""

from __future__ import annotations

import sqlite3

from app.agent.migrations import apply_migrations

SCHEMA_VERSION = 10

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'Untitled',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    summary TEXT DEFAULT '',
    task_goal TEXT DEFAULT '',
    context_tokens_estimate INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    thinking TEXT DEFAULT '',
    status TEXT DEFAULT 'complete',
    response_duration_ms INTEGER,
    attachments_json TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    input TEXT DEFAULT '',
    output TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_msg ON tool_calls(message_id);

CREATE TABLE IF NOT EXISTS tool_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    result TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    identifier TEXT NOT NULL,
    grant_type TEXT NOT NULL,
    use_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(target_type, identifier)
);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    schedule_kind TEXT NOT NULL,
    cron_expr TEXT DEFAULT '',
    interval_seconds INTEGER DEFAULT 0,
    run_at TEXT DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    enabled INTEGER NOT NULL DEFAULT 1,
    overlap_policy TEXT NOT NULL DEFAULT 'skip',
    notify_telegram INTEGER NOT NULL DEFAULT 0,
    telegram_chat_id TEXT DEFAULT '',
    reuse_conversation INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT DEFAULT '',
    last_run_status TEXT DEFAULT '',
    last_run_conversation_id TEXT DEFAULT '',
    next_run_at TEXT DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_enabled_next
ON scheduled_tasks(enabled, next_run_at);

CREATE TABLE IF NOT EXISTS scheduled_task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    conversation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    lease_owner TEXT DEFAULT '',
    lease_expires_at TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    final_text TEXT DEFAULT '',
    error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_task
ON scheduled_task_runs(task_id, id DESC);

CREATE TABLE IF NOT EXISTS conversation_compactions (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    summary TEXT NOT NULL DEFAULT '',
    source_message_id INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    tokens_before INTEGER NOT NULL DEFAULT 0,
    tokens_after INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_conversation_columns(conn: sqlite3.Connection) -> None:
    column_defs = {
        "summary": "TEXT DEFAULT ''",
        "task_goal": "TEXT DEFAULT ''",
        "context_tokens_estimate": "INTEGER DEFAULT 0",
    }
    existing = _columns(conn, "conversations")
    for column, definition in column_defs.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE conversations ADD COLUMN {column} {definition}")


def ensure_message_columns(conn: sqlite3.Connection) -> None:
    column_defs = {
        "thinking": "TEXT DEFAULT ''",
        "status": "TEXT DEFAULT 'complete'",
        "response_duration_ms": "INTEGER",
        "attachments_json": "TEXT DEFAULT ''",
    }
    existing = _columns(conn, "messages")
    for column, definition in column_defs.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
    conn.execute(
        """
        UPDATE messages
        SET status = 'paused'
        WHERE role = 'assistant'
          AND status = 'complete'
          AND (
            content LIKE 'Paused:%'
            OR content LIKE 'Paused before executing more tools%'
            OR content LIKE 'Stopped: repeated browser action%'
          )
        """
    )


def ensure_archive_message_columns(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "messages_archive")
    if not existing:
        return
    if "response_duration_ms" not in existing:
        conn.execute("ALTER TABLE messages_archive ADD COLUMN response_duration_ms INTEGER")
    if "attachments_json" not in existing:
        conn.execute("ALTER TABLE messages_archive ADD COLUMN attachments_json TEXT DEFAULT ''")


def ensure_scheduled_task_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            schedule_kind TEXT NOT NULL,
            cron_expr TEXT DEFAULT '',
            interval_seconds INTEGER DEFAULT 0,
            run_at TEXT DEFAULT '',
            timezone TEXT NOT NULL DEFAULT 'UTC',
            enabled INTEGER NOT NULL DEFAULT 1,
            overlap_policy TEXT NOT NULL DEFAULT 'skip',
            notify_telegram INTEGER NOT NULL DEFAULT 0,
            telegram_chat_id TEXT DEFAULT '',
            reuse_conversation INTEGER NOT NULL DEFAULT 0,
            owner_principal_id TEXT DEFAULT '',
            permission_profile_id TEXT DEFAULT 'scheduled-restricted',
            permission_profile_snapshot TEXT DEFAULT '',
            last_run_at TEXT DEFAULT '',
            last_run_status TEXT DEFAULT '',
            last_run_conversation_id TEXT DEFAULT '',
            next_run_at TEXT DEFAULT '',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_enabled_next
        ON scheduled_tasks(enabled, next_run_at);

        CREATE TABLE IF NOT EXISTS scheduled_task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
            conversation_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL DEFAULT '',
            lease_owner TEXT DEFAULT '',
            lease_expires_at TEXT DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'running',
            final_text TEXT DEFAULT '',
            error TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_task
        ON scheduled_task_runs(task_id, id DESC);
        """
    )
    existing = _columns(conn, "scheduled_tasks")
    if "reuse_conversation" not in existing:
        conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN reuse_conversation INTEGER NOT NULL DEFAULT 0")
    if "owner_principal_id" not in existing:
        conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN owner_principal_id TEXT DEFAULT ''")
    if "permission_profile_id" not in existing:
        conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN permission_profile_id TEXT DEFAULT 'scheduled-restricted'")
    if "permission_profile_snapshot" not in existing:
        conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN permission_profile_snapshot TEXT DEFAULT ''")

    run_existing = _columns(conn, "scheduled_task_runs")
    if "idempotency_key" not in run_existing:
        conn.execute("ALTER TABLE scheduled_task_runs ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''")
    if "lease_owner" not in run_existing:
        conn.execute("ALTER TABLE scheduled_task_runs ADD COLUMN lease_owner TEXT DEFAULT ''")
    if "lease_expires_at" not in run_existing:
        conn.execute("ALTER TABLE scheduled_task_runs ADD COLUMN lease_expires_at TEXT DEFAULT ''")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_task_runs_idempotency
        ON scheduled_task_runs(task_id, idempotency_key)
        WHERE idempotency_key <> ''
        """
    )


def migrate_bootstrap_schema(conn: sqlite3.Connection, current_version: int) -> None:
    if current_version < 2:
        ensure_conversation_columns(conn)
    if current_version < 3:
        ensure_message_columns(conn)
    ensure_scheduled_task_tables(conn)


def initialize_database(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    cur = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
    row = cur.fetchone()
    current_version = int(row["version"]) if row is not None else 0
    migrate_bootstrap_schema(conn, current_version)
    apply_migrations(conn)
    ensure_message_columns(conn)
    ensure_archive_message_columns(conn)
    if current_version < SCHEMA_VERSION:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.execute("PRAGMA optimize")
