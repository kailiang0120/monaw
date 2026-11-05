"""Profile-scoped HTTP access to durable desktop Runs and their events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agent.database import get_db
from app.agent.durable.journal import Journal
from app.agent.durable.models import (
    EventsExpiredError,
    InvalidCursorError,
    InvalidTransitionError,
    LeaseConflictError,
    OperationNotFoundError,
    RevisionConflictError,
    RunNotFoundError,
    sanitize_evidence,
)
from app.agent.durable.turn_run import get_active_lifecycle, pause_run, resume_run
from app.agent.job_manager import get_job
from app.agent.steering import SteeringInbox, get_steering, register_steering, unregister_steering
from app.agent.ui_events import publish_ui_event
from app.agent.conversation_workspace import reset_workspace_dir, set_workspace_dir
from app.agent.run_context import (
    reset_current_control_session_id,
    reset_current_conversation_id,
    reset_current_execution_source,
    reset_current_interactive,
    reset_current_permission_profile_id,
    reset_current_principal_id,
    set_current_control_session_id,
    set_current_conversation_id,
    set_current_execution_source,
    set_current_interactive,
    set_current_permission_profile_id,
    set_current_principal_id,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/runs")

_DESKTOP_SOURCES = ("desktop", "desktop_stream", "desktop_job")
_UNRESOLVED_OPERATION_STATUSES = frozenset(
    {"prepared", "authorized", "executing", "uncertain", "needs_review"}
)
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
_IDEMPOTENCY_CLAIM_SECONDS = 120


class RunCommandIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(..., ge=0)
    expected_attempt: int | None = Field(default=None, ge=1)


class RunOperationResolveIn(RunCommandIn):
    resolution: Literal["verify", "skip"]


def _raise(status_code: int, code: str, message: str, **details: Any) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": details},
    )


def _profile_id(request: Request) -> str:
    session = getattr(request.state, "control_session", None)
    profile_id = str(getattr(session, "profile_id", None) or "")
    if not profile_id:
        _raise(403, "profile_required", "A signed desktop profile is required for Run access.")
    return profile_id


def _session_id(request: Request) -> str:
    session = getattr(request.state, "control_session", None)
    return str(getattr(session, "session_id", None) or "")


def _public_snapshot(dto: dict[str, Any], scope: dict[str, Any], request: Request) -> dict[str, Any]:
    """Advertise only actions this API can safely route for this owner/session."""
    snapshot = dict(dto)
    actions = list(snapshot.get("available_actions") or [])
    # Interactive resolution is still handled by the existing approval API;
    # durable operation verification has no safe public resolver yet.
    if not scope.get("verify_available"):
        actions = [action for action in actions if action != "verify"]
    current_session = _session_id(request)
    same_live_session = (
        bool(current_session)
        and str(scope.get("control_session_id") or "") == current_session
        and get_active_lifecycle(str(snapshot.get("run_id") or "")) is not None
    )
    if not same_live_session:
        actions = [action for action in actions if action not in {"pause", "stop", "approve", "reject"}]
    snapshot["available_actions"] = actions
    return snapshot


def _authorized_scope_and_snapshot(run_id: str, request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
    """Authorize by signed profile and desktop source before exposing Run data."""
    profile_id = _profile_id(request)
    db = get_db()
    journal = Journal(db)
    placeholders = ",".join("?" for _ in _DESKTOP_SOURCES)
    with db.transaction(immediate=False) as conn:
        row = conn.execute(
            f"""SELECT r.*, c.workspace_dir AS current_workspace_dir
                FROM agent_runs AS r
                JOIN conversations AS c ON c.id = r.conversation_id
                WHERE r.run_id = ? AND r.owner_principal_id = ?
                  AND r.execution_source IN ({placeholders})""",
            (run_id, profile_id, *_DESKTOP_SOURCES),
        ).fetchone()
        if row is None:
            _raise(404, "not_found", "Run not found.")
        scope = {
            "run_id": str(row["run_id"]),
            "conversation_id": str(row["conversation_id"]),
            "workspace_dir": str(row["workspace_dir"] or ""),
            "current_workspace_dir": str(row["current_workspace_dir"] or ""),
            "execution_source": str(row["execution_source"] or ""),
            "owner_principal_id": str(row["owner_principal_id"] or ""),
            "control_session_id": str(row["control_session_id"] or ""),
        }
        if scope["workspace_dir"] != scope["current_workspace_dir"]:
            _raise(409, "workspace_changed", "The Run workspace changed after it was created.")
        snapshot = dict(journal._run_dto(conn, row))
        scope["verify_available"] = _verify_capability(conn, row)
        if scope["verify_available"] and "verify" not in snapshot["available_actions"]:
            snapshot["available_actions"].append("verify")
    return scope, snapshot


def _authorized_list(request: Request, *, conversation_id: str | None, limit: int, after_run_id: str) -> list[dict[str, Any]]:
    profile_id = _profile_id(request)
    db = get_db()
    journal = Journal(db)
    placeholders = ",".join("?" for _ in _DESKTOP_SOURCES)
    filters = [
        f"r.owner_principal_id = ?",
        f"r.execution_source IN ({placeholders})",
        "r.workspace_dir = COALESCE(c.workspace_dir, '')",
    ]
    params: list[Any] = [profile_id, *_DESKTOP_SOURCES]
    if after_run_id:
        filters.append("r.run_id > ?")
        params.append(after_run_id)
    if conversation_id:
        filters.append("r.conversation_id = ?")
        params.append(conversation_id)
    params.append(limit)
    with db.transaction(immediate=False) as conn:
        rows = conn.execute(
            f"""SELECT r.* FROM agent_runs AS r
                JOIN conversations AS c ON c.id = r.conversation_id
                WHERE {' AND '.join(filters)}
                ORDER BY r.run_id ASC LIMIT ?""",
            tuple(params),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            dto = dict(journal._run_dto(conn, row))
            verify_available = _verify_capability(conn, row)
            if verify_available and "verify" not in dto["available_actions"]:
                dto["available_actions"].append("verify")
            scope = {
                "run_id": str(row["run_id"]),
                "control_session_id": str(row["control_session_id"] or ""),
                "verify_available": verify_available,
            }
            result.append(_public_snapshot(dto, scope, request))
        return result


def _verify_capability(conn, row) -> bool:
    if (
        str(row["status"] or "") not in {"interrupted", "needs_review"}
        or get_active_lifecycle(str(row["run_id"])) is not None
    ):
        return False
    try:
        from app.agent.durable.recovery import get_owner_instance_id

        current_owner = get_owner_instance_id()
    except Exception:
        return False
    if not current_owner or str(row["owner_instance_id"] or "") != current_owner:
        return False
    return conn.execute(
        """SELECT 1 FROM run_operations WHERE run_id = ?
           AND effect_class = 'verifiable_write'
           AND status IN ('executing', 'uncertain', 'needs_review')
           AND snapshot_ref IS NOT NULL LIMIT 1""",
        (str(row["run_id"]),),
    ).fetchone() is not None


@contextmanager
def _run_verification_context(scope: dict[str, Any], *, current_session_id: str):
    """Bind the read-only verifier to the authenticated Run's execution scope."""
    workspace_token = set_workspace_dir(str(scope["workspace_dir"] or ""))
    conversation_token = set_current_conversation_id(str(scope["conversation_id"]))
    session_token = set_current_control_session_id(current_session_id)
    source_token = set_current_execution_source(str(scope["execution_source"]))
    principal_token = set_current_principal_id(str(scope["owner_principal_id"]))
    permission_token = set_current_permission_profile_id("desktop-current")
    interactive_token = set_current_interactive(True)
    try:
        yield
    finally:
        reset_current_interactive(interactive_token)
        reset_current_permission_profile_id(permission_token)
        reset_current_principal_id(principal_token)
        reset_current_execution_source(source_token)
        reset_current_control_session_id(session_token)
        reset_current_conversation_id(conversation_token)
        reset_workspace_dir(workspace_token)


def _verify_operation_in_scope(
    verifier: Any,
    private_operation: dict[str, Any],
    scope: dict[str, Any],
    current_session_id: str,
) -> dict[str, Any]:
    with _run_verification_context(scope, current_session_id=current_session_id):
        result = verifier.verify(private_operation)
    return dict(result) if isinstance(result, dict) else {"verification_result": "unknown"}


def _validated_idempotency_key(value: str | None) -> str:
    key = str(value or "").strip()
    if not key or len(key) > 200 or any(ord(char) < 0x21 or ord(char) > 0x7E for char in key):
        _raise(422, "validation_error", "Idempotency-Key must be 1 to 200 printable ASCII characters.")
    return key


def _payload_digest(run_id: str, action: str, body: RunCommandIn) -> str:
    canonical = json.dumps(
        {
            "run_id": run_id,
            "action": action,
            "expected_revision": body.expected_revision,
            "expected_attempt": body.expected_attempt,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _operation_payload_digest(
    run_id: str, operation_id: str, body: RunOperationResolveIn
) -> str:
    canonical = json.dumps(
        {
            "run_id": run_id,
            "operation_id": operation_id,
            "action": "resolve",
            "resolution": body.resolution,
            "expected_revision": body.expected_revision,
            "expected_attempt": body.expected_attempt,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_idempotency_record(
    *, principal_hash: str, action: str, run_id: str, key_hash: str, resource_id: str = ""
) -> dict[str, Any] | None:
    row = get_db().fetchone(
        """SELECT payload_hash, state, response_json, claim_owner, claim_lease_expires_at
           FROM run_command_idempotency
           WHERE principal_hash = ? AND action = ? AND run_id = ?
             AND resource_id = ? AND idempotency_key_hash = ?""",
        (principal_hash, action, run_id, resource_id, key_hash),
    )
    return dict(row) if row is not None else None


def _reserve_idempotency(
    *,
    profile_id: str,
    action: str,
    run_id: str,
    resource_id: str = "",
    key_hash: str,
    payload_hash: str,
    expected_revision: int,
    expected_attempt: int | None,
    claim_owner: str,
) -> dict[str, Any]:
    """Atomically reserve a key after rechecking profile, workspace and CAS."""
    db = get_db()
    principal_hash = _key_hash(profile_id)
    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    lease_text = (now + timedelta(seconds=_IDEMPOTENCY_CLAIM_SECONDS)).isoformat()
    placeholders = ",".join("?" for _ in _DESKTOP_SOURCES)
    with db.transaction() as conn:
        existing = conn.execute(
            """SELECT payload_hash, state, response_json, claim_owner, claim_lease_expires_at
               FROM run_command_idempotency
               WHERE principal_hash = ? AND action = ? AND run_id = ?
                 AND resource_id = ? AND idempotency_key_hash = ?""",
            (principal_hash, action, run_id, resource_id, key_hash),
        ).fetchone()
        if existing is not None:
            if str(existing["payload_hash"]) != payload_hash:
                _raise(409, "idempotency_conflict", "Idempotency-Key was reused with a different request.")
            return dict(existing)

        run = conn.execute(
            f"""SELECT r.revision, r.attempt, r.workspace_dir, r.status,
                       c.workspace_dir AS current_workspace_dir
                FROM agent_runs AS r JOIN conversations AS c ON c.id = r.conversation_id
                WHERE r.run_id = ? AND r.owner_principal_id = ?
                  AND r.execution_source IN ({placeholders})""",
            (run_id, profile_id, *_DESKTOP_SOURCES),
        ).fetchone()
        if run is None:
            _raise(404, "not_found", "Run not found.")
        if str(run["workspace_dir"] or "") != str(run["current_workspace_dir"] or ""):
            _raise(409, "workspace_changed", "The Run workspace changed after it was created.")
        if int(run["revision"]) != expected_revision or (
            expected_attempt is not None and int(run["attempt"]) != expected_attempt
        ):
            _raise(409, "revision_conflict", "The Run changed since the client last read it.")
        conn.execute(
            """INSERT INTO run_command_idempotency (
                   principal_hash, action, run_id, resource_id, idempotency_key_hash,
                   payload_hash, state, response_json, claim_owner,
                   claim_lease_expires_at, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?, ?)""",
            (
                principal_hash,
                action,
                run_id,
                resource_id,
                key_hash,
                payload_hash,
                claim_owner,
                lease_text,
                now_text,
                now_text,
            ),
        )
        return {
            "payload_hash": payload_hash,
            "state": "pending",
            "response_json": None,
            "claim_owner": claim_owner,
            "claim_lease_expires_at": lease_text,
            "reserved": True,
        }


def _complete_idempotency(
    *, principal_hash: str, action: str, run_id: str, key_hash: str, resource_id: str = "",
    payload_hash: str, response: dict[str, Any],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    with get_db().transaction() as conn:
        conn.execute(
            """UPDATE run_command_idempotency
               SET state = 'completed', response_json = ?, claim_owner = NULL,
                   claim_lease_expires_at = NULL, updated_at = ?
               WHERE principal_hash = ? AND action = ? AND run_id = ?
                 AND resource_id = ? AND idempotency_key_hash = ?
                 AND payload_hash = ? AND state = 'pending'""",
            (encoded, now, principal_hash, action, run_id, resource_id, key_hash, payload_hash),
        )
        changed = int(conn.execute("SELECT changes()").fetchone()[0])
        if changed != 1:
            raise RuntimeError("Run command idempotency claim was lost before response commit")


def _mark_idempotency_uncertain(
    *, principal_hash: str, action: str, run_id: str, key_hash: str,
    payload_hash: str, resource_id: str = "",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db().transaction() as conn:
        conn.execute(
            """UPDATE run_command_idempotency
               SET state = 'uncertain', claim_owner = NULL,
                   claim_lease_expires_at = NULL, updated_at = ?
               WHERE principal_hash = ? AND action = ? AND run_id = ?
                 AND resource_id = ? AND idempotency_key_hash = ?
                 AND payload_hash = ? AND state = 'pending'""",
            (now, principal_hash, action, run_id, resource_id, key_hash, payload_hash),
        )


async def _resolve_existing_idempotency(
    *, record: dict[str, Any], payload_hash: str, run_id: str, request: Request,
    principal_hash: str, action: str, key_hash: str,
    resource_id: str = "",
) -> dict[str, Any]:
    def current_run_details() -> dict[str, Any]:
        scope, snapshot = _authorized_scope_and_snapshot(run_id, request)
        return _public_snapshot(snapshot, scope, request)

    if str(record.get("payload_hash") or "") != payload_hash:
        _raise(409, "idempotency_conflict", "Idempotency-Key was reused with a different request.")
    if str(record.get("state") or "") == "completed":
        try:
            response = json.loads(str(record.get("response_json") or ""))
        except (TypeError, json.JSONDecodeError):
            _raise(409, "idempotency_outcome_unknown", "The saved command result cannot be read; inspect the Run state.")
        if isinstance(response, dict):
            return response
        _raise(409, "idempotency_outcome_unknown", "The saved command result is unavailable; inspect the Run state.")
    if str(record.get("state") or "") == "uncertain":
        _raise(
            409,
            "idempotency_outcome_unknown",
            "The command outcome is uncertain. Inspect the Run before issuing a new command.",
            recovery_url=f"/api/runs/{run_id}/recovery",
            current_run=current_run_details(),
        )

    try:
        from app.agent.durable.recovery import get_owner_instance_id

        owner = get_owner_instance_id()
    except Exception:
        owner = ""
    lease_expiry = str(record.get("claim_lease_expires_at") or "")
    try:
        parsed_expiry = datetime.fromisoformat(lease_expiry)
        if parsed_expiry.tzinfo is None:
            parsed_expiry = parsed_expiry.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        parsed_expiry = datetime.min.replace(tzinfo=timezone.utc)
    if str(record.get("claim_owner") or "") != owner or parsed_expiry <= datetime.now(timezone.utc):
        _raise(
            409,
            "idempotency_outcome_unknown",
            "The command did not save a result before its owner ended. Inspect the Run before issuing a new command.",
            recovery_url=f"/api/runs/{run_id}/recovery",
            current_run=current_run_details(),
        )

    # A same-process duplicate may arrive while the first request is still
    # completing. Poll the durable row; never run the action a second time.
    for _ in range(100):
        await asyncio.sleep(0.05)
        current = _load_idempotency_record(
            principal_hash=principal_hash,
            action=action,
            run_id=run_id,
            key_hash=key_hash,
            resource_id=resource_id,
        )
        if current is None:
            break
        if str(current.get("payload_hash") or "") != payload_hash:
            _raise(409, "idempotency_conflict", "Idempotency-Key was reused with a different request.")
        if str(current.get("state") or "") == "completed":
            try:
                response = json.loads(str(current.get("response_json") or ""))
            except (TypeError, json.JSONDecodeError):
                break
            if isinstance(response, dict):
                return response
            break
        if str(current.get("state") or "") == "uncertain":
            _raise(
                409,
                "idempotency_outcome_unknown",
                "The command outcome is uncertain. Inspect the Run before issuing a new command.",
                recovery_url=f"/api/runs/{run_id}/recovery",
                current_run=current_run_details(),
            )
    _raise(
        409,
        "idempotency_in_progress",
        "The original command is still being processed. Retry this key or inspect the Run state.",
        recovery_url=f"/api/runs/{run_id}/recovery",
        current_run=current_run_details(),
    )


def _live_task(run_id: str, session_id: str) -> asyncio.Task | None:
    inbox = get_steering(run_id, session_id)
    if inbox is not None:
        task = inbox.task
        if isinstance(task, asyncio.Task) and not task.done():
            return task
    job = get_job(run_id)
    task = getattr(job, "_task", None) if job is not None else None
    if isinstance(task, asyncio.Task) and not task.done():
        return task
    return None


def _start_resume_worker(durable_run: Any, resume_input: dict[str, Any]) -> None:
    run_id = str(durable_run.run_id)
    scope = dict(resume_input["scope"])
    session_id = str(resume_input["session_id"])
    profile_id = str(resume_input["profile_id"])
    merged_settings = resume_input["settings"]
    inbox = SteeringInbox(
        scope["conversation_id"],
        session_id,
        run_id=run_id,
        display_message="Continue previous task",
        continuation=True,
    )
    try:
        register_steering(inbox)
    except ValueError as exc:
        # The durable attempt is already claimed. Keep the failure visible as a
        # failed durable turn rather than leaving a running row without a worker.
        try:
            durable_run.finalize("error", "run_failed")
        except Exception:
            logger.exception("Unable to finalize duplicate resumed run run_id=%s", run_id)
        logger.warning("Unable to register resumed Run inbox run_id=%s error=%s", run_id, type(exc).__name__)
        return

    conversation_id = scope["conversation_id"]
    # Like a chat turn: the sidebar marks the chat as running, and a viewer that
    # follows this inbox gets the steering target before any other event.
    publish_ui_event("conversation.changed", {"conversation_id": conversation_id, "action": "run_started"})
    inbox.stream.publish("turn_started", {"run_id": run_id, "conversation_id": conversation_id})

    async def drive() -> None:
        try:
            from app.agent.runtime import CONTINUATION_PROMPT, run_agent_stream

            async for event in run_agent_stream(
                message=CONTINUATION_PROMPT,
                conversation_id=scope["conversation_id"],
                settings=merged_settings,
                attachments=None,
                control_session_id=session_id,
                execution_source=scope["execution_source"],
                principal_id=profile_id,
                permission_profile_id="desktop-current",
                interactive=True,
                steering=inbox,
                durable_run=durable_run,
            ):
                event_name = str(event.get("event") or "message")
                if event_name == "done":
                    publish_ui_event("conversation.changed", {"conversation_id": conversation_id, "action": "messages_changed"})
                    publish_ui_event("usage.changed", {"conversation_id": conversation_id})
                    publish_ui_event("observability.changed", {"conversation_id": conversation_id})
                inbox.stream.publish(event_name, event.get("data") or {})
                if event_name == "done":
                    break
        except asyncio.CancelledError:
            publish_ui_event("conversation.changed", {"conversation_id": conversation_id, "action": "messages_changed"})
            raise
        except Exception:
            logger.exception("Resumed durable Run failed run_id=%s", run_id)
        finally:
            unregister_steering(inbox)
            inbox.stream.close()
            publish_ui_event("conversation.changed", {"conversation_id": conversation_id, "action": "run_finished"})

    inbox.task = asyncio.create_task(drive(), name=f"resume-run-{run_id}")


def _events_page(journal: Journal, run_id: str, *, after_seq: int, limit: int) -> dict[str, Any]:
    try:
        return dict(journal.read_events(run_id, after_seq=after_seq, limit=limit))
    except EventsExpiredError:
        _raise(410, "events_expired", "The requested Run events are no longer retained.")
    except InvalidCursorError as exc:
        _raise(400, "invalid_cursor", str(exc))
    except RunNotFoundError:
        _raise(404, "not_found", "Run not found.")


def _operations_page(journal: Journal, run_id: str, *, after_sequence: int, limit: int):
    try:
        return journal.list_operations(run_id, limit=limit, after_sequence=after_sequence)
    except RunNotFoundError:
        _raise(404, "not_found", "Run not found.")


def _record_unknown_operation_verification(
    journal: Journal,
    *,
    run_id: str,
    operation_id: str,
    owner_instance_id: str,
    attempt: int,
    expected_revision: int,
    expected_scope: dict[str, str],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Persist a read-only inconclusive check as a revisioned Run event."""
    from app.agent.durable.journal import _decode_json, _safe_json, _timestamp

    safe_added = sanitize_evidence(evidence)
    now = _timestamp()
    with journal.db.transaction() as conn:
        run = journal._require_run(conn, run_id)
        journal._assert_owner(run, owner_instance_id, attempt)
        journal._assert_expected_scope(conn, run, expected_scope)
        journal._expect_revision(run, expected_revision)
        operation = conn.execute(
            "SELECT * FROM run_operations WHERE run_id = ? AND operation_id = ?",
            (run_id, operation_id),
        ).fetchone()
        if operation is None:
            _raise(404, "not_found", "Operation not found.")
        if int(operation["attempt"]) != attempt:
            _raise(409, "revision_conflict", "Operation belongs to a stale Run attempt.")
        current_status = str(operation["status"])
        if current_status not in {"executing", "uncertain", "needs_review"}:
            _raise(409, "operation_not_resolvable", "Operation is no longer awaiting verification.")
        combined = _decode_json(operation["evidence_json"])
        combined.update(safe_added)
        revision = int(run["revision"]) + 1
        conn.execute(
            """UPDATE run_operations SET evidence_json = ?, reason_code = 'verification_failed',
                   updated_at = ? WHERE run_id = ? AND operation_id = ? AND attempt = ? AND status = ?""",
            (_safe_json(combined), now, run_id, operation_id, attempt, current_status),
        )
        journal._require_rowcount(conn, "operation verification record lost its state fence")
        conn.execute(
            "UPDATE agent_runs SET revision = ?, updated_at = ? WHERE run_id = ? AND revision = ?",
            (revision, now, run_id, expected_revision),
        )
        journal._require_rowcount(conn, "operation verification lost its Run revision fence")
        journal._append_event(
            conn,
            run_id,
            "operation.updated",
            {
                "operation_id": operation_id,
                "sequence": int(operation["sequence_no"]),
                "attempt": attempt,
                "status": current_status,
                "reason_code": "verification_failed",
                "evidence": combined,
                "revision": revision,
            },
            at=now,
        )
        updated = conn.execute(
            "SELECT * FROM run_operations WHERE run_id = ? AND operation_id = ?",
            (run_id, operation_id),
        ).fetchone()
        return journal._operation_dto(updated)


def _pause_run_after_verified_resolution(
    journal: Journal, *, run_id: str, owner_instance_id: str,
    expected_scope: dict[str, str],
) -> dict[str, Any] | None:
    latest = journal.snapshot(run_id)
    internal = journal.load_run_internal(run_id)
    if (
        latest is None
        or internal is None
        or str(latest["status"]) not in {"interrupted", "needs_review"}
        or str(internal["owner_instance_id"] or "") != owner_instance_id
    ):
        return latest
    with journal.db.transaction(immediate=False) as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None or not journal._has_safe_resume_input(conn, row):
            return latest
        unresolved = conn.execute(
            """SELECT 1 FROM run_operations WHERE run_id = ?
               AND effect_class != 'read_only'
               AND status IN ('prepared', 'authorized', 'executing', 'uncertain', 'needs_review')
               LIMIT 1""",
            (run_id,),
        ).fetchone()
        if unresolved is not None:
            return latest
    try:
        return journal.transition_run(
            run_id,
            to_status="paused",
            owner_instance_id=owner_instance_id,
            attempt=int(latest["attempt"]),
            expected_revision=int(latest["revision"]),
            expected_scope=expected_scope,
            reason_code="recovery_checked",
        )
    except (InvalidTransitionError, LeaseConflictError, RevisionConflictError):
        # The result remains in the operation journal; a competing update
        # keeps the Run at its newer state and is reported on the next read.
        return journal.snapshot(run_id)


def _sse_bytes(event: dict[str, Any]) -> bytes:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n".encode("utf-8")


@router.get("")
async def list_runs(
    request: Request,
    conversation_id: str | None = Query(default=None, min_length=1, max_length=128),
    limit: int = Query(default=100, ge=1, le=500),
    after_run_id: str = Query(default="", max_length=128),
):
    runs = _authorized_list(
        request,
        conversation_id=conversation_id,
        limit=limit + 1,
        after_run_id=after_run_id,
    )
    has_more = len(runs) > limit
    runs = runs[:limit]
    return {
        "runs": runs,
        "next_run_id": str(runs[-1]["run_id"]) if has_more and runs else None,
        "has_more": has_more,
    }


@router.get("/{run_id}")
async def get_run(run_id: str, request: Request):
    scope, snapshot = _authorized_scope_and_snapshot(run_id, request)
    return _public_snapshot(snapshot, scope, request)


@router.get("/{run_id}/operations")
async def list_run_operations(
    run_id: str,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    scope, _snapshot = _authorized_scope_and_snapshot(run_id, request)
    journal = Journal(get_db())
    operations = _operations_page(journal, run_id, limit=limit, after_sequence=after_sequence)
    next_sequence = int(operations[-1]["sequence"]) if operations else after_sequence
    return {
        "run_id": run_id,
        "operations": operations,
        "next_sequence": next_sequence,
        "has_more": len(operations) == limit,
        "conversation_id": scope["conversation_id"],
    }


@router.get("/{run_id}/recovery")
async def get_run_recovery(run_id: str, request: Request):
    scope, snapshot = _authorized_scope_and_snapshot(run_id, request)
    journal = Journal(get_db())
    operations = _operations_page(journal, run_id, limit=500, after_sequence=0)
    unresolved = [
        operation
        for operation in operations
        if str(operation.get("status") or "") in _UNRESOLVED_OPERATION_STATUSES
    ]
    return {
        "run_id": run_id,
        "run": _public_snapshot(snapshot, scope, request),
        "unresolved_operations": unresolved,
        "operations_next_sequence": int(operations[-1]["sequence"]) if operations else 0,
        "operations_truncated": len(operations) == 500,
    }


@router.post("/{run_id}/operations/{operation_id}/resolve", status_code=202)
async def resolve_run_operation(
    run_id: str,
    operation_id: str,
    body: RunOperationResolveIn,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    scope, initial_snapshot = _authorized_scope_and_snapshot(run_id, request)
    profile_id = _profile_id(request)
    key = _validated_idempotency_key(idempotency_key)
    payload_hash = _operation_payload_digest(run_id, operation_id, body)
    principal_hash = _key_hash(profile_id)
    key_hash = _key_hash(key)
    resource_id = operation_id

    recorded = _load_idempotency_record(
        principal_hash=principal_hash,
        action="resolve",
        run_id=run_id,
        resource_id=resource_id,
        key_hash=key_hash,
    )
    if recorded is not None:
        return await _resolve_existing_idempotency(
            record=recorded,
            payload_hash=payload_hash,
            run_id=run_id,
            request=request,
            principal_hash=principal_hash,
            action="resolve",
            resource_id=resource_id,
            key_hash=key_hash,
        )

    snapshot = _public_snapshot(initial_snapshot, scope, request)
    if int(snapshot["revision"]) != body.expected_revision or (
        body.expected_attempt is not None and int(snapshot["attempt"]) != body.expected_attempt
    ):
        _raise(
            409,
            "revision_conflict",
            "The Run changed since the client last read it.",
            current_run=snapshot,
        )
    if body.resolution == "skip":
        _raise(
            409,
            "skip_unavailable",
            "Skip requires evidence that the uncertain effect is isolated and cannot be repeated.",
        )
    if body.resolution != "verify" or "verify" not in snapshot["available_actions"]:
        _raise(409, "verification_unavailable", "This Run has no safely verifiable operation.", current_run=snapshot)
    if str(snapshot["status"]) not in {"interrupted", "needs_review"}:
        _raise(409, "verification_unavailable", "Only an interrupted Run can be verified.", current_run=snapshot)
    if get_active_lifecycle(run_id) is not None:
        _raise(409, "run_not_live", "Wait until the Run worker has stopped before verifying its effects.")

    journal = Journal(get_db())
    private_operation = journal.load_operation_internal(run_id, operation_id)
    if private_operation is None:
        _raise(404, "not_found", "Operation not found.")
    if (
        str(private_operation.get("effect_class") or "") != "verifiable_write"
        or not private_operation.get("snapshot_ref")
        or str(private_operation.get("status") or "") not in {"executing", "uncertain", "needs_review"}
        or int(private_operation.get("attempt") or 0) != int(snapshot["attempt"])
    ):
        _raise(409, "operation_not_resolvable", "Operation is not eligible for read-only verification.")

    try:
        from app.agent.durable.recovery import get_owner_instance_id

        owner_instance_id = get_owner_instance_id()
    except Exception:
        owner_instance_id = ""
    run_internal = journal.load_run_internal(run_id)
    if (
        not owner_instance_id
        or run_internal is None
        or str(run_internal.get("owner_instance_id") or "") != owner_instance_id
        or int(run_internal.get("attempt") or 0) != int(snapshot["attempt"])
    ):
        _raise(409, "owner_unavailable", "This Run has no verifiable recovery owner in the current backend process.")

    try:
        from app.agent.file_mutation.verifier import get_file_mutation_verifier

        verifier = get_file_mutation_verifier()
    except Exception:
        _raise(503, "verification_unavailable", "The read-only file verifier is unavailable.")

    try:
        claim_owner = get_owner_instance_id()
    except Exception:
        _raise(503, "service_unavailable", "Run ownership is unavailable.")
    reservation = _reserve_idempotency(
        profile_id=profile_id,
        action="resolve",
        run_id=run_id,
        resource_id=resource_id,
        key_hash=key_hash,
        payload_hash=payload_hash,
        expected_revision=body.expected_revision,
        expected_attempt=body.expected_attempt,
        claim_owner=claim_owner,
    )
    if not reservation.get("reserved"):
        return await _resolve_existing_idempotency(
            record=reservation,
            payload_hash=payload_hash,
            run_id=run_id,
            request=request,
            principal_hash=principal_hash,
            action="resolve",
            resource_id=resource_id,
            key_hash=key_hash,
        )

    try:
        latest_operation = journal.load_operation_internal(run_id, operation_id)
        latest_run = journal.snapshot(run_id)
        if (
            latest_operation is None
            or latest_run is None
            or int(latest_run["revision"]) != body.expected_revision
            or int(latest_run["attempt"]) != int(snapshot["attempt"])
            or str(latest_operation.get("status") or "")
            not in {"executing", "uncertain", "needs_review"}
            or int(latest_operation.get("attempt") or 0) != int(snapshot["attempt"])
        ):
            _raise(409, "revision_conflict", "The Run or operation changed before verification began.")
        current_session = _session_id(request)
        if not current_session:
            _raise(403, "control_session_required", "A signed desktop control session is required for verification.")
        verification = await asyncio.to_thread(
            _verify_operation_in_scope,
            verifier,
            latest_operation,
            scope,
            current_session,
        )
        session = getattr(request.state, "control_session", None)
        if int(getattr(session, "expires_at", 0) or 0) <= int(time.time()):
            _raise(401, "unauthorized", "Refresh the desktop control token before resolving this operation.")
        current_scope, current_snapshot = _authorized_scope_and_snapshot(run_id, request)
        if (
            int(current_snapshot["revision"]) != body.expected_revision
            or int(current_snapshot["attempt"]) != int(snapshot["attempt"])
            or current_scope["workspace_dir"] != scope["workspace_dir"]
            or current_scope["execution_source"] != scope["execution_source"]
            or current_scope["owner_principal_id"] != scope["owner_principal_id"]
        ):
            _raise(409, "revision_conflict", "Run scope changed while verification was in progress.")
        result = str(verification.get("verification_result") or "unknown")
        expected_scope = {
            "owner_principal_id": profile_id,
            "execution_source": scope["execution_source"],
            "workspace_dir": scope["workspace_dir"],
            "conversation_workspace_dir": scope["workspace_dir"],
        }
        if result == "completed":
            try:
                operation = journal.transition_operation(
                    run_id,
                    operation_id,
                    to_status="verified",
                    owner_instance_id=owner_instance_id,
                    attempt=int(snapshot["attempt"]),
                    expected_revision=body.expected_revision,
                    expected_scope=expected_scope,
                    reason_code="verified",
                    evidence=verification,
                )
            except (RunNotFoundError, OperationNotFoundError):
                _raise(404, "not_found", "Run or operation not found.")
            except (RevisionConflictError, LeaseConflictError, InvalidTransitionError):
                _raise(409, "revision_conflict", "Run scope or operation changed before verification could be committed.")
            _pause_run_after_verified_resolution(
                journal,
                run_id=run_id,
                owner_instance_id=owner_instance_id,
                expected_scope=expected_scope,
            )
        elif result == "unchanged":
            try:
                operation = journal.transition_operation(
                    run_id,
                    operation_id,
                    to_status="failed",
                    owner_instance_id=owner_instance_id,
                    attempt=int(snapshot["attempt"]),
                    expected_revision=body.expected_revision,
                    expected_scope=expected_scope,
                    reason_code="safe_to_retry",
                    evidence=verification,
                )
            except (RunNotFoundError, OperationNotFoundError):
                _raise(404, "not_found", "Run or operation not found.")
            except (RevisionConflictError, LeaseConflictError, InvalidTransitionError):
                _raise(409, "revision_conflict", "Run scope or operation changed before verification could be committed.")
            _pause_run_after_verified_resolution(
                journal,
                run_id=run_id,
                owner_instance_id=owner_instance_id,
                expected_scope=expected_scope,
            )
        else:
            evidence = {"verification_result": "unknown"}
            try:
                operation = _record_unknown_operation_verification(
                    journal,
                    run_id=run_id,
                    operation_id=operation_id,
                    owner_instance_id=owner_instance_id,
                    attempt=int(snapshot["attempt"]),
                    expected_revision=body.expected_revision,
                    expected_scope=expected_scope,
                    evidence=evidence,
                )
            except (RunNotFoundError, OperationNotFoundError):
                _raise(404, "not_found", "Run or operation not found.")
            except (RevisionConflictError, LeaseConflictError, InvalidTransitionError):
                _raise(409, "revision_conflict", "Run scope or operation changed before verification could be recorded.")

        latest_scope, latest = _authorized_scope_and_snapshot(run_id, request)
        response = {
            "accepted": True,
            "verification_result": result if result in {"completed", "unchanged"} else "unknown",
            "run": _public_snapshot(latest, latest_scope, request),
            "operation": operation,
        }
        _complete_idempotency(
            principal_hash=principal_hash,
            action="resolve",
            run_id=run_id,
            resource_id=resource_id,
            key_hash=key_hash,
            payload_hash=payload_hash,
            response=response,
        )
        return response
    except Exception:
        _mark_idempotency_uncertain(
            principal_hash=principal_hash,
            action="resolve",
            run_id=run_id,
            resource_id=resource_id,
            key_hash=key_hash,
            payload_hash=payload_hash,
        )
        raise


@router.get("/{run_id}/events")
async def list_run_events(
    run_id: str,
    request: Request,
    after_seq: int = Query(default=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    _scope, _snapshot = _authorized_scope_and_snapshot(run_id, request)
    return _events_page(Journal(get_db()), run_id, after_seq=after_seq, limit=limit)


@router.get("/{run_id}/events/stream")
async def stream_run_events(
    run_id: str,
    request: Request,
    after_seq: int = Query(default=0),
):
    _scope, _snapshot = _authorized_scope_and_snapshot(run_id, request)
    journal = Journal(get_db())
    # Validate the cursor before headers are sent so expiry is an HTTP 410.
    _events_page(journal, run_id, after_seq=after_seq, limit=100)

    async def stream() -> AsyncIterator[bytes]:
        cursor = after_seq
        last_heartbeat = time.monotonic()
        while not await request.is_disconnected():
            session = getattr(request.state, "control_session", None)
            if int(getattr(session, "expires_at", 0) or 0) <= int(time.time()):
                payload = json.dumps({"code": "auth_expired", "message": "Reconnect with a refreshed control token."})
                yield f"event: reset\ndata: {payload}\n\n".encode("utf-8")
                return
            try:
                _scope, current_snapshot = _authorized_scope_and_snapshot(run_id, request)
            except HTTPException:
                return
            try:
                page = journal.read_events(run_id, after_seq=cursor, limit=100)
            except EventsExpiredError:
                payload = json.dumps({"code": "events_expired", "message": "Reload the Run snapshot."})
                yield f"event: reset\ndata: {payload}\n\n".encode("utf-8")
                return
            except (InvalidCursorError, LookupError):
                return
            events = list(page["events"])
            for event in events:
                cursor = int(event["seq"])
                yield _sse_bytes(event)
            if not events and cursor >= int(page["last_seq"]):
                if (
                    str(current_snapshot["status"]) in _TERMINAL_RUN_STATUSES
                    and int(current_snapshot["last_seq"]) <= cursor
                ):
                    return
                now = time.monotonic()
                if now - last_heartbeat >= 15.0:
                    yield b": keepalive\n\n"
                    last_heartbeat = now
                await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _run_command(
    action: str,
    run_id: str,
    body: RunCommandIn,
    request: Request,
    idempotency_key: str | None,
) -> dict[str, Any]:
    scope, initial_snapshot = _authorized_scope_and_snapshot(run_id, request)
    profile_id = _profile_id(request)
    key = _validated_idempotency_key(idempotency_key)
    digest = _payload_digest(run_id, action, body)
    principal_hash = _key_hash(profile_id)
    key_hash = _key_hash(key)

    # The profile/source/workspace authorization above deliberately precedes
    # every durable idempotency read. A completed response survives restart;
    # an orphaned claim is inspected and never re-executed.
    recorded = _load_idempotency_record(
        principal_hash=principal_hash,
        action=action,
        run_id=run_id,
        key_hash=key_hash,
    )
    if recorded is not None:
        return await _resolve_existing_idempotency(
            record=recorded,
            payload_hash=digest,
            run_id=run_id,
            request=request,
            principal_hash=principal_hash,
            action=action,
            key_hash=key_hash,
        )

    snapshot = _public_snapshot(initial_snapshot, scope, request)
    if body.expected_revision != int(snapshot["revision"]):
        _raise(
            409,
            "revision_conflict",
            "The Run changed since the client last read it.",
            current_run=snapshot,
        )
    if body.expected_attempt is not None and body.expected_attempt != int(snapshot["attempt"]):
        _raise(
            409,
            "revision_conflict",
            "The Run attempt changed since the client last read it.",
            current_run=snapshot,
        )

    current_session = _session_id(request)
    task: asyncio.Task | None = None
    merged_settings = None
    if action in {"pause", "stop"}:
        if scope["control_session_id"] != current_session or get_active_lifecycle(run_id) is None:
            _raise(409, "run_not_live", "This Run has no live controller in the current desktop session.")
        if action not in snapshot["available_actions"]:
            _raise(409, "action_unavailable", f"Run cannot {action} from its current state.", current_run=snapshot)
        if action == "stop":
            task = _live_task(run_id, current_session)
            if task is None:
                _raise(409, "run_not_live", "This Run has no live task in the current desktop session.")
    elif action == "resume":
        if "resume" not in snapshot["available_actions"]:
            _raise(409, "action_unavailable", "Run cannot resume from its current state.", current_run=snapshot)
        if _live_task(run_id, current_session) is not None:
            _raise(409, "run_not_live", "This Run already has a live controller.")
        try:
            from app.agent.settings_store import build_runtime_namespace, load_agent_settings
            from app.config import settings

            merged_settings = build_runtime_namespace(settings, load_agent_settings(settings))
        except Exception as exc:
            logger.exception("Unable to prepare Run continuation run_id=%s", run_id)
            _raise(503, "resume_unavailable", "Runtime settings are unavailable for safe continuation.")
    else:
        _raise(404, "not_found", "Run action not found.")

    try:
        from app.agent.durable.recovery import get_owner_instance_id

        claim_owner = get_owner_instance_id()
    except Exception:
        _raise(503, "service_unavailable", "Run ownership is unavailable.")
    reservation = _reserve_idempotency(
        profile_id=profile_id,
        action=action,
        run_id=run_id,
        key_hash=key_hash,
        payload_hash=digest,
        expected_revision=body.expected_revision,
        expected_attempt=body.expected_attempt,
        claim_owner=claim_owner,
    )
    if not reservation.get("reserved"):
        return await _resolve_existing_idempotency(
            record=reservation,
            payload_hash=digest,
            run_id=run_id,
            request=request,
            principal_hash=principal_hash,
            action=action,
            key_hash=key_hash,
        )

    try:
        if action == "pause":
            if not await pause_run(run_id):
                _raise(409, "run_not_live", "This Run has no live controller in the current desktop session.")
        elif action == "stop":
            assert task is not None
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                # The worker publishes its durable outcome; a task failure
                # does not justify inventing a different Run state here.
                logger.exception("Run stop worker failed run_id=%s", run_id)
        else:
            durable_run, _checkpoint = resume_run(
                run_id,
                execution_source=scope["execution_source"],
                principal_id=profile_id,
                control_session_id=current_session,
                workspace_dir=scope["workspace_dir"],
            )
            _start_resume_worker(
                durable_run,
                {
                    "scope": scope,
                    "session_id": current_session,
                    "profile_id": profile_id,
                    "settings": merged_settings,
                },
            )

        latest_scope, latest = _authorized_scope_and_snapshot(run_id, request)
        response = {
            "accepted": True,
            "run": _public_snapshot(latest, latest_scope, request),
        }
        _complete_idempotency(
            principal_hash=principal_hash,
            action=action,
            run_id=run_id,
            key_hash=key_hash,
            payload_hash=digest,
            response=response,
        )
        return response
    except Exception:
        _mark_idempotency_uncertain(
            principal_hash=principal_hash,
            action=action,
            run_id=run_id,
            key_hash=key_hash,
            payload_hash=digest,
        )
        raise


@router.post("/{run_id}/pause", status_code=202)
async def pause_run_route(
    run_id: str,
    body: RunCommandIn,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return await _run_command("pause", run_id, body, request, idempotency_key)


@router.post("/{run_id}/resume", status_code=202)
async def resume_run_route(
    run_id: str,
    body: RunCommandIn,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return await _run_command("resume", run_id, body, request, idempotency_key)


@router.post("/{run_id}/stop", status_code=202)
async def stop_run_route(
    run_id: str,
    body: RunCommandIn,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return await _run_command("stop", run_id, body, request, idempotency_key)

