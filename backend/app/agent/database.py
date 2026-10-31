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
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.agent.db_bootstrap import initialize_database
from app.agent.runtime_paths import RUNTIME_DIR

logger = logging.getLogger(__name__)

_RUNTIME_DIR = RUNTIME_DIR
_DB_PATH = _RUNTIME_DIR / "agent.db"
_JSON_CONVERSATIONS_DIR = _RUNTIME_DIR / "conversations"


class ScheduledTaskConflictError(RuntimeError):
    """A configuration edit was based on a superseded task revision."""


class ScheduledTaskCapacityError(RuntimeError):
    """No bounded queue capacity is available for a manual request."""


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
            initialize_database(self.conn)
            self.conn.commit()

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

    def create_conversation(self, conv_id: str, title: str, workspace_dir: str = "") -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO conversations (id, title, created_at, updated_at, workspace_dir) VALUES (?, ?, ?, ?, ?)",
                (conv_id, title, now, now, workspace_dir),
            )
            self.conn.commit()
        return {"id": conv_id, "title": title, "created_at": now, "workspace_dir": workspace_dir}

    def list_conversations(self) -> list[dict]:
        rows = self.fetchall(
            "SELECT id, title, created_at, updated_at, workspace_dir FROM conversations ORDER BY updated_at DESC"
        )
        return [
            {"id": r["id"], "title": r["title"], "created_at": r["created_at"], "workspace_dir": r["workspace_dir"]}
            for r in rows
        ]

    def get_conversation(self, conv_id: str) -> dict | None:
        row = self.fetchone("SELECT * FROM conversations WHERE id = ?", (conv_id,))
        return dict(row) if row else None

    # C2: Allowed column names for update_conversation - prevents dynamic SQL injection.
    _CONVERSATION_MUTABLE_COLS: frozenset[str] = frozenset(
        {"title", "updated_at", "summary", "task_goal", "context_tokens_estimate", "workspace_dir"}
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
        "config_revision",
    )
    _SCHEDULED_TASK_MUTABLE_COLS: frozenset[str] = frozenset(
        set(_SCHEDULED_TASK_COLUMNS) - {"id", "created_at", "config_revision"}
    )
    _SCHEDULED_TASK_CONFIG_COLS = frozenset({
        "title", "prompt", "schedule_kind", "cron_expr", "interval_seconds", "run_at",
        "timezone", "enabled", "overlap_policy", "notify_telegram", "telegram_chat_id",
        "reuse_conversation", "owner_principal_id", "permission_profile_id", "permission_profile_snapshot",
    })
    _SCHEDULED_TASK_COMPLETION_COLS = frozenset({
        "last_run_at", "last_run_status", "last_run_conversation_id", "next_run_at",
        "consecutive_failures", "enabled",
    })

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
            "config_revision": 0,
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

    def get_scheduled_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
        return dict(row) if row else None

    def accept_scheduled_occurrence(
        self, task_id: str, *, now_iso: str, lease_owner: str, lease_expires_at: str,
        build: Callable[[dict[str, Any]], dict[str, Any]], manual_key: str = "",
        expected_due: str = "", expected_revision: int = 0, max_running: int = 4,
        max_queued: int = 16, blocked_tasks: set[str] = frozenset(), stopping_count: int = 0,
    ) -> dict[str, Any] | None:
        """Accept once, snapshot inputs, and advance cadence in one transaction.

        Queued means execution has not started. Only start_scheduled_occurrence
        may transition it to running; a crash between acceptance and dispatch
        therefore cannot erase due intent or replay an uncertain running action.
        """
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if manual_key:
                existing = self.conn.execute(
                    "SELECT run_id AS id, conversation_id FROM scheduled_run_requests WHERE task_id = ? AND operation_key = ?",
                    (task_id, manual_key),
                ).fetchone()
                if existing is not None:
                    return {**dict(existing), "created": False}
            task = self.conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            current = dict(task)
            manual = bool(manual_key)
            if not manual and (
                not current["enabled"] or not expected_due or current["next_run_at"] != expected_due
                or current["config_revision"] != expected_revision or expected_due > now_iso
            ):
                return None

            running = self.conn.execute("SELECT COUNT(*) FROM scheduled_task_runs WHERE status = 'running'").fetchone()[0]
            active = self.conn.execute(
                "SELECT COUNT(*) FROM scheduled_task_runs WHERE task_id = ? AND status = 'running'", (task_id,),
            ).fetchone()[0]
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM scheduled_task_runs WHERE task_id = ? AND status = 'queued'", (task_id,),
            ).fetchone()[0]
            queued = self.conn.execute("SELECT COUNT(*) FROM scheduled_task_runs WHERE status = 'queued'").fetchone()[0]
            replace = manual or current["overlap_policy"] == "cancel_previous"
            busy = bool(active or pending or task_id in blocked_tasks)
            reason = ""
            if not replace and current["overlap_policy"] == "skip":
                if busy:
                    reason = "overlap"
                elif running + stopping_count >= max_running:
                    reason = "global_concurrency_limit"
            if not reason and ((not replace and pending) or queued - (pending if replace else 0) >= max_queued):
                reason = "queue_limit"
            if manual and reason:
                raise ScheduledTaskCapacityError("Scheduled task queue is full. Wait for pending work to finish.")

            values = build(current)
            key = manual_key or f"auto:{current['config_revision']}:{expected_due}"
            conversation_id = "" if reason else values["conversation_id"]
            if replace and not reason:
                self.conn.execute(
                    """UPDATE scheduled_task_runs SET status = 'cancelled', finished_at = ?,
                       error = 'replaced before execution', lease_owner = '', lease_expires_at = '', task_snapshot = ''
                       WHERE task_id = ? AND status = 'queued'""", (now_iso, task_id),
                )
                self.conn.execute(
                    "UPDATE scheduled_task_runs SET cancel_requested = 1 WHERE task_id = ? AND status = 'running'",
                    (task_id,),
                )
            if conversation_id:
                self.conn.execute(
                    "INSERT OR IGNORE INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (conversation_id, values["conversation_title"], now_iso, now_iso),
                )
            cur = self.conn.execute(
                """INSERT INTO scheduled_task_runs
                   (task_id, conversation_id, idempotency_key, lease_owner, lease_expires_at,
                    started_at, finished_at, status, error, config_revision, task_snapshot, manual, scheduled_for, coalesced)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, conversation_id, key, "" if reason else lease_owner, "" if reason else lease_expires_at,
                 now_iso, now_iso if reason else "", "skipped" if reason else "queued", reason,
                 current["config_revision"], "" if reason else json.dumps(current), manual, values["scheduled_for"], values["coalesced"]),
            )
            if not manual:
                self.conn.execute(
                    "UPDATE scheduled_tasks SET next_run_at = ?, updated_at = ? WHERE id = ?",
                    (values["next_run_at"], now_iso, task_id),
                )
                if reason:
                    self.conn.execute(
                        """UPDATE scheduled_tasks SET last_run_at = ?, last_run_status = ?,
                           enabled = CASE WHEN schedule_kind = 'once' THEN 0 ELSE enabled END WHERE id = ?""",
                        (now_iso, reason, task_id),
                    )
            result = dict(self.conn.execute("SELECT * FROM scheduled_task_runs WHERE id = ?", (cur.lastrowid,)).fetchone())
            if manual:
                self.conn.execute(
                    "INSERT INTO scheduled_run_requests (task_id, operation_key, run_id, conversation_id) VALUES (?, ?, ?, ?)",
                    (task_id, key, result["id"], conversation_id),
                )
        return {**result, "created": True}

    def get_scheduled_operation(self, task_id: str, key: str) -> dict[str, Any] | None:
        row = self.fetchone(
            "SELECT run_id AS id, conversation_id FROM scheduled_run_requests WHERE task_id = ? AND operation_key = ?", (task_id, key),
        )
        return dict(row) if row else None

    def cancel_disabled_scheduled_occurrences(self, now_iso: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE scheduled_task_runs SET status = 'cancelled', finished_at = ?,
                   error = 'task disabled before execution', lease_owner = '', lease_expires_at = '', task_snapshot = ''
                   WHERE status = 'queued' AND manual = 0 AND task_id IN
                   (SELECT id FROM scheduled_tasks WHERE enabled = 0)""", (now_iso,),
            )

    def list_queued_scheduled_occurrences(self, owner: str, now_iso: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.fetchall(
            """SELECT * FROM scheduled_task_runs WHERE status = 'queued' AND task_snapshot <> ''
               AND (lease_owner = ? OR lease_expires_at <= ?) ORDER BY id""", (owner, now_iso),
        )]

    def start_scheduled_occurrence(self, run_id: int, owner: str, now_iso: str, expires: str, max_running: int) -> bool:
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.execute(
                """UPDATE scheduled_task_runs SET status = 'running', lease_owner = ?, lease_expires_at = ?, started_at = ?
                   WHERE id = ? AND status = 'queued' AND cancel_requested = 0
                   AND (lease_owner = ? OR lease_expires_at <= ?)
                   AND EXISTS (SELECT 1 FROM scheduled_tasks t WHERE t.id = task_id AND (t.enabled = 1 OR manual = 1))
                   AND (SELECT COUNT(*) FROM scheduled_task_runs WHERE status = 'running') < ?
                   AND NOT EXISTS (SELECT 1 FROM scheduled_task_runs r
                                   WHERE r.task_id = scheduled_task_runs.task_id AND r.status = 'running')""",
                (owner, expires, now_iso, run_id, owner, now_iso, max_running),
            )
        return cur.rowcount == 1

    def release_queued_scheduled_occurrences(self, owner: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE scheduled_task_runs SET lease_owner = '', lease_expires_at = '' WHERE status = 'queued' AND lease_owner = ?",
                (owner,),
            )

    def update_scheduled_task(
        self, task_id: str, *, expected_config_revision: int | None = None, **fields: Any,
    ) -> dict[str, Any] | None:
        if not fields:
            return self.get_scheduled_task(task_id)
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        unknown = set(fields) - self._SCHEDULED_TASK_MUTABLE_COLS
        if unknown:
            raise ValueError(f"update_scheduled_task: unknown column(s): {unknown}")
        sets = ", ".join(f"{key} = ?" for key in fields)
        if fields.keys() & self._SCHEDULED_TASK_CONFIG_COLS:
            sets += ", config_revision = config_revision + 1"
        values = tuple(fields.values()) + (task_id,)
        where = "id = ?"
        if expected_config_revision is not None:
            where += " AND config_revision = ?"
            values += (expected_config_revision,)
        with self._lock, self.conn:
            cur = self.conn.execute(f"UPDATE scheduled_tasks SET {sets} WHERE {where}", values)
            if cur.rowcount == 0 and self.conn.execute(
                "SELECT 1 FROM scheduled_tasks WHERE id = ?", (task_id,),
            ).fetchone():
                raise ScheduledTaskConflictError("Scheduled task changed; reload it before saving again.")
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
        config_revision: int = 0,
    ) -> int:
        with self._lock:
            cur = self.conn.execute(
                """
                INSERT INTO scheduled_task_runs (
                    task_id, conversation_id, idempotency_key, lease_owner,
                    lease_expires_at, started_at, status, config_revision
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    conversation_id,
                    idempotency_key,
                    lease_owner,
                    lease_expires_at,
                    started_at,
                    status,
                    config_revision,
                ),
            )
            self.conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def finalize_scheduled_task_run(
        self, *, task_id: str, run_id: int, conversation_id: str, lease_owner: str,
        finished_at: str, status: str, final_text: str, error: str,
        prepare_task_update: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Commit terminal evidence once, preserving current configuration.

        The callback is synchronous and pure: schedule calculation only, no
        nested database helpers (which commit) or external calls. BEGIN IMMEDIATE
        protects the read/decision/write against other connections as well as
        threads using this instance. A superseded run retains its own evidence
        but cannot change the current task summary or failure counter.
        """
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            task = self.conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
            run = self.conn.execute("SELECT * FROM scheduled_task_runs WHERE id = ?", (run_id,)).fetchone()
            if (
                task is None or run is None or run["task_id"] != task_id
                or run["conversation_id"] != conversation_id
                or run["status"] not in {"queued", "running"} or run["finished_at"]
                or run["lease_owner"] != lease_owner
                or (run["lease_expires_at"] and run["lease_expires_at"] <= finished_at)
            ):
                return None

            latest_id = self.conn.execute(
                """SELECT MAX(id) FROM scheduled_task_runs WHERE task_id = ? AND status <> 'skipped'
                   AND (status <> 'queued' OR id = ?)""", (task_id, run_id),
            ).fetchone()[0]
            fields = prepare_task_update(dict(task)) if latest_id == run_id else {}
            unknown = fields.keys() - self._SCHEDULED_TASK_COMPLETION_COLS
            if unknown:
                raise ValueError(f"finalize_scheduled_task_run: forbidden column(s): {unknown}")
            if fields and run["task_snapshot"] and (not run["manual"] or task["next_run_at"]):
                # Acceptance already advanced cadence. Completion must not
                # replace it with a delay based on completion time.
                fields["next_run_at"] = task["next_run_at"] if fields.get("enabled", task["enabled"]) else ""
                if task["config_revision"] != run["config_revision"] and task["next_run_at"] and status != "failed":
                    fields["enabled"] = task["enabled"]
                    fields["next_run_at"] = task["next_run_at"] if task["enabled"] else ""
            # Keep the legacy dead-letter API value for compatibility. The
            # notifier still receives the authoritative execution outcome.
            stored_status = "dead_letter" if fields.get("last_run_status") == "dead_letter" else status
            self.conn.execute(
                """UPDATE scheduled_task_runs
                   SET finished_at = ?, status = ?, final_text = ?, error = ?,
                       lease_owner = '', lease_expires_at = '', task_snapshot = '' WHERE id = ?""",
                (finished_at, stored_status, final_text, error, run_id),
            )
            if fields:
                fields["updated_at"] = finished_at
                sets = ", ".join(f"{key} = ?" for key in fields)
                self.conn.execute(
                    f"UPDATE scheduled_tasks SET {sets} WHERE id = ?", tuple(fields.values()) + (task_id,),
                )
            current = dict(self.conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone())
        return current

    def renew_scheduled_task_run_lease(
        self, run_id: int, *, lease_owner: str, now_iso: str, lease_expires_at: str,
        start: bool = False, manual: bool = False,
    ) -> bool:
        """Renew only live owned work; never resurrect a recovered attempt."""
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE scheduled_task_runs SET lease_expires_at = ?,
                       status = CASE WHEN ? THEN 'running' ELSE status END
                   WHERE id = ? AND lease_owner = ? AND status IN ('queued', 'running')
                     AND finished_at = '' AND lease_expires_at > ?
                     AND EXISTS (SELECT 1 FROM scheduled_tasks t WHERE t.id = task_id
                                 AND (NOT ? OR t.enabled = 1 OR ?))""",
                (lease_expires_at, start, run_id, lease_owner, now_iso, start, manual),
            )
        return cur.rowcount == 1

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
        clear_lease: bool = False,
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
        if clear_lease:
            fields["lease_owner"] = ""
            fields["lease_expires_at"] = ""
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

    def list_queued_scheduled_task_ids(self) -> set[str]:
        return {row["task_id"] for row in self.fetchall(
            "SELECT DISTINCT task_id FROM scheduled_task_runs WHERE status = 'queued'",
        )}

    def recover_expired_scheduled_task_runs(self, now_iso: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                """
                UPDATE scheduled_task_runs
                SET status = 'abandoned',
                    finished_at = ?,
                    error = 'scheduler lease expired',
                    lease_owner = '',
                    lease_expires_at = '',
                    task_snapshot = ''
                WHERE status = 'running'
                  AND lease_expires_at <> ''
                  AND lease_expires_at <= ?
                """,
                (now_iso, now_iso),
            )
            self.conn.commit()
            return int(cur.rowcount)

    def repair_scheduled_intent(self, now_iso: str, compute_next: Callable[[dict[str, Any]], str]) -> None:
        """Repair legacy cleared claims, without replaying interrupted work."""
        with self._lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                """UPDATE scheduled_task_runs SET status = 'abandoned', finished_at = ?,
                   error = 'legacy queued lease expired', lease_owner = '', lease_expires_at = '', task_snapshot = ''
                   WHERE status = 'queued' AND task_snapshot = '' AND lease_expires_at <> '' AND lease_expires_at <= ?""",
                (now_iso, now_iso),
            )
            rows = self.conn.execute(
                """SELECT * FROM scheduled_tasks t WHERE enabled = 1 AND next_run_at = ''
                   AND NOT EXISTS (SELECT 1 FROM scheduled_task_runs r WHERE r.task_id = t.id AND status IN ('queued', 'running'))
                   """,
            ).fetchall()
            for row in rows:
                due = compute_next(dict(row))
                self.conn.execute(
                    "UPDATE scheduled_tasks SET next_run_at = ?, enabled = ?, updated_at = ? WHERE id = ?",
                    (due, int(bool(due)), now_iso, row["id"]),
                )

    def enforce_scheduled_task_run_retention(
        self,
        *,
        max_age_days: int = 30,
        max_output_chars: int = 20_000,
    ) -> dict[str, int]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(max_age_days)))).isoformat()
        max_chars = max(0, int(max_output_chars))
        with self._lock:
            deleted = self.conn.execute(
                """
                DELETE FROM scheduled_task_runs
                WHERE finished_at <> ''
                  AND finished_at < ?
                """,
                (cutoff,),
            ).rowcount
            truncated = self.conn.execute(
                """
                UPDATE scheduled_task_runs
                SET final_text = substr(final_text, 1, ?),
                    error = substr(error, 1, ?)
                WHERE length(final_text) > ? OR length(error) > ?
                """,
                (max_chars, max_chars, max_chars, max_chars),
            ).rowcount
            self.conn.commit()
        return {
            "scheduled_task_runs_deleted": int(deleted),
            "scheduled_task_outputs_truncated": int(truncated),
        }

    def compact(self) -> None:
        """Give the space freed by deletions back to the disk."""
        with self._lock:
            self.conn.commit()
            self.conn.execute("VACUUM")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def clear_scheduled_task_outputs(self) -> dict[str, int]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT COUNT(*) FROM scheduled_task_runs
                WHERE final_text <> '' OR error <> ''
                """
            ).fetchone()
            updated = int(row[0] if row else 0)
            self.conn.execute(
                """UPDATE scheduled_task_runs SET final_text = '', error = '',
                   task_snapshot = CASE WHEN status IN ('queued', 'running') THEN task_snapshot ELSE '' END"""
            )
            self.conn.commit()
        return {"scheduled_task_outputs_cleared": updated}

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

    def get_recent_tool_calls_for_message(self, message_id: int, limit: int = 5) -> list[dict]:
        rows = self.fetchall(
            "SELECT tool_name, input, output, status FROM tool_calls WHERE message_id = ? ORDER BY id DESC LIMIT ?",
            (message_id, limit),
        )
        return [dict(row) for row in reversed(rows)]

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

    def consume_once_grant(self, target_type: str, identifier: str) -> bool:
        """Claim a one-time grant; only the first caller gets True."""
        with self._lock:
            cursor = self.conn.execute(
                "UPDATE session_grants SET use_count = use_count + 1 "
                "WHERE target_type = ? AND identifier = ? AND grant_type = 'once' AND use_count = 0",
                (target_type, identifier),
            )
            self.conn.commit()
            return cursor.rowcount == 1

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
