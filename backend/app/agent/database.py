"""SQLite database layer for the agent runtime.

Single agent.db file in .runtime/ replaces per-conversation JSON files.
Uses WAL mode for concurrent read/write safety and synchronous sqlite3
with a reentrant lock for thread-safe access.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent.migrations import apply_migrations
from app.agent.runtime_paths import RUNTIME_DIR

logger = logging.getLogger(__name__)

_RUNTIME_DIR = RUNTIME_DIR
_DB_PATH = _RUNTIME_DIR / "agent.db"
_JSON_CONVERSATIONS_DIR = _RUNTIME_DIR / "conversations"

_SCHEMA_VERSION = 10

_SCHEMA_SQL = """
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_task_runs_idempotency
ON scheduled_task_runs(task_id, idempotency_key)
WHERE idempotency_key <> '';

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


class Database:
    """Thread-safe synchronous SQLite connection manager (singleton)."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or _DB_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            # L3: timeout=5 prevents indefinite blocking when the DB file is locked.
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=5.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def init_db(self) -> None:
        """Create tables if they don't exist, apply migrations."""
        with self._lock:
            self.conn.executescript(_SCHEMA_SQL)
            cur = self.conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            )
            row = cur.fetchone()
            current_version = int(row["version"]) if row is not None else 0
            self._migrate(current_version)
            apply_migrations(self.conn)
            self._ensure_message_columns()
            self._ensure_archive_message_columns()
            if current_version < _SCHEMA_VERSION:
                self.conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (_SCHEMA_VERSION,),
                )
            self.conn.execute("PRAGMA optimize")
            self.conn.commit()

    def _migrate(self, current_version: int) -> None:
        # The conversations table existed before these resumability fields were added.
        # Ensure upgrades add them for existing runtime databases.
        if current_version < 2:
            self._ensure_conversation_columns()
        if current_version < 3:
            self._ensure_message_columns()
        self._ensure_scheduled_task_tables()

    def _ensure_conversation_columns(self) -> None:
        column_defs = {
            "summary": "TEXT DEFAULT ''",
            "task_goal": "TEXT DEFAULT ''",
            "context_tokens_estimate": "INTEGER DEFAULT 0",
        }
        existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(conversations)").fetchall()
        }
        for column, definition in column_defs.items():
            if column not in existing:
                self.conn.execute(
                    f"ALTER TABLE conversations ADD COLUMN {column} {definition}"
                )

    def _ensure_message_columns(self) -> None:
        column_defs = {
            "thinking": "TEXT DEFAULT ''",
            "status": "TEXT DEFAULT 'complete'",
            "response_duration_ms": "INTEGER",
            "attachments_json": "TEXT DEFAULT ''",
        }
        existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        for column, definition in column_defs.items():
            if column not in existing:
                self.conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
        self.conn.execute(
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

    def _ensure_archive_message_columns(self) -> None:
        existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(messages_archive)").fetchall()
        }
        if not existing:
            return
        if "response_duration_ms" not in existing:
            self.conn.execute("ALTER TABLE messages_archive ADD COLUMN response_duration_ms INTEGER")
        if "attachments_json" not in existing:
            self.conn.execute("ALTER TABLE messages_archive ADD COLUMN attachments_json TEXT DEFAULT ''")

    def _ensure_scheduled_task_tables(self) -> None:
        self.conn.executescript(
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
            CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_task_runs_idempotency
            ON scheduled_task_runs(task_id, idempotency_key)
            WHERE idempotency_key <> '';
            """
        )
        existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
        }
        if "reuse_conversation" not in existing:
            self.conn.execute(
                "ALTER TABLE scheduled_tasks ADD COLUMN reuse_conversation INTEGER NOT NULL DEFAULT 0"
            )
        if "owner_principal_id" not in existing:
            self.conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN owner_principal_id TEXT DEFAULT ''")
        if "permission_profile_id" not in existing:
            self.conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN permission_profile_id TEXT DEFAULT 'scheduled-restricted'")
        if "permission_profile_snapshot" not in existing:
            self.conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN permission_profile_snapshot TEXT DEFAULT ''")
        run_existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(scheduled_task_runs)").fetchall()
        }
        if "idempotency_key" not in run_existing:
            self.conn.execute("ALTER TABLE scheduled_task_runs ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''")
        if "lease_owner" not in run_existing:
            self.conn.execute("ALTER TABLE scheduled_task_runs ADD COLUMN lease_owner TEXT DEFAULT ''")
        if "lease_expires_at" not in run_existing:
            self.conn.execute("ALTER TABLE scheduled_task_runs ADD COLUMN lease_expires_at TEXT DEFAULT ''")
        self.conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_task_runs_idempotency
            ON scheduled_task_runs(task_id, idempotency_key)
            WHERE idempotency_key <> ''
            """
        )

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, seq: list[tuple]) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.executemany(sql, seq)

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    def fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # Conversation CRUD

    def create_conversation(self, conv_id: str, title: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conv_id, title, now, now),
            )
            self.conn.commit()
        return {"id": conv_id, "title": title, "created_at": now}

    def list_conversations(self) -> list[dict]:
        rows = self.fetchall(
            "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
        )
        return [
            {"id": r["id"], "title": r["title"], "created_at": r["created_at"]}
            for r in rows
        ]

    def get_conversation(self, conv_id: str) -> dict | None:
        row = self.fetchone("SELECT * FROM conversations WHERE id = ?", (conv_id,))
        return dict(row) if row else None

    # C2: Allowed column names for update_conversation - prevents dynamic SQL injection.
    _CONVERSATION_MUTABLE_COLS: frozenset[str] = frozenset(
        {"title", "updated_at", "summary", "task_goal", "context_tokens_estimate"}
    )

    def update_conversation(self, conv_id: str, **fields) -> None:
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        unknown = set(fields) - self._CONVERSATION_MUTABLE_COLS
        if unknown:
            raise ValueError(f"update_conversation: unknown column(s): {unknown}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = tuple(fields.values()) + (conv_id,)
        with self._lock:
            self.conn.execute(f"UPDATE conversations SET {sets} WHERE id = ?", vals)
            self.conn.commit()

    def delete_conversation(self, conv_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            self.conn.commit()
            return cur.rowcount > 0

    # Scheduled task CRUD

    _SCHEDULED_TASK_COLUMNS: tuple[str, ...] = (
        "id",
        "title",
        "prompt",
        "schedule_kind",
        "cron_expr",
        "interval_seconds",
        "run_at",
        "timezone",
        "enabled",
        "overlap_policy",
        "notify_telegram",
        "telegram_chat_id",
        "reuse_conversation",
        "owner_principal_id",
        "permission_profile_id",
        "permission_profile_snapshot",
        "last_run_at",
        "last_run_status",
        "last_run_conversation_id",
        "next_run_at",
        "consecutive_failures",
        "created_at",
        "updated_at",
    )
    _SCHEDULED_TASK_MUTABLE_COLS: frozenset[str] = frozenset(
        set(_SCHEDULED_TASK_COLUMNS) - {"id", "created_at"}
    )

    def create_scheduled_task(self, fields: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        values = {
            "cron_expr": "",
            "interval_seconds": 0,
            "run_at": "",
            "timezone": "UTC",
            "enabled": 1,
            "overlap_policy": "skip",
            "notify_telegram": 0,
            "telegram_chat_id": "",
            "reuse_conversation": 0,
            "owner_principal_id": "",
            "permission_profile_id": "scheduled-restricted",
            "permission_profile_snapshot": "",
            "last_run_at": "",
            "last_run_status": "",
            "last_run_conversation_id": "",
            "next_run_at": "",
            "consecutive_failures": 0,
            "created_at": now,
            "updated_at": now,
            **fields,
        }
        unknown = set(values) - set(self._SCHEDULED_TASK_COLUMNS)
        if unknown:
            raise ValueError(f"create_scheduled_task: unknown column(s): {unknown}")
        missing = {"id", "title", "prompt", "schedule_kind"} - set(values)
        if missing:
            raise ValueError(f"create_scheduled_task: missing column(s): {missing}")
        columns = list(self._SCHEDULED_TASK_COLUMNS)
        placeholders = ", ".join("?" for _ in columns)
        with self._lock:
            self.conn.execute(
                f"INSERT INTO scheduled_tasks ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(values.get(column) for column in columns),
            )
            self.conn.commit()
        task = self.get_scheduled_task(str(values["id"]))
        if task is None:
            raise RuntimeError("created scheduled task could not be reloaded")
        return task

    def list_scheduled_tasks(self) -> list[dict[str, Any]]:
        rows = self.fetchall(
            "SELECT * FROM scheduled_tasks ORDER BY created_at DESC"
        )
        return [dict(row) for row in rows]

    def list_due_scheduled_tasks(self, now_iso: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.fetchall(
            """
            SELECT * FROM scheduled_tasks
            WHERE enabled = 1
              AND next_run_at <> ''
              AND next_run_at <= ?
            ORDER BY next_run_at ASC
            LIMIT ?
            """,
            (now_iso, max(1, min(200, int(limit)))),
        )
        return [dict(row) for row in rows]

    def claim_due_scheduled_task(
        self,
        task_id: str,
        *,
        expected_next_run_at: str,
        now_iso: str,
    ) -> bool:
        if not str(expected_next_run_at or "").strip():
            return False
        with self._lock:
            cur = self.conn.execute(
                """
                UPDATE scheduled_tasks
                SET next_run_at = '',
                    updated_at = ?
                WHERE id = ?
                  AND enabled = 1
                  AND next_run_at = ?
                  AND next_run_at <> ''
                  AND next_run_at <= ?
                """,
                (now_iso, task_id, expected_next_run_at, now_iso),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def get_scheduled_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
        return dict(row) if row else None

    def update_scheduled_task(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_scheduled_task(task_id)
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        unknown = set(fields) - self._SCHEDULED_TASK_MUTABLE_COLS
        if unknown:
            raise ValueError(f"update_scheduled_task: unknown column(s): {unknown}")
        sets = ", ".join(f"{key} = ?" for key in fields)
        values = tuple(fields.values()) + (task_id,)
        with self._lock:
            self.conn.execute(f"UPDATE scheduled_tasks SET {sets} WHERE id = ?", values)
            self.conn.commit()
        return self.get_scheduled_task(task_id)

    def delete_scheduled_task(self, task_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def create_scheduled_task_run(
        self,
        *,
        task_id: str,
        conversation_id: str,
        started_at: str,
        status: str = "running",
        idempotency_key: str = "",
        lease_owner: str = "",
        lease_expires_at: str = "",
    ) -> int:
        with self._lock:
            cur = self.conn.execute(
                """
                INSERT INTO scheduled_task_runs (
                    task_id, conversation_id, idempotency_key, lease_owner,
                    lease_expires_at, started_at, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    conversation_id,
                    idempotency_key,
                    lease_owner,
                    lease_expires_at,
                    started_at,
                    status,
                ),
            )
            self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def update_scheduled_task_run(
        self,
        run_id: int,
        *,
        finished_at: str = "",
        status: str = "",
        final_text: str = "",
        error: str = "",
        lease_owner: str = "",
        lease_expires_at: str = "",
    ) -> None:
        fields = {
            key: value
            for key, value in {
                "finished_at": finished_at,
                "status": status,
                "final_text": final_text,
                "error": error,
                "lease_owner": lease_owner,
                "lease_expires_at": lease_expires_at,
            }.items()
            if value != ""
        }
        if not fields:
            return
        sets = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self.conn.execute(
                f"UPDATE scheduled_task_runs SET {sets} WHERE id = ?",
                tuple(fields.values()) + (int(run_id),),
            )
            self.conn.commit()

    def list_scheduled_task_runs(self, task_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.fetchall(
            """
            SELECT * FROM scheduled_task_runs
            WHERE task_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (task_id, max(1, min(100, int(limit)))),
        )
        return [dict(row) for row in rows]

    def list_running_scheduled_task_ids(self) -> set[str]:
        rows = self.fetchall(
            """
            SELECT DISTINCT task_id
            FROM scheduled_task_runs
            WHERE status = 'running'
            """
        )
        return {str(row["task_id"]) for row in rows}

    def recover_expired_scheduled_task_runs(self, now_iso: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                """
                UPDATE scheduled_task_runs
                SET status = 'error',
                    finished_at = ?,
                    error = 'scheduler lease expired',
                    lease_owner = '',
                    lease_expires_at = ''
                WHERE status = 'running'
                  AND lease_expires_at <> ''
                  AND lease_expires_at <= ?
                """,
                (now_iso, now_iso),
            )
            self.conn.commit()
            return int(cur.rowcount)

    # Message CRUD

    def add_message(
        self,
        conv_id: str,
        role: str,
        content: str,
        thinking: str = "",
        status: str = "complete",
        response_duration_ms: int | None = None,
        attachments_json: str = "",
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self.conn.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, thinking, status,
                    response_duration_ms, attachments_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conv_id,
                    role,
                    content,
                    thinking,
                    status,
                    response_duration_ms,
                    attachments_json,
                    now,
                ),
            )
            self.conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id)
            )
            self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_messages(
        self,
        conv_id: str,
        limit: int = 50,
        before_id: int | None = None,
    ) -> list[dict]:
        if before_id is not None:
            rows = self.fetchall(
                "SELECT * FROM messages WHERE conversation_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
                (conv_id, before_id, limit),
            )
        else:
            rows = self.fetchall(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                (conv_id, limit),
            )
        result = [dict(r) for r in rows]
        result.reverse()
        return result

    def get_messages_after(
        self,
        conv_id: str,
        after_id: int = 0,
        limit: int = 200,
    ) -> list[dict]:
        rows = self.fetchall(
            """
            SELECT * FROM messages
            WHERE conversation_id = ?
              AND id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (conv_id, max(0, int(after_id or 0)), max(1, int(limit))),
        )
        return [dict(row) for row in rows]

    def get_last_message_id(self, conv_id: str) -> int:
        row = self.fetchone(
            "SELECT MAX(id) AS last_id FROM messages WHERE conversation_id = ?",
            (conv_id,),
        )
        return int(row["last_id"] or 0) if row is not None else 0

    def get_recent_messages(self, conv_id: str, limit: int = 10) -> list[dict]:
        return self.get_messages(conv_id, limit=limit)

    def count_messages(self, conv_id: str) -> int:
        row = self.fetchone(
            "SELECT COUNT(*) as cnt FROM messages WHERE conversation_id = ?",
            (conv_id,),
        )
        return row["cnt"] if row else 0

    def archive_old_messages(
        self,
        conv_id: str,
        *,
        keep_recent: int = 50,
        max_to_archive: int | None = None,
    ) -> int:
        """Move older hot messages into messages_archive, preserving ids."""
        keep_recent = max(0, int(keep_recent))
        sql = """
            SELECT id, conversation_id, role, content,
                   COALESCE(thinking, '') AS thinking,
                   COALESCE(status, 'complete') AS status,
                   response_duration_ms,
                   COALESCE(attachments_json, '') AS attachments_json,
                   created_at
            FROM messages
            WHERE conversation_id = ?
              AND id NOT IN (
                  SELECT id
                  FROM messages
                  WHERE conversation_id = ?
                  ORDER BY id DESC
                  LIMIT ?
              )
            ORDER BY id ASC
        """
        params: tuple[Any, ...] = (conv_id, conv_id, keep_recent)
        if max_to_archive is not None:
            sql = f"{sql} LIMIT ?"
            params = params + (max(0, int(max_to_archive)),)

        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
            if not rows:
                return 0
            self.conn.executemany(
                """
                INSERT OR IGNORE INTO messages_archive (
                    id, conversation_id, role, content, thinking, status,
                    response_duration_ms, attachments_json, created_at, archived_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["id"],
                        row["conversation_id"],
                        row["role"],
                        row["content"],
                        row["thinking"],
                        row["status"],
                        row["response_duration_ms"],
                        row["attachments_json"],
                        row["created_at"],
                        now,
                    )
                    for row in rows
                ],
            )
            message_ids = [(row["id"],) for row in rows]
            self.conn.executemany("DELETE FROM messages WHERE id = ?", message_ids)
            self.conn.commit()
            return len(rows)

    def restore_message(self, message_id: int) -> dict[str, Any] | None:
        """Restore one archived message back into the hot messages table."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM messages_archive WHERE id = ?",
                (int(message_id),),
            ).fetchone()
            if row is None:
                return None
            try:
                self.conn.execute(
                    """
                    INSERT INTO messages (
                        id, conversation_id, role, content, thinking, status,
                        response_duration_ms, attachments_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["conversation_id"],
                        row["role"],
                        row["content"],
                        row["thinking"],
                        row["status"],
                        row["response_duration_ms"],
                        row["attachments_json"] if "attachments_json" in row.keys() else "",
                        row["created_at"],
                    ),
                )
            except sqlite3.IntegrityError:
                return None
            self.conn.execute("DELETE FROM messages_archive WHERE id = ?", (int(message_id),))
            self.conn.commit()
            return dict(row)

    def count_archived_messages(self, conv_id: str | None = None) -> int:
        if conv_id:
            row = self.fetchone(
                "SELECT COUNT(*) AS cnt FROM messages_archive WHERE conversation_id = ?",
                (conv_id,),
            )
        else:
            row = self.fetchone("SELECT COUNT(*) AS cnt FROM messages_archive")
        return int(row["cnt"]) if row else 0


    def add_tool_outcome(self, conv_id: str, tool_name: str, result: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                "INSERT INTO tool_outcomes (conversation_id, tool_name, result, created_at) VALUES (?, ?, ?, ?)",
                (conv_id, tool_name, result[:200] if result else "", now),
            )
            self.conn.commit()

    def add_tool_call(
        self,
        *,
        message_id: int,
        conv_id: str,
        tool_name: str,
        tool_input: str = "",
        tool_output: str = "",
        status: str = "pending",
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self.conn.execute(
                """
                INSERT INTO tool_calls (message_id, conversation_id, tool_name, input, output, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, conv_id, tool_name, tool_input, tool_output, status, now),
            )
            self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_tool_calls_for_message(self, message_id: int) -> list[dict]:
        rows = self.fetchall(
            "SELECT id, tool_name, input, output, status, created_at FROM tool_calls WHERE message_id = ? ORDER BY id",
            (message_id,),
        )
        return [dict(row) for row in rows]

    def count_tool_calls_for_conversation(self, conv_id: str) -> int:
        row = self.fetchone(
            "SELECT COUNT(*) AS count FROM tool_calls WHERE conversation_id = ?",
            (conv_id,),
        )
        return int(row["count"]) if row is not None else 0

    def get_conversation_compaction(self, conv_id: str) -> dict[str, Any] | None:
        row = self.fetchone(
            "SELECT * FROM conversation_compactions WHERE conversation_id = ?",
            (conv_id,),
        )
        return dict(row) if row is not None else None

    def upsert_conversation_compaction(
        self,
        *,
        conv_id: str,
        summary: str,
        source_message_id: int,
        message_count: int,
        tokens_before: int,
        tokens_after: int,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO conversation_compactions (
                    conversation_id, summary, source_message_id, message_count,
                    tokens_before, tokens_after, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    summary = excluded.summary,
                    source_message_id = excluded.source_message_id,
                    message_count = excluded.message_count,
                    tokens_before = excluded.tokens_before,
                    tokens_after = excluded.tokens_after,
                    updated_at = excluded.updated_at
                """,
                (
                    conv_id,
                    summary,
                    int(source_message_id),
                    int(message_count),
                    int(tokens_before),
                    int(tokens_after),
                    now,
                    now,
                ),
            )
            self.conn.commit()
        compaction = self.get_conversation_compaction(conv_id)
        if compaction is None:
            raise RuntimeError("conversation compaction could not be reloaded")
        return compaction

    def get_recent_tool_outcomes(self, conv_id: str, limit: int = 5) -> list[dict]:
        rows = self.fetchall(
            "SELECT tool_name, result FROM tool_outcomes WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (conv_id, limit),
        )
        result = [{"tool": r["tool_name"], "result": r["result"]} for r in rows]
        result.reverse()
        return result

    # Plan steps

    def sync_plan_steps(
        self, conv_id: str, pending: list[dict], completed: list[str]
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                "DELETE FROM plan_steps WHERE conversation_id = ?", (conv_id,)
            )
            for step in pending:
                self.conn.execute(
                    "INSERT INTO plan_steps (conversation_id, step_id, description, status, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        conv_id,
                        step.get("step_id", ""),
                        step.get("description", ""),
                        step.get("status", "pending"),
                        now,
                    ),
                )
            for desc in completed:
                self.conn.execute(
                    "INSERT INTO plan_steps (conversation_id, step_id, description, status, created_at) VALUES (?, ?, ?, ?, ?)",
                    (conv_id, "", desc, "done", now),
                )
            self.conn.commit()

    def get_pending_steps(self, conv_id: str) -> list[dict]:
        rows = self.fetchall(
            "SELECT step_id, description, status FROM plan_steps WHERE conversation_id = ? AND status != 'done' ORDER BY id",
            (conv_id,),
        )
        return [dict(r) for r in rows]

    def get_completed_steps(self, conv_id: str) -> list[str]:
        rows = self.fetchall(
            "SELECT description FROM plan_steps WHERE conversation_id = ? AND status = 'done' ORDER BY id",
            (conv_id,),
        )
        return [r["description"] for r in rows]

    def clear_plan_steps(self, conv_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM plan_steps WHERE conversation_id = ?", (conv_id,)
            )
            self.conn.commit()

    # Session grants

    def add_session_grant(
        self, target_type: str, identifier: str, grant_type: str
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO session_grants (target_type, identifier, grant_type, use_count, created_at) VALUES (?, ?, ?, 0, ?)",
                (target_type, identifier, grant_type, now),
            )
            self.conn.commit()

    def check_session_grant(self, target_type: str, identifier: str) -> str | None:
        row = self.fetchone(
            "SELECT grant_type, use_count FROM session_grants WHERE target_type = ? AND identifier = ?",
            (target_type, identifier),
        )
        if row is None:
            return None
        if row["grant_type"] == "once" and row["use_count"] > 0:
            return None
        return row["grant_type"]

    def consume_once_grant(self, target_type: str, identifier: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE session_grants SET use_count = use_count + 1 WHERE target_type = ? AND identifier = ? AND grant_type = 'once'",
                (target_type, identifier),
            )
            self.conn.commit()

    def clear_session_grants(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM session_grants")
            self.conn.commit()

    # JSON migration

    def migrate_from_json(self) -> int:
        """Import existing JSON conversation files into SQLite.

        Returns the number of conversations migrated.
        """
        if not _JSON_CONVERSATIONS_DIR.exists():
            return 0

        json_files = list(_JSON_CONVERSATIONS_DIR.glob("*.json"))
        if not json_files:
            return 0

        existing = {
            r["id"]
            for r in self.fetchall("SELECT id FROM conversations")
        }

        count = 0
        for file_path in json_files:
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
                conv_id = data.get("conversation_id", file_path.stem)
                if conv_id in existing:
                    continue

                title = data.get("title", "Untitled") or "Untitled"
                created_at = str(data.get("created_at", datetime.now(timezone.utc).isoformat()))
                updated_at = str(data.get("updated_at", created_at))
                summary = data.get("summary", "")
                task_goal = data.get("task_goal", "")

                with self._lock:
                    self.conn.execute(
                        "INSERT INTO conversations (id, title, created_at, updated_at, summary, task_goal) VALUES (?, ?, ?, ?, ?, ?)",
                        (conv_id, title, created_at, updated_at, summary, task_goal),
                    )

                    for msg in data.get("all_messages", []) or data.get("recent_messages", []):
                        role = msg.get("role", "user")
                        content = msg.get("content", "")
                        ts = msg.get("timestamp", created_at)
                        self.conn.execute(
                            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                            (conv_id, role, content, str(ts)),
                        )

                    for outcome in data.get("tool_outcomes", []):
                        self.conn.execute(
                            "INSERT INTO tool_outcomes (conversation_id, tool_name, result, created_at) VALUES (?, ?, ?, ?)",
                            (
                                conv_id,
                                outcome.get("tool", ""),
                                outcome.get("result", ""),
                                updated_at,
                            ),
                        )

                    self.conn.commit()

                count += 1
            except Exception as e:
                logger.warning("Failed to migrate %s: %s", file_path.name, e)
                continue

        if count > 0:
            backup_dir = _RUNTIME_DIR / "conversations_backup"
            try:
                _JSON_CONVERSATIONS_DIR.rename(backup_dir)
                logger.info(
                    "Migrated %d conversations from JSON to SQLite. Backup at %s",
                    count,
                    backup_dir,
                )
            except Exception as e:
                logger.warning("Could not rename conversations dir: %s", e)

        return count


# Singleton

_db: Database | None = None


def get_db() -> Database:
    """Return the shared Database singleton, initializing on first call."""
    global _db
    if _db is None:
        _db = Database()
        _db.init_db()
        migrated = _db.migrate_from_json()
        if migrated:
            logger.info("SQLite migration complete: %d conversations imported", migrated)
    return _db


def reset_db() -> None:
    """Close and discard the singleton (for testing)."""
    global _db
    if _db is not None:
        _db.close()
        _db = None
