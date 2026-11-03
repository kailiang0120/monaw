"""Transactional Journal for durable Agent runs and operations."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.agent.database import Database
from app.agent.durable.event_store import EventStore
from app.agent.durable.models import (
    EFFECT_CLASSES,
    EVENT_TYPES,
    OPERATION_KINDS,
    OPERATION_STATUSES,
    REASON_CODES,
    RUN_STATUSES,
    EffectClass,
    EventPageDTO,
    IdempotencyConflictError,
    InFlightOperationsError,
    InvalidTransitionError,
    JournalConflictError,
    LeaseConflictError,
    OperationDTO,
    OperationNotFoundError,
    OperationStatus,
    RevisionConflictError,
    RunDTO,
    RunNotFoundError,
    RunStatus,
    TurnCheckpointDTO,
    is_safe_identifier,
    sanitize_evidence,
    sanitize_event_payload,
    sanitize_public_evidence,
)
from app.agent.runtime_paths import WORKSPACE_DIR

_ACTIVE_RUN_STATUSES = frozenset({"running", "waiting_approval", "pausing"})
_LEASE_RENEWABLE_STATUSES = _ACTIVE_RUN_STATUSES | frozenset({"interrupted", "needs_review"})
_RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    "running": frozenset(
        {"waiting_approval", "pausing", "interrupted", "needs_review", "completed", "failed", "cancelled"}
    ),
    "waiting_approval": frozenset(
        {"running", "pausing", "interrupted", "needs_review", "failed", "cancelled"}
    ),
    "pausing": frozenset({"paused", "interrupted", "needs_review", "failed", "cancelled"}),
    "paused": frozenset(),
    "interrupted": frozenset({"needs_review", "paused", "failed", "cancelled"}),
    "needs_review": frozenset({"paused", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
_OPERATION_TRANSITIONS: dict[str, frozenset[str]] = {
    "prepared": frozenset({"authorized", "cancelled"}),
    "authorized": frozenset({"executing", "cancelled"}),
    "executing": frozenset({"verified", "failed", "uncertain", "returned"}),
    "uncertain": frozenset({"needs_review", "verified", "failed"}),
    "needs_review": frozenset({"verified", "failed"}),
    "verified": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "returned": frozenset(),
}
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
_UNRESOLVED_WRITE_STATUSES = frozenset(
    {"prepared", "authorized", "executing", "uncertain", "needs_review"}
)
_EXECUTION_SOURCES = frozenset(
    {
        "desktop",
        "desktop_job",
        "desktop_stream",
        "scheduled",
        "scheduler",
        "telegram",
        "api",
        "replay",
        "test",
        "other",
    }
)
_SAFE_TOOL_NAMES = frozenset(
    {
        "read_file",
        "write_file",
        "move_file",
        "copy_file",
        "delete_file",
        "create_directory",
        "file_mutation",
        "undo",
        "shell_command",
        "browser_action",
        "mcp_call",
        "tool_call",
        "unknown",
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="microseconds")


def _lease_seconds(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600:
        raise ValueError("lease_seconds must be between 1 and 3600")
    return value


def _private_text(value: str | None, *, name: str, max_length: int = 4096) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_length or "\x00" in value:
        raise ValueError(f"{name} is invalid")
    return value or None


def _required_id(value: str, *, name: str) -> str:
    if not is_safe_identifier(value):
        raise ValueError(f"{name} must be a bounded identifier")
    return value


def _reason_code(value: str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in REASON_CODES:
        return value
    # Never persist or publish arbitrary caller-provided explanation text.
    return "unknown"


def _safe_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode_json(value: object) -> dict[str, bool | int | str]:
    try:
        raw = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return sanitize_evidence(raw)


def _workspace_key(value: object) -> str:
    raw = str(value or "").strip() or str(WORKSPACE_DIR)
    try:
        path = str(Path(raw).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        path = os.path.abspath(os.path.normpath(raw))
    return os.path.normcase(os.path.normpath(path))


class Journal:
    """Read and mutate Run/Operation state using atomic SQLite transactions.

    All writes use Database's shared connection, RLock, and an immediate
    transaction. The run revision is the compare-and-swap token; the owner and
    attempt form a second fence for every worker mutation.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self.event_store = EventStore(db)

    def create_run(
        self,
        *,
        run_id: str,
        conversation_id: str,
        execution_source: str,
        owner_instance_id: str,
        turn_id: str,
        workspace_dir: str = "",
        owner_principal_id: str | None = None,
        control_session_id: str | None = None,
        recovery_input_ref: str | None = None,
        lease_seconds: int = 30,
    ) -> RunDTO:
        run_id = _required_id(run_id, name="run_id")
        conversation_id = _required_id(conversation_id, name="conversation_id")
        owner_instance_id = _required_id(owner_instance_id, name="owner_instance_id")
        turn_id = _required_id(turn_id, name="turn_id")
        lease_seconds = _lease_seconds(lease_seconds)
        workspace_dir = _private_text(workspace_dir, name="workspace_dir", max_length=32768) or ""
        owner_principal_id = _private_text(owner_principal_id, name="owner_principal_id", max_length=512)
        control_session_id = _private_text(control_session_id, name="control_session_id", max_length=512)
        recovery_input_ref = _private_text(recovery_input_ref, name="recovery_input_ref", max_length=4096)
        source = execution_source if isinstance(execution_source, str) and execution_source in _EXECUTION_SOURCES else "other"
        now_dt = _now()
        now = _timestamp(now_dt)
        lease_until = _timestamp(now_dt + timedelta(seconds=lease_seconds))

        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is not None:
                first_attempt = conn.execute(
                    "SELECT turn_id FROM run_attempts WHERE run_id = ? AND attempt = 1",
                    (run_id,),
                ).fetchone()
                same_scope = (
                    existing["conversation_id"] == conversation_id
                    and existing["execution_source"] == source
                    and (existing["owner_principal_id"] or None) == owner_principal_id
                    and (existing["control_session_id"] or None) == control_session_id
                    and existing["workspace_dir"] == workspace_dir
                    and first_attempt is not None
                    and first_attempt["turn_id"] == turn_id
                )
                if not same_scope:
                    raise IdempotencyConflictError("run_id was reused with different creation scope")
                return self._run_dto(conn, existing)

            if conn.execute(
                "SELECT 1 FROM turn_checkpoints WHERE turn_id = ?", (turn_id,)
            ).fetchone() is not None:
                raise IdempotencyConflictError("turn_id already has a committed checkpoint")

            active_statuses = tuple(_ACTIVE_RUN_STATUSES | {"interrupted", "needs_review"})
            placeholders = ",".join("?" for _ in active_statuses)
            active_runs = conn.execute(
                f"""SELECT run_id, conversation_id, workspace_dir, owner_instance_id, status
                    FROM agent_runs WHERE status IN ({placeholders})""",
                active_statuses,
            ).fetchall()
            new_workspace = _workspace_key(workspace_dir)
            for active in active_runs:
                if str(active["conversation_id"] or "") == conversation_id:
                    raise LeaseConflictError("conversation already has an active durable run")
                if (
                    str(active["owner_instance_id"] or "") != owner_instance_id
                    and _workspace_key(active["workspace_dir"]) == new_workspace
                ):
                    raise LeaseConflictError("workspace has an active durable run in another process")

            conn.execute(
                """INSERT INTO agent_runs (
                       run_id, conversation_id, workspace_dir, execution_source,
                       owner_principal_id, control_session_id, owner_instance_id,
                       lease_expires_at, attempt, revision, status, reason_code,
                       turn_id, recovery_input_ref, last_seq, created_at, updated_at,
                       finished_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 'running', NULL, ?, ?, 0, ?, ?, NULL)""",
                (
                    run_id,
                    conversation_id,
                    workspace_dir,
                    source,
                    owner_principal_id,
                    control_session_id,
                    owner_instance_id,
                    lease_until,
                    turn_id,
                    recovery_input_ref,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO run_attempts (run_id, attempt, turn_id, created_at) VALUES (?, 1, ?, ?)",
                (run_id, turn_id, now),
            )
            self._append_event(
                conn,
                run_id,
                "run.created",
                {"status": "running", "attempt": 1, "revision": 1, "reason_code": None},
                at=now,
            )
            row = self._require_run(conn, run_id)
            return self._run_dto(conn, row)

    def load_run(self, run_id: str) -> RunDTO | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            return self._run_dto(conn, row) if row is not None else None

    def load_run_internal(self, run_id: str) -> dict[str, Any] | None:
        """Load private run scope and owner fields for recovery/dispatch guards."""
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            return {
                "run_id": str(row["run_id"]),
                "conversation_id": str(row["conversation_id"]),
                "workspace_dir": str(row["workspace_dir"] or ""),
                "execution_source": str(row["execution_source"]),
                "owner_principal_id": str(row["owner_principal_id"] or ""),
                "control_session_id": str(row["control_session_id"] or ""),
                "owner_instance_id": str(row["owner_instance_id"] or ""),
                "lease_expires_at": str(row["lease_expires_at"] or ""),
                "attempt": int(row["attempt"]),
                "revision": int(row["revision"]),
                "status": str(row["status"]),
                "reason_code": _reason_code(row["reason_code"]),
                "turn_id": str(row["turn_id"] or ""),
                "recovery_input_ref": str(row["recovery_input_ref"] or ""),
            }

    def list_recovery_runs(self, *, after_run_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        """Page active, owned runs for startup recovery without exposing them publicly."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, 500)
        statuses = ("running", "waiting_approval", "pausing", "interrupted", "needs_review")
        placeholders = ",".join("?" for _ in statuses)
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                f"""SELECT run_id, conversation_id, workspace_dir, execution_source,
                           owner_principal_id, control_session_id, owner_instance_id,
                           lease_expires_at, attempt, revision, status
                    FROM agent_runs
                    WHERE run_id > ? AND status IN ({placeholders})
                    ORDER BY run_id ASC LIMIT ?""",
                (after_run_id, *statuses, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def snapshot(self, run_id: str) -> RunDTO | None:
        """Return a public snapshot and event cursor from one read transaction."""
        return self.load_run(run_id)

    def claim_run(
        self,
        run_id: str,
        *,
        owner_instance_id: str,
        expected_revision: int,
        turn_id: str,
        lease_seconds: int = 30,
        expected_attempt: int | None = None,
    ) -> RunDTO:
        run_id = _required_id(run_id, name="run_id")
        owner_instance_id = _required_id(owner_instance_id, name="owner_instance_id")
        turn_id = _required_id(turn_id, name="turn_id")
        lease_seconds = _lease_seconds(lease_seconds)
        now_dt = _now()
        now = _timestamp(now_dt)
        with self.db.transaction() as conn:
            row = self._require_run(conn, run_id)
            self._expect_revision(row, expected_revision)
            self._expect_attempt(row, expected_attempt)
            if row["status"] != "paused":
                raise InvalidTransitionError("only a paused run can start a new attempt")
            if row["owner_instance_id"] is not None or row["lease_expires_at"] is not None:
                raise LeaseConflictError("paused run still has an owner")
            if not self._has_recovery_input(conn, row):
                raise InvalidTransitionError("run has no committed recovery input")
            if not self._has_safe_resume_input(conn, row):
                raise InvalidTransitionError("run has no safe committed recovery input")
            if conn.execute(
                "SELECT 1 FROM turn_checkpoints WHERE turn_id = ?", (turn_id,)
            ).fetchone() is not None:
                raise IdempotencyConflictError("turn_id already has a committed checkpoint")

            attempt = int(row["attempt"]) + 1
            revision = int(row["revision"]) + 1
            try:
                conn.execute(
                    "INSERT INTO run_attempts (run_id, attempt, turn_id, created_at) VALUES (?, ?, ?, ?)",
                    (run_id, attempt, turn_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise IdempotencyConflictError("turn_id is already assigned to an attempt") from exc
            conn.execute(
                """UPDATE agent_runs SET status = 'running', owner_instance_id = ?,
                       lease_expires_at = ?, attempt = ?, revision = ?, turn_id = ?,
                       reason_code = NULL, updated_at = ?, finished_at = NULL
                   WHERE run_id = ? AND revision = ? AND status = 'paused'
                     AND owner_instance_id IS NULL AND lease_expires_at IS NULL""",
                (
                    owner_instance_id,
                    _timestamp(now_dt + timedelta(seconds=lease_seconds)),
                    attempt,
                    revision,
                    turn_id,
                    now,
                    run_id,
                    int(row["revision"]),
                ),
            )
            self._require_rowcount(conn, "run claim lost its compare-and-swap")
            self._append_event(
                conn,
                run_id,
                "run.updated",
                {"status": "running", "attempt": attempt, "revision": revision, "reason_code": None},
                at=now,
            )
            return self._run_dto(conn, self._require_run(conn, run_id))

    def renew_lease(
        self,
        run_id: str,
        *,
        owner_instance_id: str,
        attempt: int,
        lease_seconds: int = 30,
    ) -> RunDTO:
        run_id = _required_id(run_id, name="run_id")
        owner_instance_id = _required_id(owner_instance_id, name="owner_instance_id")
        lease_seconds = _lease_seconds(lease_seconds)
        now_dt = _now()
        now = _timestamp(now_dt)
        with self.db.transaction() as conn:
            row = self._require_run(conn, run_id)
            self._assert_owner(row, owner_instance_id, attempt)
            if row["status"] not in _LEASE_RENEWABLE_STATUSES:
                raise LeaseConflictError("run is not in a lease-owning state")
            conn.execute(
                """UPDATE agent_runs SET lease_expires_at = ?, updated_at = ?
                   WHERE run_id = ? AND owner_instance_id = ? AND attempt = ?""",
                (
                    _timestamp(now_dt + timedelta(seconds=lease_seconds)),
                    now,
                    run_id,
                    owner_instance_id,
                    attempt,
                ),
            )
            self._require_rowcount(conn, "lease renewal lost its owner fence")
            return self._run_dto(conn, self._require_run(conn, run_id))

    def release_expired_run(
        self,
        run_id: str,
        *,
        expected_owner_instance_id: str,
        recovery_owner_instance_id: str,
        expected_attempt: int,
        expected_revision: int,
        safe_to_release: bool,
        owner_proven_dead: bool,
        reason_code: str | None = "worker_lost",
        lease_seconds: int = 30,
    ) -> RunDTO:
        """Release an owner after recovery supplied a safety proof.

        The recovery caller must supply a recognized process-death proof.
        Lease expiry alone is never sufficient; proof of process death permits
        immediate handoff even while the lease is fresh.
        """
        run_id = _required_id(run_id, name="run_id")
        expected_owner_instance_id = _required_id(
            expected_owner_instance_id, name="expected_owner_instance_id"
        )
        recovery_owner_instance_id = _required_id(
            recovery_owner_instance_id, name="recovery_owner_instance_id"
        )
        if safe_to_release is not True:
            raise LeaseConflictError("recovery caller did not confirm safe release")
        if owner_proven_dead is not True:
            raise LeaseConflictError("recovery caller did not prove the owner process is dead")
        if recovery_owner_instance_id == expected_owner_instance_id:
            raise LeaseConflictError("recovery release requires a distinct caller")
        lease_seconds = _lease_seconds(lease_seconds)

        now_dt = _now()
        now = _timestamp(now_dt)
        with self.db.transaction() as conn:
            row = self._require_run(conn, run_id)
            self._expect_revision(row, expected_revision)
            self._expect_attempt(row, expected_attempt)
            if row["owner_instance_id"] != expected_owner_instance_id:
                raise LeaseConflictError("expired owner no longer matches")
            if row["status"] not in (_ACTIVE_RUN_STATUSES | {"interrupted", "needs_review"}):
                raise LeaseConflictError("run is not in an expirable state")
            expires_text = row["lease_expires_at"]
            try:
                datetime.fromisoformat(str(expires_text))
            except (TypeError, ValueError) as exc:
                raise LeaseConflictError("stored lease expiry is invalid") from exc
            previous_revision = self._reconcile_interrupted_operations(conn, run_id, now)
            revision = previous_revision + 1
            safe_reason = _reason_code(reason_code) or "worker_lost"
            interrupted_status = "needs_review" if row["status"] == "needs_review" else "interrupted"
            conn.execute(
                """UPDATE agent_runs SET status = ?, owner_instance_id = ?,
                       lease_expires_at = ?, reason_code = ?, revision = ?, updated_at = ?
                   WHERE run_id = ? AND owner_instance_id = ? AND attempt = ? AND revision = ?""",
                (
                    interrupted_status,
                    recovery_owner_instance_id,
                    _timestamp(now_dt + timedelta(seconds=lease_seconds)),
                    safe_reason,
                    revision,
                    now,
                    run_id,
                    expected_owner_instance_id,
                    expected_attempt,
                    previous_revision,
                ),
            )
            self._require_rowcount(conn, "safe release lost its owner fence")
            self._append_event(
                conn,
                run_id,
                "run.updated",
                {
                    "status": interrupted_status,
                    "attempt": int(row["attempt"]),
                    "revision": revision,
                    "reason_code": safe_reason,
                },
                at=now,
            )
            return self._run_dto(conn, self._require_run(conn, run_id))

    def transition_run(
        self,
        run_id: str,
        *,
        to_status: RunStatus,
        owner_instance_id: str,
        attempt: int,
        expected_revision: int,
        reason_code: str | None = None,
        recovery_input_ref: str | None = None,
    ) -> RunDTO:
        run_id = _required_id(run_id, name="run_id")
        owner_instance_id = _required_id(owner_instance_id, name="owner_instance_id")
        if to_status not in RUN_STATUSES:
            raise ValueError("unsupported run status")
        safe_ref = _private_text(recovery_input_ref, name="recovery_input_ref", max_length=4096)
        safe_reason = _reason_code(reason_code)
        now = _timestamp()

        with self.db.transaction() as conn:
            row = self._require_run(conn, run_id)
            self._assert_owner(row, owner_instance_id, attempt)
            self._expect_revision(row, expected_revision)
            current = str(row["status"])
            if to_status not in _RUN_TRANSITIONS[current]:
                raise InvalidTransitionError(f"cannot transition run from {current} to {to_status}")

            if to_status in {"paused", *_TERMINAL_RUN_STATUSES}:
                self._ensure_no_unresolved_writes(conn, run_id)
            if to_status == "completed" and not self._has_committed_checkpoint(conn, row):
                raise InvalidTransitionError("completed run requires a committed turn checkpoint")
            if to_status == "paused":
                if not self._has_recovery_input(conn, row, recovery_input_ref=safe_ref):
                    raise InvalidTransitionError("paused run requires committed recovery input")

            if to_status == "interrupted":
                current_revision = self._reconcile_interrupted_operations(conn, run_id, now)
            else:
                current_revision = int(row["revision"])
            revision = current_revision + 1
            new_ref = safe_ref if safe_ref is not None else row["recovery_input_ref"]
            finished_at = now if to_status in _TERMINAL_RUN_STATUSES else None
            release_owner = to_status == "paused" or to_status in _TERMINAL_RUN_STATUSES
            owner_clause = ", owner_instance_id = NULL, lease_expires_at = NULL" if release_owner else ""
            conn.execute(
                f"""UPDATE agent_runs SET status = ?, reason_code = ?, revision = ?,
                       recovery_input_ref = ?, finished_at = ?, updated_at = ?{owner_clause}
                   WHERE run_id = ? AND owner_instance_id = ? AND attempt = ? AND revision = ?""",
                (
                    to_status,
                    safe_reason,
                    revision,
                    new_ref,
                    finished_at,
                    now,
                    run_id,
                    owner_instance_id,
                    attempt,
                    current_revision,
                ),
            )
            self._require_rowcount(conn, "run transition lost its owner or revision fence")
            self._append_event(
                conn,
                run_id,
                "run.updated",
                {
                    "status": to_status,
                    "attempt": int(row["attempt"]),
                    "revision": revision,
                    "reason_code": safe_reason,
                },
                at=now,
            )
            return self._run_dto(conn, self._require_run(conn, run_id))

    def prepare_operation(
        self,
        run_id: str,
        *,
        operation_id: str,
        owner_instance_id: str,
        attempt: int,
        operation_kind: str,
        effect_class: EffectClass,
        input_summary: object = None,
        evidence: object = None,
        snapshot_ref: str | None = None,
        related_operation_id: str | None = None,
        tool_name: str | None = None,
    ) -> OperationDTO:
        run_id = _required_id(run_id, name="run_id")
        operation_id = _required_id(operation_id, name="operation_id")
        owner_instance_id = _required_id(owner_instance_id, name="owner_instance_id")
        related_operation_id = (
            _required_id(related_operation_id, name="related_operation_id")
            if related_operation_id is not None
            else None
        )
        if operation_kind not in OPERATION_KINDS:
            raise ValueError("unsupported operation_kind")
        if effect_class not in EFFECT_CLASSES:
            raise ValueError("unsupported effect_class")
        safe_summary = sanitize_evidence(input_summary)
        safe_evidence = sanitize_evidence(evidence)
        private_snapshot_ref = _private_text(snapshot_ref, name="snapshot_ref", max_length=4096)
        if isinstance(tool_name, str) and tool_name in _SAFE_TOOL_NAMES:
            safe_tool_name = tool_name
        elif tool_name is None and operation_kind in {"shell_command", "browser_action", "mcp_call", "tool_call", "undo"}:
            safe_tool_name = operation_kind
        elif tool_name is None and operation_kind.startswith("file_"):
            safe_tool_name = "file_mutation"
        else:
            safe_tool_name = "unknown"
        now = _timestamp()

        with self.db.transaction() as conn:
            run = self._require_run(conn, run_id)
            self._assert_owner(run, owner_instance_id, attempt)
            if run["status"] not in {"running", "waiting_approval"}:
                raise InvalidTransitionError("operations can only be prepared on an active run")

            existing = conn.execute(
                "SELECT * FROM run_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if existing is not None:
                matches = (
                    existing["run_id"] == run_id
                    and int(existing["attempt"]) == attempt
                    and existing["operation_kind"] == operation_kind
                    and existing["effect_class"] == effect_class
                    and existing["related_operation_id"] == related_operation_id
                    and _decode_json(existing["input_summary_json"]) == safe_summary
                    and _decode_json(existing["precondition_json"]) == safe_evidence
                    and existing["snapshot_ref"] == private_snapshot_ref
                    and existing["tool_name"] == safe_tool_name
                )
                if not matches:
                    raise IdempotencyConflictError("operation_id was reused with different metadata")
                return self._operation_dto(existing)

            if related_operation_id is not None:
                related = conn.execute(
                    "SELECT sequence_no FROM run_operations WHERE run_id = ? AND operation_id = ?",
                    (run_id, related_operation_id),
                ).fetchone()
                if related is None:
                    raise JournalConflictError("related operation must exist in the same run")

            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM run_operations WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
            )
            try:
                conn.execute(
                    """INSERT INTO run_operations (
                           operation_id, run_id, attempt, sequence_no, tool_name,
                           operation_kind, effect_class, status, reason_code,
                           related_operation_id, input_summary_json, precondition_json,
                           evidence_json, snapshot_ref, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, ?, ?, ?, '{}', ?, ?, ?)""",
                    (
                        operation_id,
                        run_id,
                        attempt,
                        sequence,
                        safe_tool_name,
                        operation_kind,
                        effect_class,
                        related_operation_id,
                        _safe_json(safe_summary),
                        _safe_json(safe_evidence),
                        private_snapshot_ref,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise JournalConflictError("operation references an invalid run or attempt") from exc

            revision = int(run["revision"]) + 1
            conn.execute(
                "UPDATE agent_runs SET revision = ?, updated_at = ? WHERE run_id = ? AND revision = ?",
                (revision, now, run_id, int(run["revision"])),
            )
            self._require_rowcount(conn, "operation prepare lost its run revision fence")
            self._append_event(
                conn,
                run_id,
                "operation.prepared",
                {
                    "operation_id": operation_id,
                    "sequence": sequence,
                    "attempt": attempt,
                    "operation_kind": operation_kind,
                    "effect_class": effect_class,
                    "status": "prepared",
                    "revision": revision,
                },
                at=now,
            )
            row = conn.execute(
                "SELECT * FROM run_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            return self._operation_dto(row)

    def transition_operation(
        self,
        run_id: str,
        operation_id: str,
        *,
        to_status: OperationStatus,
        owner_instance_id: str,
        attempt: int,
        expected_revision: int | None = None,
        reason_code: str | None = None,
        evidence: object = None,
    ) -> OperationDTO:
        run_id = _required_id(run_id, name="run_id")
        operation_id = _required_id(operation_id, name="operation_id")
        owner_instance_id = _required_id(owner_instance_id, name="owner_instance_id")
        if to_status not in OPERATION_STATUSES:
            raise ValueError("unsupported operation status")
        safe_reason = _reason_code(reason_code)
        added_evidence = sanitize_evidence(evidence)
        now = _timestamp()

        with self.db.transaction() as conn:
            run = self._require_run(conn, run_id)
            self._assert_owner(run, owner_instance_id, attempt)
            if expected_revision is not None:
                self._expect_revision(run, expected_revision)
            op = conn.execute(
                "SELECT * FROM run_operations WHERE run_id = ? AND operation_id = ?",
                (run_id, operation_id),
            ).fetchone()
            if op is None:
                raise OperationNotFoundError(operation_id)
            if int(op["attempt"]) != attempt:
                raise LeaseConflictError("operation belongs to a stale attempt")

            current = str(op["status"])
            effect = str(op["effect_class"])
            if to_status not in _OPERATION_TRANSITIONS[current]:
                raise InvalidTransitionError(f"cannot transition operation from {current} to {to_status}")
            if to_status == "executing" and run["status"] != "running":
                raise InvalidTransitionError("operation execution requires a running Run")
            if to_status == "returned" and effect != "opaque_effect":
                raise InvalidTransitionError("returned is reserved for opaque effects")

            combined_evidence = _decode_json(op["evidence_json"])
            combined_evidence.update(added_evidence)
            if to_status == "verified" and effect == "opaque_effect":
                raise InvalidTransitionError("opaque effects cannot be marked verified")
            if (
                to_status == "verified"
                and effect == "verifiable_write"
                and added_evidence.get("verification_result") != "completed"
            ):
                raise InvalidTransitionError("verified writes require fresh completed verification evidence")
            if (
                to_status == "failed"
                and effect != "read_only"
                and current in {"executing", "uncertain", "needs_review"}
                and added_evidence.get("verification_result") != "unchanged"
            ):
                raise InvalidTransitionError("failed writes require fresh evidence that no effect occurred")

            revision = int(run["revision"]) + 1
            conn.execute(
                """UPDATE run_operations SET status = ?, reason_code = ?, evidence_json = ?,
                       updated_at = ? WHERE operation_id = ? AND run_id = ? AND attempt = ?
                       AND status = ?""",
                (
                    to_status,
                    safe_reason,
                    _safe_json(combined_evidence),
                    now,
                    operation_id,
                    run_id,
                    attempt,
                    current,
                ),
            )
            self._require_rowcount(conn, "operation transition lost its state fence")
            conn.execute(
                "UPDATE agent_runs SET revision = ?, updated_at = ? WHERE run_id = ? AND revision = ?",
                (revision, now, run_id, int(run["revision"])),
            )
            self._require_rowcount(conn, "operation transition lost its run revision fence")
            self._append_event(
                conn,
                run_id,
                "operation.updated",
                {
                    "operation_id": operation_id,
                    "sequence": int(op["sequence_no"]),
                    "attempt": attempt,
                    "status": to_status,
                    "reason_code": safe_reason,
                    "evidence": combined_evidence,
                    "revision": revision,
                },
                at=now,
            )
            updated = conn.execute(
                "SELECT * FROM run_operations WHERE run_id = ? AND operation_id = ?",
                (run_id, operation_id),
            ).fetchone()
            return self._operation_dto(updated)

    def load_operation(self, run_id: str, operation_id: str) -> OperationDTO | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT * FROM run_operations WHERE run_id = ? AND operation_id = ?",
                (run_id, operation_id),
            ).fetchone()
            return self._operation_dto(row) if row is not None else None

    def load_operation_internal(self, run_id: str, operation_id: str) -> dict[str, Any] | None:
        """Load the typed private operation record for recovery/verifier code.

        This internal path can return raw hashes and an opaque snapshot
        reference. Do not expose it from HTTP/event handlers.
        """
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT * FROM run_operations WHERE run_id = ? AND operation_id = ?",
                (run_id, operation_id),
            ).fetchone()
            if row is None:
                return None
            return {
                "run_id": str(row["run_id"]),
                "operation_id": str(row["operation_id"]),
                "sequence": int(row["sequence_no"]),
                "attempt": int(row["attempt"]),
                "operation_kind": str(row["operation_kind"]),
                "effect_class": str(row["effect_class"]),
                "status": str(row["status"]),
                "reason_code": _reason_code(row["reason_code"]),
                "input_summary": _decode_json(row["input_summary_json"]),
                "precondition_evidence": _decode_json(row["precondition_json"]),
                "evidence": _decode_json(row["evidence_json"]),
                "snapshot_ref": str(row["snapshot_ref"]) if row["snapshot_ref"] else None,
            }

    def list_operations(
        self,
        run_id: str,
        *,
        limit: int = 100,
        after_sequence: int = 0,
    ) -> list[OperationDTO]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise ValueError("after_sequence must be a nonnegative integer")
        limit = min(limit, 500)
        with self.db.transaction(immediate=False) as conn:
            if conn.execute("SELECT 1 FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                raise RunNotFoundError(run_id)
            rows = conn.execute(
                """SELECT * FROM run_operations WHERE run_id = ? AND sequence_no > ?
                   ORDER BY sequence_no ASC LIMIT ?""",
                (run_id, after_sequence, limit),
            ).fetchall()
            return [self._operation_dto(row) for row in rows]

    def load_turn_checkpoint(self, run_id: str, attempt: int) -> TurnCheckpointDTO | None:
        """Resolve the committed checkpoint for one durable run attempt."""
        with self.db.transaction(immediate=False) as conn:
            run = conn.execute(
                "SELECT conversation_id FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise RunNotFoundError(run_id)
            mapping = conn.execute(
                "SELECT turn_id FROM run_attempts WHERE run_id = ? AND attempt = ?",
                (run_id, attempt),
            ).fetchone()
            if mapping is None:
                return None
            checkpoint = conn.execute(
                """SELECT turn_id, conversation_id, user_message_ids FROM turn_checkpoints
                   WHERE turn_id = ? AND conversation_id = ?""",
                (mapping["turn_id"], run["conversation_id"]),
            ).fetchone()
            if checkpoint is None:
                return None
            try:
                user_ids = json.loads(checkpoint["user_message_ids"])
            except (TypeError, json.JSONDecodeError):
                user_ids = []
            if not isinstance(user_ids, list) or any(
                isinstance(item, bool) or not isinstance(item, int) for item in user_ids
            ):
                user_ids = []
            return {
                "run_id": run_id,
                "attempt": int(attempt),
                "turn_id": str(checkpoint["turn_id"]),
                "conversation_id": str(checkpoint["conversation_id"]),
                "user_message_ids": user_ids,
            }

    def read_events(
        self, run_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> EventPageDTO:
        return self.event_store.read_events(run_id, after_seq=after_seq, limit=limit)

    def _append_event(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: object,
        *,
        at: str,
    ) -> int:
        if event_type not in EVENT_TYPES:
            raise ValueError("unsupported durable event type")
        safe_payload = sanitize_event_payload(event_type, payload)
        payload_json = _safe_json(safe_payload)
        if len(payload_json.encode("utf-8")) > 8192:
            raise ValueError("public event payload exceeds its size limit")
        row = conn.execute(
            "SELECT last_seq FROM agent_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        seq = int(row["last_seq"]) + 1
        conn.execute(
            "UPDATE agent_runs SET last_seq = ? WHERE run_id = ? AND last_seq = ?",
            (seq, run_id, int(row["last_seq"])),
        )
        self._require_rowcount(conn, "event sequence allocation lost its compare-and-swap")
        conn.execute(
            "INSERT INTO run_events (run_id, seq, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, seq, event_type, payload_json, at),
        )
        return seq

    def _run_dto(self, conn: sqlite3.Connection, row: sqlite3.Row) -> RunDTO:
        """Project public state; API callers must reauthorize available_actions."""
        status = str(row["status"])
        actions: list[str]
        if status == "running":
            actions = ["pause", "stop"]
        elif status == "waiting_approval":
            actions = ["approve", "reject", "pause", "stop"]
        elif status == "pausing":
            actions = ["stop"]
        elif status == "paused":
            actions = []
            if self._has_safe_resume_input(conn, row):
                actions.append("resume")
        elif status == "interrupted":
            actions = ["inspect", "stop"]
        elif status == "needs_review":
            actions = ["inspect", "verify", "stop"]
        else:
            actions = ["inspect"]
        return {
            "run_id": str(row["run_id"]),
            "conversation_id": str(row["conversation_id"]),
            "attempt": int(row["attempt"]),
            "revision": int(row["revision"]),
            "status": status,  # type: ignore[typeddict-item]
            "reason_code": _reason_code(row["reason_code"]),
            "available_actions": actions,
            "last_seq": int(row["last_seq"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
        }

    def _operation_dto(self, row: sqlite3.Row) -> OperationDTO:
        return {
            "run_id": str(row["run_id"]),
            "operation_id": str(row["operation_id"]),
            "sequence": int(row["sequence_no"]),
            "attempt": int(row["attempt"]),
            "operation_kind": str(row["operation_kind"]),
            "related_operation_id": (
                str(row["related_operation_id"]) if row["related_operation_id"] else None
            ),
            "effect_class": str(row["effect_class"]),  # type: ignore[typeddict-item]
            "status": str(row["status"]),  # type: ignore[typeddict-item]
            "reason_code": _reason_code(row["reason_code"]),
            "input_summary": sanitize_public_evidence(_decode_json(row["input_summary_json"])),
            "evidence": sanitize_public_evidence(_decode_json(row["evidence_json"])),
            "snapshot_available": bool(row["snapshot_ref"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _require_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        return row

    @staticmethod
    def _expect_revision(row: sqlite3.Row, expected_revision: int) -> None:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("expected_revision must be an integer")
        if int(row["revision"]) != expected_revision:
            raise RevisionConflictError("run revision is stale")

    @staticmethod
    def _expect_attempt(row: sqlite3.Row, expected_attempt: int | None) -> None:
        if expected_attempt is not None and int(row["attempt"]) != expected_attempt:
            raise LeaseConflictError("run attempt is stale")

    @staticmethod
    def _assert_owner(row: sqlite3.Row, owner_instance_id: str, attempt: int) -> None:
        if row["owner_instance_id"] != owner_instance_id or int(row["attempt"]) != attempt:
            raise LeaseConflictError("owner or attempt does not match the current run generation")

    @staticmethod
    def _require_rowcount(conn: sqlite3.Connection, message: str) -> None:
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise RevisionConflictError(message)

    @staticmethod
    def _has_committed_checkpoint(conn: sqlite3.Connection, run_row: sqlite3.Row) -> bool:
        return conn.execute(
            "SELECT 1 FROM turn_checkpoints WHERE turn_id = ? AND conversation_id = ?",
            (run_row["turn_id"], run_row["conversation_id"]),
        ).fetchone() is not None

    def _has_recovery_input(
        self,
        conn: sqlite3.Connection,
        run_row: sqlite3.Row,
        *,
        recovery_input_ref: str | None = None,
    ) -> bool:
        ref = recovery_input_ref if recovery_input_ref is not None else run_row["recovery_input_ref"]
        return bool(ref) or self._has_committed_checkpoint(conn, run_row)

    def _has_safe_resume_input(self, conn: sqlite3.Connection, run_row: sqlite3.Row) -> bool:
        """Only advertise resume when input and all effect evidence are safe."""
        conversation = conn.execute(
            "SELECT workspace_dir, updated_at FROM conversations WHERE id = ?",
            (run_row["conversation_id"],),
        ).fetchone()
        if conversation is None or str(conversation["workspace_dir"] or "") != str(run_row["workspace_dir"] or ""):
            return False
        try:
            run_updated = datetime.fromisoformat(str(run_row["updated_at"]))
            conversation_updated = datetime.fromisoformat(str(conversation["updated_at"]))
            if run_updated.tzinfo is None:
                run_updated = run_updated.replace(tzinfo=timezone.utc)
            if conversation_updated.tzinfo is None:
                conversation_updated = conversation_updated.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        if conversation_updated > run_updated:
            return False

        ref = str(run_row["recovery_input_ref"] or "")
        checkpoint_turn_id = (
            ref.removeprefix("turn_checkpoint:")
            if ref.startswith("turn_checkpoint:")
            else str(run_row["turn_id"] or "")
        )
        checkpoint = conn.execute(
            """SELECT user_message_ids FROM turn_checkpoints
               WHERE turn_id = ? AND conversation_id = ?""",
            (checkpoint_turn_id, run_row["conversation_id"]),
        ).fetchone()
        if checkpoint is None:
            return False
        try:
            user_ids = json.loads(checkpoint["user_message_ids"])
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(user_ids, list) or not user_ids or any(
            isinstance(item, bool) or not isinstance(item, int) for item in user_ids
        ):
            return False
        placeholders = ",".join("?" for _ in user_ids)
        user_rows = conn.execute(
            f"""SELECT id, role, attachments_json FROM messages
                WHERE conversation_id = ? AND id IN ({placeholders})""",
            (run_row["conversation_id"], *user_ids),
        ).fetchall()
        by_id = {int(item["id"]): item for item in user_rows}
        if any(
            message_id not in by_id
            or by_id[message_id]["role"] != "user"
            or str(by_id[message_id]["attachments_json"] or "").strip()
            for message_id in user_ids
        ):
            return False
        latest_checkpoint_user_id = max(user_ids)
        if conn.execute(
            """SELECT 1 FROM messages WHERE conversation_id = ? AND role = 'user'
               AND id > ? LIMIT 1""",
            (run_row["conversation_id"], latest_checkpoint_user_id),
        ).fetchone() is not None:
            return False

        operations = conn.execute(
            """SELECT effect_class, status, evidence_json FROM run_operations
               WHERE run_id = ? ORDER BY sequence_no""",
            (run_row["run_id"],),
        ).fetchall()
        for operation in operations:
            effect = str(operation["effect_class"])
            status = str(operation["status"])
            evidence = _decode_json(operation["evidence_json"])
            if effect == "opaque_effect" and status != "cancelled":
                return False
            if effect != "read_only" and status not in {"verified", "cancelled"}:
                if not (status == "failed" and evidence.get("verification_result") == "unchanged"):
                    return False
        return True

    def _ensure_no_unresolved_writes(self, conn: sqlite3.Connection, run_id: str) -> None:
        row = conn.execute(
            """SELECT operation_id, status FROM run_operations
               WHERE run_id = ? AND effect_class != 'read_only'
                 AND status IN ('prepared', 'authorized', 'executing', 'uncertain', 'needs_review')
               ORDER BY sequence_no LIMIT 1""",
            (run_id,),
        ).fetchone()
        if row is not None:
            raise InFlightOperationsError("run has an unresolved write operation")

    def _reconcile_interrupted_operations(
        self, conn: sqlite3.Connection, run_id: str, now: str
    ) -> int:
        """Cancel undispatched intent and mark interrupted writes uncertain."""
        run = self._require_run(conn, run_id)
        revision = int(run["revision"])
        operations = conn.execute(
            """SELECT operation_id, attempt, sequence_no, effect_class, status, evidence_json
               FROM run_operations WHERE run_id = ?
                 AND status IN ('prepared', 'authorized', 'executing')
               ORDER BY sequence_no""",
            (run_id,),
        ).fetchall()
        for operation in operations:
            current = str(operation["status"])
            if current == "executing" and operation["effect_class"] != "read_only":
                target = "uncertain"
            else:
                target = "cancelled"
            revision += 1
            evidence = _decode_json(operation["evidence_json"])
            conn.execute(
                """UPDATE run_operations SET status = ?, reason_code = 'worker_lost',
                       updated_at = ? WHERE operation_id = ? AND run_id = ? AND status = ?""",
                (target, now, operation["operation_id"], run_id, current),
            )
            self._require_rowcount(conn, "operation reconciliation lost its state fence")
            conn.execute(
                "UPDATE agent_runs SET revision = ?, updated_at = ? WHERE run_id = ? AND revision = ?",
                (revision, now, run_id, revision - 1),
            )
            self._require_rowcount(conn, "operation reconciliation lost its run revision fence")
            self._append_event(
                conn,
                run_id,
                "operation.updated",
                {
                    "operation_id": str(operation["operation_id"]),
                    "sequence": int(operation["sequence_no"]),
                    "attempt": int(operation["attempt"]),
                    "status": target,
                    "reason_code": "worker_lost",
                    "evidence": evidence,
                    "revision": revision,
                },
                at=now,
            )
        return revision
