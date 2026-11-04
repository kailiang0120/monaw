"""Runtime ownership and cooperative controls for durable agent turns."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.agent.database import Database, get_db
from app.agent.durable.journal import Journal
from app.agent.durable.models import InvalidTransitionError, LeaseConflictError

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 30
LEASE_RENEW_INTERVAL_SECONDS = 10.0
CONTINUATION_PROMPT = (
    "Continue the interrupted task from the saved conversation and tool checkpoint. "
    "Use the recorded results as evidence, inspect the current state before repeating "
    "any action, and do not repeat completed work."
)


@dataclass(eq=False)
class DurableTurnRun:
    """The journal fence and turn checkpoint identity held by one live worker."""

    journal: Journal
    run_id: str
    owner_instance_id: str
    attempt: int
    turn_id: str
    revision: int
    recovery_input_ref: str = ""
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    status: str = "running"
    lease_lost: bool = False
    pause_requested: bool = False
    is_continuation: bool = False
    recovery_evidence: list[dict[str, Any]] = field(default_factory=list)
    pause_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def persist_recovery_input(
        self,
        conversation_id: str,
        user_message: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
    ) -> list[int]:
        """Persist the accepted user input before this attempt can cause effects."""
        if self.is_continuation:
            raise InvalidTransitionError("continuation attempts reuse their original recovery input")
        result = self.journal.persist_recovery_input(
            self.run_id,
            owner_instance_id=self.owner_instance_id,
            attempt=self.attempt,
            turn_id=self.turn_id,
            conversation_id=conversation_id,
            user_message=user_message,
            attachments=attachments,
            expected_revision=self.revision,
        )
        run = result["run"]
        self.revision = int(run["revision"])
        self.status = str(run["status"])
        return [int(item) for item in result["user_message_ids"]]

    def request_pause(self) -> None:
        self.pause_requested = True
        self.pause_event.set()

    def renew_lease(self) -> None:
        if self.lease_lost:
            raise LeaseConflictError("durable run lease was already lost")
        try:
            dto = self.journal.renew_lease(
                self.run_id,
                owner_instance_id=self.owner_instance_id,
                attempt=self.attempt,
                lease_seconds=self.lease_seconds,
            )
        except Exception:
            self.lease_lost = True
            raise
        self.revision = int(dto["revision"])
        self.status = str(dto["status"])

    def _latest_owned(self) -> dict[str, Any]:
        with self.journal.db.transaction(immediate=False) as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (self.run_id,)).fetchone()
            if row is None:
                self.lease_lost = True
                raise LeaseConflictError("durable run disappeared")
            dto = self.journal._run_dto(conn, row)
        if row["owner_instance_id"] != self.owner_instance_id or int(row["attempt"] or 0) != self.attempt:
            self.lease_lost = True
            raise LeaseConflictError("durable run owner or attempt changed")
        try:
            expires = datetime.fromisoformat(str(row["lease_expires_at"]))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError) as exc:
            self.lease_lost = True
            raise LeaseConflictError("durable run lease expiry is invalid") from exc
        if expires <= datetime.now(timezone.utc):
            self.lease_lost = True
            raise LeaseConflictError("durable run lease expired")
        self.revision = int(dto["revision"])
        self.status = str(dto["status"])
        return dict(dto)

    def begin_pause(self) -> None:
        """Move an owned run into pausing before it reaches a safe checkpoint."""
        self.request_pause()
        dto = self._latest_owned()
        current = str(dto["status"])
        if current == "pausing":
            return
        if current not in {"running", "waiting_approval"}:
            raise InvalidTransitionError(f"run cannot pause from {current}")
        updated = self.journal.transition_run(
            self.run_id,
            to_status="pausing",
            owner_instance_id=self.owner_instance_id,
            attempt=self.attempt,
            expected_revision=int(dto["revision"]),
            reason_code="pause_requested",
        )
        self.revision = int(updated["revision"])
        self.status = str(updated["status"])

    def mark_needs_review(self, reason_code: str = "external_effect_unknown") -> None:
        dto = self._latest_owned()
        current = str(dto["status"])
        if current == "needs_review":
            return
        if current not in {"running", "waiting_approval", "pausing"}:
            raise InvalidTransitionError(f"run cannot require review from {current}")
        updated = self.journal.transition_run(
            self.run_id,
            to_status="needs_review",
            owner_instance_id=self.owner_instance_id,
            attempt=self.attempt,
            expected_revision=int(dto["revision"]),
            reason_code=reason_code,
        )
        self.revision = int(updated["revision"])
        self.status = str(updated["status"])

    def begin_dispatch(self) -> None:
        """Return an approved wait to running before its trusted replay."""
        if self.pause_requested or self.lease_lost:
            raise LeaseConflictError("durable run is pausing or no longer owned")
        dto = self._latest_owned()
        current = str(dto["status"])
        if current == "running":
            return
        if current != "waiting_approval":
            raise InvalidTransitionError(f"run cannot dispatch from {current}")
        updated = self.journal.transition_run(
            self.run_id,
            to_status="running",
            owner_instance_id=self.owner_instance_id,
            attempt=self.attempt,
            expected_revision=int(dto["revision"]),
            reason_code="verified",
        )
        self.revision = int(updated["revision"])
        self.status = str(updated["status"])

    def wait_for_approval(self) -> None:
        """Publish that execution is waiting on an interactive gate."""
        if self.pause_requested or self.lease_lost:
            raise LeaseConflictError("durable run is pausing or no longer owned")
        dto = self._latest_owned()
        current = str(dto["status"])
        if current == "waiting_approval":
            return
        if current != "running":
            raise InvalidTransitionError(f"run cannot wait for approval from {current}")
        updated = self.journal.transition_run(
            self.run_id,
            to_status="waiting_approval",
            owner_instance_id=self.owner_instance_id,
            attempt=self.attempt,
            expected_revision=int(dto["revision"]),
            reason_code="approval_required",
        )
        self.revision = int(updated["revision"])
        self.status = str(updated["status"])

    def finalize(self, outcome_status: str, reason_code: str = "") -> bool:
        """Commit the durable outcome; return false when the run needs review."""
        if self.lease_lost:
            raise LeaseConflictError("durable run lease was lost before finalization")
        target = {
            "complete": "completed",
            "paused": "paused",
            "error": "failed",
        }.get(outcome_status, "failed")
        if outcome_status == "paused" and reason_code == "cancelled":
            target = "cancelled"
        dto = self._latest_owned()
        current = str(dto["status"])
        if current == target:
            return True
        try:
            if target == "paused" and current in {"running", "waiting_approval"}:
                pausing = self.journal.transition_run(
                    self.run_id,
                    to_status="pausing",
                    owner_instance_id=self.owner_instance_id,
                    attempt=self.attempt,
                    expected_revision=int(dto["revision"]),
                    reason_code="pause_requested",
                )
                self.revision = int(pausing["revision"])
                self.status = str(pausing["status"])
                dto = self._latest_owned()
                current = str(dto["status"])
            updated = self.journal.transition_run(
                self.run_id,
                to_status=target,
                owner_instance_id=self.owner_instance_id,
                attempt=self.attempt,
                expected_revision=int(dto["revision"]),
                reason_code={
                    "completed": "completed",
                    "paused": (
                        "user_pause" if self.pause_requested
                        else "timeout" if reason_code == "turn_timeout"
                        else "budget_exceeded" if "budget" in reason_code or "limit" in reason_code
                        else "checkpoint_committed"
                    ),
                    "failed": "run_failed",
                    "cancelled": "user_cancelled",
                }.get(target, "unknown"),
                recovery_input_ref=(
                    self.recovery_input_ref or None if target == "paused" else None
                ),
            )
        except Exception:
            # An interrupted external effect must be inspected before the run
            # can be advertised as paused, failed, or complete.
            latest = self.journal.snapshot(self.run_id)
            if latest is not None and str(latest["status"]) in {"running", "waiting_approval", "pausing"}:
                try:
                    reviewed = self.journal.transition_run(
                        self.run_id,
                        to_status="needs_review",
                        owner_instance_id=self.owner_instance_id,
                        attempt=self.attempt,
                        expected_revision=int(latest["revision"]),
                        reason_code="external_effect_unknown",
                    )
                    self.revision = int(reviewed["revision"])
                    self.status = str(reviewed["status"])
                except Exception:
                    logger.exception("Unable to mark durable run needs_review run_id=%s", self.run_id)
            raise
        self.revision = int(updated["revision"])
        self.status = str(updated["status"])
        return self.status in {"paused", "completed", "failed", "cancelled"}


def _owner_instance_id() -> str:
    from app.agent.durable.recovery import get_owner_instance_id

    return get_owner_instance_id()


def create_durable_run(
    *,
    conversation_id: str,
    execution_source: str,
    principal_id: str,
    control_session_id: str,
    workspace_dir: str,
    run_id: str | None = None,
    turn_id: str | None = None,
    db: Database | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> DurableTurnRun:
    database = db or get_db()
    journal = Journal(database)
    stable_run_id = str(run_id or uuid.uuid4().hex)
    checkpoint_turn_id = str(turn_id or uuid.uuid4().hex)
    owner = _owner_instance_id()
    dto = journal.create_run(
        run_id=stable_run_id,
        conversation_id=conversation_id,
        execution_source=execution_source,
        owner_instance_id=owner,
        turn_id=checkpoint_turn_id,
        workspace_dir=workspace_dir,
        owner_principal_id=principal_id or None,
        control_session_id=control_session_id or None,
        recovery_input_ref=None,
        lease_seconds=lease_seconds,
    )
    return DurableTurnRun(
        journal=journal,
        run_id=stable_run_id,
        owner_instance_id=owner,
        attempt=int(dto["attempt"]),
        turn_id=checkpoint_turn_id,
        revision=int(dto["revision"]),
        lease_seconds=lease_seconds,
        status=str(dto["status"]),
        recovery_input_ref=f"turn_checkpoint:{checkpoint_turn_id}",
    )


def resume_run(
    run_id: str,
    *,
    execution_source: str,
    principal_id: str,
    control_session_id: str,
    workspace_dir: str,
    db: Database | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> tuple[DurableTurnRun, dict[str, Any]]:
    """Claim a safe paused attempt for continuation without replaying its input.

    This internal primitive deliberately refuses runs whose original text,
    scope, checkpoint, or external effects cannot be reconstructed safely.
    The caller starts the loop with ``CONTINUATION_PROMPT`` and the returned
    handle; the original user message remains in persisted history.
    """
    database = db or get_db()
    journal = Journal(database)
    current = journal.snapshot(run_id)
    if current is None or current["status"] != "paused" or "resume" not in current["available_actions"]:
        raise InvalidTransitionError("run is not an eligible paused run")
    scope = database.fetchone(
        """SELECT r.conversation_id, r.workspace_dir, r.execution_source,
                  r.owner_principal_id, r.control_session_id, r.owner_instance_id,
                  r.recovery_input_ref, r.updated_at AS run_updated_at,
                  c.workspace_dir AS current_workspace_dir,
                  c.updated_at AS conversation_updated_at
           FROM agent_runs AS r JOIN conversations AS c ON c.id = r.conversation_id
           WHERE r.run_id = ?""",
        (run_id,),
    )
    if scope is None or scope["owner_instance_id"] is not None:
        raise LeaseConflictError("paused run still has an owner")
    # The trusted caller supplies a stable profile principal for desktop
    # recovery. The old control session is intentionally not part of this
    # identity check: it is process-scoped and approval tickets remain bound
    # to it. Legacy runs whose principal was their old session ID do not match
    # a newly minted profile principal and are not migrated here.
    expected_scope = (
        str(scope["workspace_dir"] or ""),
        str(scope["execution_source"] or ""),
        str(scope["owner_principal_id"] or ""),
    )
    requested_scope = (
        str(workspace_dir or ""),
        str(execution_source or ""),
        str(principal_id or ""),
    )
    if requested_scope != expected_scope:
        raise InvalidTransitionError("run scope changed since the saved checkpoint")
    if not str(control_session_id or ""):
        raise InvalidTransitionError("a current desktop control session is required")
    if str(scope["current_workspace_dir"] or "") != str(scope["workspace_dir"] or ""):
        raise InvalidTransitionError("conversation workspace changed since the saved checkpoint")
    try:
        run_updated = datetime.fromisoformat(str(scope["run_updated_at"]))
        conversation_updated = datetime.fromisoformat(str(scope["conversation_updated_at"]))
        if run_updated.tzinfo is None:
            run_updated = run_updated.replace(tzinfo=timezone.utc)
        if conversation_updated.tzinfo is None:
            conversation_updated = conversation_updated.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise InvalidTransitionError("saved conversation revision is invalid") from exc
    if conversation_updated > run_updated:
        raise InvalidTransitionError("conversation has newer turns than the saved run checkpoint")

    # Keep pointing at the original committed input across continuation
    # attempts, whose synthetic prompt may have no independent user ID.
    ref = str(scope["recovery_input_ref"] or "")
    original_turn_id = ref.removeprefix("turn_checkpoint:") if ref.startswith("turn_checkpoint:") else ""
    checkpoint_row = None
    if original_turn_id:
        checkpoint_row = database.fetchone(
            """SELECT turn_id, conversation_id, user_message_ids FROM turn_checkpoints
               WHERE turn_id = ? AND conversation_id = ?""",
            (original_turn_id, str(scope["conversation_id"])),
        )
    if checkpoint_row is None:
        checkpoint = journal.load_turn_checkpoint(run_id, int(current["attempt"]))
        checkpoint_ids = list(checkpoint["user_message_ids"]) if checkpoint is not None else []
        original_turn_id = str(checkpoint["turn_id"]) if checkpoint is not None else ""
    else:
        import json

        try:
            checkpoint_ids = json.loads(checkpoint_row["user_message_ids"])
        except (TypeError, ValueError):
            checkpoint_ids = []
        if not isinstance(checkpoint_ids, list) or any(
            isinstance(item, bool) or not isinstance(item, int) for item in checkpoint_ids
        ):
            checkpoint_ids = []
    if not checkpoint_ids:
        raise InvalidTransitionError("run has no reconstructable user checkpoint")
    checkpoint_ids = [int(message_id) for message_id in checkpoint_ids]
    placeholders = ",".join("?" for _ in checkpoint_ids)
    user_rows = database.fetchall(
        f"SELECT * FROM messages WHERE conversation_id = ? AND id IN ({placeholders})",
        (str(scope["conversation_id"]), *checkpoint_ids),
    )
    by_id = {int(row["id"]): dict(row) for row in user_rows}
    checkpoint_users = [by_id.get(message_id) for message_id in checkpoint_ids]
    if not checkpoint_users or any(row is None for row in checkpoint_users):
        raise InvalidTransitionError("saved user input is no longer available")

    turn_id = uuid.uuid4().hex
    owner = _owner_instance_id()
    latest = journal.snapshot(run_id)
    if (
        latest is None
        or latest["status"] != "paused"
        or "resume" not in latest["available_actions"]
        or int(latest["revision"]) != int(current["revision"])
    ):
        raise InvalidTransitionError("run changed while its saved checkpoint was being validated")
    claimed = journal.claim_run(
        run_id,
        owner_instance_id=owner,
        expected_revision=int(latest["revision"]),
        expected_attempt=int(latest["attempt"]),
        turn_id=turn_id,
        lease_seconds=lease_seconds,
        control_session_id=control_session_id,
    )
    handle = DurableTurnRun(
        journal=journal,
        run_id=run_id,
        owner_instance_id=owner,
        attempt=int(claimed["attempt"]),
        turn_id=turn_id,
        revision=int(claimed["revision"]),
        lease_seconds=lease_seconds,
        status=str(claimed["status"]),
        recovery_input_ref=f"turn_checkpoint:{original_turn_id}",
        is_continuation=True,
        recovery_evidence=[
            {
                "operation_id": str(operation["operation_id"]),
                "operation_kind": str(operation["operation_kind"]),
                "status": "verified",
                "input_summary": dict(operation["input_summary"]),
                "evidence": dict(operation["evidence"]),
            }
            for operation in journal.list_operations(run_id, limit=500)
            if operation["effect_class"] == "verifiable_write" and operation["status"] == "verified"
        ][-20:],
    )
    return handle, {
        "run_id": run_id,
        "attempt": int(current["attempt"]),
        "turn_id": original_turn_id,
        "conversation_id": str(scope["conversation_id"]),
        "user_message_ids": checkpoint_ids,
    }


_ACTIVE_LIFECYCLES: dict[str, Any] = {}


def register_active_lifecycle(run_id: str, lifecycle: Any) -> None:
    if run_id:
        _ACTIVE_LIFECYCLES[run_id] = lifecycle


def get_active_lifecycle(run_id: str) -> Any | None:
    """Return the live lifecycle object for a run owned by this process."""
    return _ACTIVE_LIFECYCLES.get(str(run_id or ""))


def unregister_active_lifecycle(run_id: str, lifecycle: Any) -> None:
    if _ACTIVE_LIFECYCLES.get(run_id) is lifecycle:
        _ACTIVE_LIFECYCLES.pop(run_id, None)


async def pause_run(run_id: str) -> bool:
    """Request a cooperative pause from a live owner; no HTTP surface is added."""
    lifecycle = _ACTIVE_LIFECYCLES.get(run_id)
    if lifecycle is None:
        return False
    await lifecycle.request_pause()
    return True
