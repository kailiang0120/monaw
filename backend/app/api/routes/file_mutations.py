"""Session-scoped HTTP access to P3 native file mutation previews.

The first API slice deliberately exposes only ordinary single-file writes.
It reuses file_ops' planner, permission checks, native OperationExecution,
snapshot, and verifier path. Preview records are short lived and remain in
process memory; request contents and private plan digests are never persisted
to the public journal or returned to clients.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.database import get_db
from app.agent.durable.journal import Journal
from app.agent.file_mutation.planner import MutationPlan
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

router = APIRouter(prefix="/file-mutations")

PREVIEW_TTL_SECONDS = 15 * 60
UNDO_CONFIRMATION_TTL_SECONDS = 5 * 60
PREVIEW_LIMIT = 512
UNDO_CONFIRMATION_LIMIT = 512
_PREVIEWS: dict[str, "MutationPreviewRecord"] = {}
_IDEMPOTENCY: dict[tuple[str, str, str, str], tuple[str, dict[str, Any]]] = {}
_UNDO_CONFIRMATIONS: dict[str, "UndoConfirmationRecord"] = {}
_PREVIEW_LOCKS: dict[tuple[str, str, str, str], threading.Lock] = {}
_PREVIEW_LOCK_USERS: dict[tuple[str, str, str, str], int] = {}
_RECORD_LOCK = threading.RLock()


class FileMutationPreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, max_length=128)
    conversation_id: str = Field(..., min_length=1, max_length=128)
    expected_revision: int = Field(..., ge=1)
    operation: Literal["write"] = "write"
    path: str = Field(..., min_length=1, max_length=4096)
    content: str = Field(..., max_length=16 * 1024 * 1024)
    overwrite: bool = False
    create_parents: bool = False
    encoding: str = Field("utf-8", min_length=1, max_length=40)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("path is invalid")
        return value


class FileMutationApplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(..., ge=1)


class FileMutationUndoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, max_length=128)
    conversation_id: str = Field(..., min_length=1, max_length=128)
    expected_revision: int = Field(..., ge=1)
    confirmation_id: str = Field(..., min_length=32, max_length=128)


class FileMutationUndoPreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, max_length=128)
    conversation_id: str = Field(..., min_length=1, max_length=128)
    expected_revision: int = Field(..., ge=1)


@dataclass
class MutationPreviewRecord:
    mutation_id: str
    session_id: str
    principal_id: str
    run_id: str
    conversation_id: str
    workspace_dir: str
    run_revision: int
    operation: str
    path: str
    content: str
    overwrite: bool
    create_parents: bool
    encoding: str
    plan: MutationPlan
    permission_signature: str
    apply_available: bool
    reason_code: str
    response: dict[str, Any]
    expires_at: float
    status: Literal["previewed", "applying", "applied", "failed"] = "previewed"
    applied_operation_id: str = ""
    apply_key: str = ""
    apply_payload_digest: str = ""
    apply_response: dict[str, Any] | None = None


@dataclass
class UndoConfirmationRecord:
    confirmation_id: str
    session_id: str
    principal_id: str
    run_id: str
    conversation_id: str
    workspace_key: str
    operation_id: str
    revision: int
    fingerprint_digest: str
    preview: dict[str, Any]
    expires_at: float
    state: Literal["prepared", "used"] = "prepared"
    idempotency_key: str = ""
    request_digest: str = ""


def _raise(status_code: int, code: str, message: str, **details: Any) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": details},
    )


def _workspace_key(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        from app.agent.runtime_paths import WORKSPACE_DIR

        raw = str(WORKSPACE_DIR)
    try:
        path = str(Path(raw).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        import os

        path = os.path.abspath(os.path.normpath(raw))
    import os

    return os.path.normcase(os.path.normpath(path))


def _active_lifecycle_for_run(run_id: str):
    from app.agent.durable.turn_run import get_active_lifecycle

    return get_active_lifecycle(run_id)


def _authorized_run_context(
    request: Request,
    *,
    run_id: str,
    conversation_id: str,
    expected_revision: int | None,
    require_active: bool = True,
) -> dict[str, Any]:
    """Authorize the current session or signed desktop profile before reading files/cache."""
    db = get_db()
    journal = Journal(db)
    control_session = request.state.control_session
    session_id = str(control_session.session_id)
    profile_id = str(getattr(control_session, "profile_id", None) or "")
    scope = journal.load_run_internal(run_id)
    if scope is None:
        _raise(404, "run_not_found", "Run not found")
    stored_session_id = str(scope.get("control_session_id") or "")
    owner_principal_id = str(scope.get("owner_principal_id") or "")
    profile_match = bool(profile_id) and owner_principal_id == profile_id
    legacy_session_match = (
        stored_session_id == session_id
        and owner_principal_id == session_id
    )
    if not (profile_match or legacy_session_match):
        _raise(403, "run_scope_mismatch", "Run is not owned by this control session")
    if (
        str(scope.get("conversation_id") or "") != conversation_id
        or str(scope.get("execution_source") or "") != "desktop"
    ):
        _raise(404, "run_not_found", "Run not found")
    conversation = db.get_conversation(conversation_id)
    if conversation is None:
        _raise(404, "conversation_not_found", "Conversation not found")
    workspace_dir = str(conversation.get("workspace_dir") or "")
    if _workspace_key(workspace_dir) != _workspace_key(scope.get("workspace_dir")):
        _raise(409, "workspace_changed", "The conversation workspace changed after this Run started")
    snapshot = journal.snapshot(run_id)
    if snapshot is None:
        _raise(404, "run_not_found", "Run not found")
    if expected_revision is not None and int(snapshot["revision"]) != expected_revision:
        _raise(409, "stale_preview", "The Run changed after this file preview was prepared")

    lifecycle = None
    durable_run = None
    if require_active:
        # The stable profile can inspect and undo its persisted operations after
        # restart, but active Apply requires the exact current live session.
        if stored_session_id != session_id:
            _raise(403, "run_scope_mismatch", "Active file changes require the Run's current control session")
        lifecycle = _active_lifecycle_for_run(run_id)
        durable_run = getattr(lifecycle, "durable_run", None) if lifecycle is not None else None
        if (
            lifecycle is None
            or durable_run is None
            or str(getattr(durable_run, "run_id", "")) != run_id
            or str(getattr(durable_run, "owner_instance_id", "")) != str(scope.get("owner_instance_id") or "")
            or int(getattr(durable_run, "attempt", 0)) != int(scope.get("attempt") or 0)
            or str(snapshot["status"]) != "running"
        ):
            _raise(409, "run_not_active", "A live desktop Run is required for this file action")

    return {
        "db": db,
        "journal": journal,
        "scope": scope,
        "snapshot": snapshot,
        "conversation": conversation,
        "session_id": session_id,
        "principal_id": profile_id if profile_match else session_id,
        "workspace_dir": workspace_dir,
        "lifecycle": lifecycle,
        "durable_run": durable_run,
    }


@contextmanager
def _runtime_context(scope: dict[str, Any]):
    from app.agent.conversation_workspace import reset_workspace_dir, set_workspace_dir

    workspace_token = set_workspace_dir(str(scope["workspace_dir"] or ""))
    conversation_token = set_current_conversation_id(str(scope["scope"]["conversation_id"]))
    session_token = set_current_control_session_id(str(scope["session_id"]))
    principal_token = set_current_principal_id(str(scope["principal_id"]))
    execution_token = set_current_execution_source("desktop")
    profile_token = set_current_permission_profile_id("desktop-current")
    interactive_token = set_current_interactive(True)
    try:
        yield
    finally:
        reset_current_interactive(interactive_token)
        reset_current_permission_profile_id(profile_token)
        reset_current_execution_source(execution_token)
        reset_current_principal_id(principal_token)
        reset_current_control_session_id(session_token)
        reset_current_conversation_id(conversation_token)
        reset_workspace_dir(workspace_token)


def _stable_permissions(path: str) -> tuple[bool, str, str]:
    """Return apply eligibility plus a digest of current stable READ/WRITE policy."""
    from app.agent.controller_policy import ActionType, resolve_permission

    snapshot: list[dict[str, Any]] = []
    apply_available = True
    reason_code = ""
    for action in (ActionType.MUTATE, ActionType.READ):
        decision = resolve_permission(
            action,
            target_path=path,
            allow_session_grants=False,
            consume_once_grants=False,
        )
        snapshot.append({
            "action": action.value,
            "allowed": bool(decision.allowed),
            "blocked": bool(decision.blocked),
            "requires_confirmation": bool(decision.requires_confirmation),
            "requires_access_grant": bool(decision.requires_access_grant),
            "reason_code": str(decision.reason_code or ""),
            "policy_source": str(decision.policy_source or ""),
        })
        if action == ActionType.MUTATE and (
            decision.blocked or decision.requires_access_grant or not decision.allowed
        ):
            _raise(403, decision.reason_code or "permission_denied", "Current file permissions do not allow this action")
        if action == ActionType.READ and (
            decision.blocked
            or decision.requires_access_grant
            or not decision.allowed
            or decision.requires_confirmation
        ):
            apply_available = False
            reason_code = "read_permission_required"
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return apply_available, reason_code, hashlib.sha256(payload).hexdigest()


def _plan_write(scope: dict[str, Any], body: FileMutationPreviewIn) -> tuple[MutationPlan, bool, str, str]:
    """Build a P3 plan without creating an approval ticket or exposing private evidence."""
    from app.agent.controller_policy import ActionType
    from app.skills.filesystem import file_ops

    with _runtime_context(scope):
        shape_error = file_ops._validate_path_shape(body.path, operation="write")
        if shape_error:
            _raise(422, "invalid_path", "The file path is not supported")
        budget_error = file_ops._validate_write_budget(body.content, body.encoding)
        if budget_error:
            _raise(413, "write_size_limit_exceeded", "File content exceeds the supported write size")
        try:
            after_bytes = body.content.encode(body.encoding or "utf-8")
        except (LookupError, UnicodeError):
            _raise(422, "invalid_encoding", "The requested text encoding is not supported")
        resolved = file_ops._resolve_path(body.path)
        stable_apply, permission_reason, permission_signature = _stable_permissions(str(resolved))
        access = file_ops._p3_initial_mutation_access(
            execution=None,
            action_targets=[(ActionType.MUTATE, str(resolved))],
            tool_name="fs_write",
            action_description=f"Write file: {resolved}",
            payload_args={
                "path": body.path,
                "content": body.content,
                "overwrite": body.overwrite,
                "create_parents": body.create_parents,
                "encoding": body.encoding,
                "dry_run": True,
            },
        )
        if access:
            try:
                pending = json.loads(access)
            except (TypeError, json.JSONDecodeError):
                pending = {}
            if pending.get("status") == "pending_access_grant":
                _raise(403, "access_grant_required", "A path access grant is required before previewing this file", ticket_id=str(pending.get("ticket_id") or ""))
            _raise(403, str(pending.get("reason_code") or "permission_denied"), "Current file permissions do not allow this action")
        if resolved.parent.exists() and not resolved.parent.is_dir():
            _raise(409, "parent_directory_unavailable", "The target parent is not a directory")
        if not resolved.parent.is_dir():
            _raise(409, "parent_directory_unavailable", "A verifiable native write requires an existing parent directory")
        if resolved.exists() and resolved.is_dir():
            _raise(409, "unsupported_target", "Only ordinary files can be changed by this API")
        if resolved.exists() and not body.overwrite:
            _raise(409, "file_exists", "The target already exists; set overwrite to preview replacement")
        revalidate = file_ops._revalidate_mutation_target(body.path)
        if revalidate:
            try:
                error_payload = json.loads(revalidate)
            except (TypeError, json.JSONDecodeError):
                error_payload = {}
            _raise(403, str(error_payload.get("reason_code") or "permission_denied"), "The target path is not permitted")
        read_authorized, read_result = file_ops._p3_read_access(
            [resolved],
            operation="write",
            tool_name="fs_write",
            action_description=f"Write file: {resolved}",
            payload_args={
                "path": body.path,
                "content": body.content,
                "overwrite": body.overwrite,
                "create_parents": body.create_parents,
                "encoding": body.encoding,
                "dry_run": True,
            },
            dry_run=True,
            execution=None,
        )
        if read_result:
            _raise(403, "read_permission_required", "READ permission is required before this file change")
        request_payload = {
            "path": str(resolved),
            "content": body.content,
            "overwrite": body.overwrite,
            "create_parents": body.create_parents,
            "encoding": body.encoding or "utf-8",
            # Bind to the non-dry-run request which Apply will execute.
            "dry_run": False,
        }
        try:
            plan, _before_bytes, _fingerprint = file_ops._p3_plan_file_write(
                path=resolved,
                operation="write",
                request=request_payload,
                after_bytes=after_bytes,
                encoding=body.encoding or "utf-8",
                read_authorized=read_authorized,
            )
        except Exception:
            _raise(409, "preview_unavailable", "A safe file preview could not be built")
        if not read_authorized:
            stable_apply = False
            permission_reason = permission_reason or "read_permission_required"
        return plan, stable_apply, permission_reason, permission_signature


def _purge_expired_records(now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    for key, record in list(_PREVIEWS.items()):
        if record.expires_at <= current:
            _PREVIEWS.pop(key, None)
    if len(_PREVIEWS) > PREVIEW_LIMIT:
        oldest = sorted(_PREVIEWS.values(), key=lambda item: item.expires_at)
        for record in oldest[: len(_PREVIEWS) - PREVIEW_LIMIT]:
            _PREVIEWS.pop(record.mutation_id, None)
    # Idempotency entries are bounded by the preview lifetime too.
    live_ids = set(_PREVIEWS)
    for key, (_digest, response) in list(_IDEMPOTENCY.items()):
        mutation_id = str(response.get("mutation_id") or "")
        if mutation_id and mutation_id not in live_ids:
            _IDEMPOTENCY.pop(key, None)
    for key, lock in list(_PREVIEW_LOCKS.items()):
        if key not in _IDEMPOTENCY and not _PREVIEW_LOCK_USERS.get(key) and not lock.locked():
            _PREVIEW_LOCKS.pop(key, None)
    for confirmation_id, record in list(_UNDO_CONFIRMATIONS.items()):
        if record.expires_at <= current:
            _UNDO_CONFIRMATIONS.pop(confirmation_id, None)


def _request_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _replay_idempotent_response(value: dict[str, Any]) -> dict[str, Any]:
    cached_error = value.get("_http_error")
    if isinstance(cached_error, dict):
        detail = cached_error.get("detail")
        status_code = int(cached_error.get("status_code") or 409)
        if isinstance(detail, dict):
            _raise(
                status_code,
                str(detail.get("code") or "request_failed"),
                str(detail.get("message") or "The request could not be completed"),
                **(detail.get("details") if isinstance(detail.get("details"), dict) else {}),
            )
        _raise(status_code, "request_failed", "The request could not be completed")
    return dict(value)


def _check_idempotency_key(value: str) -> str:
    key = str(value or "").strip()
    if not key or len(key) > 128 or any(ord(char) < 0x21 or ord(char) > 0x7E for char in key):
        _raise(422, "invalid_idempotency_key", "Idempotency-Key must be 1 to 128 visible ASCII characters")
    return key


def _preview_response(record: MutationPreviewRecord) -> dict[str, Any]:
    return {
        "mutation_id": record.mutation_id,
        "run_id": record.run_id,
        "conversation_id": record.conversation_id,
        "revision": record.run_revision,
        "operation": record.operation,
        "files": record.plan.to_preview_dict()["files"],
        "preview": record.plan.to_preview_dict()["preview"],
        "undo_capability": record.plan.undo_capability,
        "apply_available": record.apply_available,
        "reason_code": record.reason_code or None,
        "expires_in_seconds": PREVIEW_TTL_SECONDS,
    }


def _preview_key(scope: dict[str, Any], path: str, idempotency_key: str) -> tuple[str, str, str, str]:
    return (str(scope["session_id"]), "preview", path, idempotency_key)


@router.post("/preview")
async def preview_file_mutation(
    body: FileMutationPreviewIn,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    key = _check_idempotency_key(idempotency_key)
    # Auth/session/conversation/workspace/run/revision checks deliberately run
    # before consulting the idempotency cache or touching a target path.
    scope = _authorized_run_context(
        request,
        run_id=body.run_id,
        conversation_id=body.conversation_id,
        expected_revision=body.expected_revision,
    )
    with _runtime_context(scope):
        from app.skills.filesystem import file_ops

        canonical_target = str(file_ops._resolve_path(body.path))
    idem_key = _preview_key(scope, canonical_target, key)
    digest = _request_digest(body.model_dump(mode="json"))
    # Re-evaluate effective READ and WRITE permissions before a cache hit can
    # return an older Diff. The helper hashes only permission decisions.
    with _runtime_context(scope):
        _apply_available_now, _reason_now, permission_signature_now = _stable_permissions(canonical_target)

    with _RECORD_LOCK:
        _purge_expired_records()
        key_lock = _PREVIEW_LOCKS.setdefault(idem_key, threading.Lock())
        _PREVIEW_LOCK_USERS[idem_key] = _PREVIEW_LOCK_USERS.get(idem_key, 0) + 1
    await asyncio.to_thread(key_lock.acquire)
    try:
        with _RECORD_LOCK:
            _purge_expired_records()
            previous = _IDEMPOTENCY.get(idem_key)
            if previous is not None:
                if not hmac.compare_digest(previous[0], digest):
                    _raise(409, "idempotency_conflict", "Idempotency-Key was reused with a different preview request")
                cached_record = _PREVIEWS.get(str(previous[1].get("mutation_id") or ""))
                if not _apply_available_now and _reason_now == "read_permission_required":
                    _raise(403, "read_permission_required", "Current READ permissions do not allow access to the cached preview")
                if (
                    cached_record is None
                    or not hmac.compare_digest(cached_record.permission_signature, permission_signature_now)
                ):
                    _raise(409, "stale_preview", "File permissions changed after the cached preview was prepared")
                return _replay_idempotent_response(previous[1])

        try:
            plan, apply_available, reason_code, permission_signature = await asyncio.to_thread(
                _plan_write, scope, body
            )
        except HTTPException:
            raise
        except Exception:
            _raise(422, "preview_unavailable", "A safe file preview could not be built")

        record = MutationPreviewRecord(
            mutation_id=f"mut-{uuid.uuid4().hex}",
            session_id=str(scope["session_id"]),
            principal_id=str(scope["principal_id"]),
            run_id=body.run_id,
            conversation_id=body.conversation_id,
            workspace_dir=str(scope["workspace_dir"] or ""),
            run_revision=int(scope["snapshot"]["revision"]),
            operation="write",
            path=canonical_target,
            content=body.content,
            overwrite=bool(body.overwrite),
            create_parents=bool(body.create_parents),
            encoding=body.encoding or "utf-8",
            plan=plan,
            permission_signature=permission_signature,
            apply_available=apply_available,
            reason_code=reason_code,
            response={},
            expires_at=time.monotonic() + PREVIEW_TTL_SECONDS,
        )
        response = _preview_response(record)
        record.response = response
        with _RECORD_LOCK:
            _purge_expired_records()
            _PREVIEWS[record.mutation_id] = record
            _IDEMPOTENCY[idem_key] = (digest, response)
        return response
    finally:
        key_lock.release()
        with _RECORD_LOCK:
            users = _PREVIEW_LOCK_USERS.get(idem_key, 1) - 1
            if users <= 0:
                _PREVIEW_LOCK_USERS.pop(idem_key, None)
                if idem_key not in _IDEMPOTENCY and not key_lock.locked():
                    _PREVIEW_LOCKS.pop(idem_key, None)
            else:
                _PREVIEW_LOCK_USERS[idem_key] = users


def _fingerprint_matches(record: MutationPreviewRecord) -> bool:
    from app.agent.file_mutation.fingerprint import fingerprint_path
    from app.agent.file_mutation.planner import PathState

    try:
        current = PathState.from_fingerprint(fingerprint_path(record.path, include_bytes=True))
    except Exception:
        return False
    return current == record.plan.operands[0].before


def _stable_permission_matches(scope: dict[str, Any], record: MutationPreviewRecord) -> bool:
    with _runtime_context(scope):
        _apply_available, _reason, signature = _stable_permissions(record.path)
    if not _apply_available:
        return False
    return hmac.compare_digest(signature, record.permission_signature)


def _apply_p3_write(scope: dict[str, Any], record: MutationPreviewRecord, operation_id: str) -> dict[str, Any]:
    """Run one API Apply through the normal trusted P3 native mutation path."""
    from app.agent.approval_broker import approve_ticket, get_ticket, ticket_validation_error
    from app.agent.controller_policy import ActionType, resolve_permission
    from app.agent.durable.operation_execution import (
        OperationExecution,
        reset_current_operation_execution,
        set_current_operation_execution,
    )
    from app.agent.durable.operation_policy import classify_tool
    from app.agent.harness.workers import resource_lock
    from app.skills.filesystem import file_ops

    durable_run = scope["durable_run"]
    lifecycle = scope["lifecycle"]
    policy = classify_tool("fs_write", trusted_builtin=True)
    operation = OperationExecution(
        durable_run=durable_run,
        lifecycle=lifecycle,
        operation_id=operation_id,
        tool_name="fs_write",
        trusted_builtin=True,
        journal_tool_name=policy.journal_tool_name,
        operation_kind=policy.operation_kind,
        effect_class=policy.effect_class,
    )
    try:
        operation.prepare()
    except Exception:
        _raise(409, "operation_conflict", "The durable operation could not be claimed")

    lock = resource_lock("filesystem")
    with lock:
        try:
            operation.begin_effect()
        except Exception:
            operation.settle(None, cancelled=True)
            _raise(409, "run_not_active", "The live Run no longer owns this operation")
        operation_token = set_current_operation_execution(operation)
        try:
            with _runtime_context(scope):
                path_decision = resolve_permission(
                    ActionType.MUTATE,
                    target_path=record.path,
                    allow_session_grants=False,
                    consume_once_grants=False,
                )
                read_decision = resolve_permission(
                    ActionType.READ,
                    target_path=record.path,
                    allow_session_grants=False,
                    consume_once_grants=False,
                )
                if (
                    path_decision.blocked
                    or path_decision.requires_access_grant
                    or not path_decision.allowed
                    or read_decision.blocked
                    or read_decision.requires_access_grant
                    or not read_decision.allowed
                    or read_decision.requires_confirmation
                ):
                    operation.native_file_no_effect(path_decision.reason_code or read_decision.reason_code or "permission_denied")
                    _raise(403, "permission_denied", "Current stable file permissions do not allow this change")
            if not _fingerprint_matches(record):
                operation.native_file_no_effect("stale_preview")
                _raise(409, "stale_preview", "The file changed after this preview; no file change was made")
            with _runtime_context(scope):
                output = file_ops.file_write(
                    record.path,
                    record.content,
                    overwrite=record.overwrite,
                    create_parents=record.create_parents,
                    encoding=record.encoding,
                    dry_run=False,
                    _expected_binding_digest=record.plan.binding_digest,
                )
                try:
                    payload_out = json.loads(output)
                except (TypeError, json.JSONDecodeError):
                    payload_out = {"status": "error", "reason_code": "invalid_result"}

                status = str(payload_out.get("status") or "error")
                if status == "pending_approval":
                    if payload_out.get("preview") != record.plan.to_preview_dict():
                        operation.note_pending_preflight(output)
                        operation.preflight_rejected("operation_failed")
                        _raise(409, "stale_preview", "The file preview changed; no file change was made")
                    ticket_id = str(payload_out.get("ticket_id") or "")
                    ticket = get_ticket(ticket_id) if ticket_id else None
                    if (
                        ticket is None
                        or ticket_validation_error(
                            ticket,
                            expected_session_id=str(scope["session_id"]),
                            expected_conversation_id=str(scope["scope"]["conversation_id"]),
                            expected_execution_source="desktop",
                        )
                    ):
                        operation.note_pending_preflight(output)
                        operation.preflight_rejected("approval_rejected")
                        _raise(409, "approval_unavailable", "The file approval could not be validated")
                    operation.note_pending_preflight(output)
                    if approve_ticket(ticket_id, resolved_by="manual_file_mutation_apply") is None:
                        operation.preflight_rejected("approval_rejected")
                        _raise(409, "approval_unavailable", "The file approval could not be claimed")
                    operation.begin_effect()
                    output = file_ops.file_write(
                        record.path,
                        record.content,
                        overwrite=record.overwrite,
                        create_parents=record.create_parents,
                        encoding=record.encoding,
                        dry_run=False,
                        _bypass_gate=True,
                        _expected_binding_digest=record.plan.binding_digest,
                    )
                    try:
                        payload_out = json.loads(output)
                    except (TypeError, json.JSONDecodeError):
                        payload_out = {"status": "error", "reason_code": "invalid_result"}
                    status = str(payload_out.get("status") or "error")
                elif status == "pending_access_grant":
                    operation.note_pending_preflight(output)
                    operation.preflight_rejected("operation_failed")
                    _raise(403, "access_grant_required", "A path access grant is required before this file change")

                operation.settle(output)
        except HTTPException:
            raise
        except Exception:
            try:
                operation.settle('{"status":"error","reason_code":"operation_failed"}')
            except Exception:
                pass
            _raise(409, "file_mutation_failed", "The file change could not be completed safely")
        finally:
            reset_current_operation_execution(operation_token)

    if status in {"error", "blocked", "denied", "failed"}:
        reason = str(payload_out.get("reason_code") or "operation_failed")
        status_code = 409 if reason in {"stale_preview", "stale_precondition"} else 403 if status in {"blocked", "denied"} else 409
        code = "stale_preview" if reason in {"stale_preview", "stale_precondition"} else reason
        _raise(status_code, code, "The file change was not completed")
    return {
        "status": "applied",
        "mutation_id": record.mutation_id,
        "run_id": record.run_id,
        "conversation_id": record.conversation_id,
        "operation_id": operation.operation_id,
        "revision": int(scope["journal"].snapshot(record.run_id)["revision"]),
        "verification_result": str(payload_out.get("verification_result") or "unknown"),
        "preview": record.plan.to_preview_dict(),
        "undo_capability": record.plan.undo_capability,
    }


@router.post("/{mutation_id}/apply")
async def apply_file_mutation(
    mutation_id: str,
    body: FileMutationApplyIn,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    key = _check_idempotency_key(idempotency_key)
    # Run/session/workspace authorization must precede preview and idempotency reads.
    # The ID itself is opaque. First bind it to the authenticated control
    # session, then load and authorize its saved run context.
    with _RECORD_LOCK:
        _purge_expired_records()
        record = _PREVIEWS.get(mutation_id)
        if record is None or record.session_id != str(request.state.control_session.session_id):
            _raise(404, "mutation_not_found", "File mutation preview was not found or has expired")
    scope = _authorized_run_context(
        request,
        run_id=record.run_id,
        conversation_id=record.conversation_id,
        expected_revision=None,
        require_active=False,
    )
    # A retry can arrive after Apply advanced the Run revision. Re-check
    # current permissions and return an exact cached result before enforcing
    # the original expected revision or requiring the live lifecycle.
    with _runtime_context(scope):
        _apply_allowed_now, _permission_reason_now, permission_signature_now = _stable_permissions(record.path)
    # Now that run and owner scope are verified, inspect retry state.
    payload_digest = _request_digest({"mutation_id": mutation_id, "expected_revision": body.expected_revision})
    idem_scope = (str(scope["session_id"]), "apply", mutation_id, key)
    with _RECORD_LOCK:
        record = _PREVIEWS.get(mutation_id)
        if record is None or record.session_id != str(scope["session_id"]):
            _raise(404, "mutation_not_found", "File mutation preview was not found or has expired")
        previous = _IDEMPOTENCY.get(idem_scope)
        if previous is not None:
            if not hmac.compare_digest(previous[0], payload_digest):
                _raise(409, "idempotency_conflict", "Idempotency-Key was reused with a different Apply request")
            if not hmac.compare_digest(record.permission_signature, permission_signature_now):
                _raise(403, "permission_denied", "Current file permissions do not allow access to the previous Apply result")
            return _replay_idempotent_response(previous[1])
        if record.apply_response is not None and record.apply_key == key:
            if not hmac.compare_digest(record.apply_payload_digest, payload_digest):
                _raise(409, "idempotency_conflict", "Idempotency-Key was reused with a different Apply request")
            if not hmac.compare_digest(record.permission_signature, permission_signature_now):
                _raise(403, "permission_denied", "Current file permissions do not allow access to the previous Apply result")
            return dict(record.apply_response)
        if body.expected_revision != record.run_revision:
            _raise(409, "stale_preview", "The Apply revision does not match the preview revision")
        if record.status == "applied":
            _raise(409, "mutation_consumed", "This preview has already been applied")
        if record.status == "applying":
            _raise(409, "mutation_in_progress", "This file change is already being applied")
        if record.status == "failed":
            _raise(409, "external_effect_unknown", "This operation needs review before another Apply")
        if not record.apply_available:
            _raise(403, record.reason_code or "read_permission_required", "The preview lacks the stable READ permission required for safe Apply")
        scope = _authorized_run_context(
            request,
            run_id=record.run_id,
            conversation_id=record.conversation_id,
            expected_revision=body.expected_revision,
            require_active=True,
        )
        record.status = "applying"

    current_revision = int(scope["journal"].snapshot(record.run_id)["revision"])
    if current_revision != record.run_revision:
        with _RECORD_LOCK:
            record.status = "previewed"
        _raise(409, "stale_preview", "The Run changed after this file preview was prepared")
    if not _stable_permission_matches(scope, record):
        with _RECORD_LOCK:
            record.status = "previewed"
        _raise(409, "stale_preview", "File permissions changed after this preview was prepared")

    operation_id = "op-api-" + hashlib.sha256(
        f"{scope['session_id']}\0{record.run_id}\0{mutation_id}\0{key}".encode("utf-8")
    ).hexdigest()[:32]
    try:
        result = await asyncio.to_thread(_apply_p3_write, scope, record, operation_id)
    except HTTPException as exc:
        with _RECORD_LOCK:
            if record.status == "applying":
                row = scope["journal"].load_operation(record.run_id, operation_id)
                if row is not None and row.get("status") in {"executing", "uncertain", "needs_review"}:
                    record.status = "failed"
                else:
                    record.status = "previewed"
            _IDEMPOTENCY[idem_scope] = (
                payload_digest,
                {
                    "mutation_id": mutation_id,
                    "_http_error": {"status_code": exc.status_code, "detail": exc.detail},
                },
            )
        raise
    except Exception:
        with _RECORD_LOCK:
            if record.status == "applying":
                record.status = "failed"
            _IDEMPOTENCY[idem_scope] = (
                payload_digest,
                {
                    "mutation_id": mutation_id,
                    "_http_error": {
                        "status_code": 409,
                        "detail": {
                            "code": "file_mutation_failed",
                            "message": "The file change could not be completed safely",
                            "details": {},
                        },
                    },
                },
            )
        _raise(409, "file_mutation_failed", "The file change could not be completed safely")

    with _RECORD_LOCK:
        record.status = "applied"
        record.applied_operation_id = str(result["operation_id"])
        record.apply_key = key
        record.apply_payload_digest = payload_digest
        record.apply_response = dict(result)
        _IDEMPOTENCY[idem_scope] = (payload_digest, dict(result))
    return result


@router.get("/{operation_id}/undo-capability")
async def undo_capability(
    operation_id: str,
    request: Request,
    run_id: str = Query(..., min_length=1, max_length=128),
    conversation_id: str = Query(..., min_length=1, max_length=128),
):
    """Check current Undo eligibility without claiming or changing files.

    Eligibility is advisory: Undo independently repeats every check at its
    effect boundary. Do not expose paths, contents, or private snapshot data.
    """
    scope = _authorized_run_context(
        request, run_id=run_id, conversation_id=conversation_id,
        expected_revision=None, require_active=False,
    )

    def check() -> dict[str, Any]:
        import os
        from app.agent.file_mutation.fingerprint import FingerprintError
        from app.agent.file_mutation.snapshot_store import SnapshotStoreError
        from app.agent.file_mutation.undo_manager import UndoManager, UndoPermissionError, UndoUnavailableError
        from app.skills.filesystem import file_ops

        journal = scope["journal"]
        operation = journal.load_operation_internal(run_id, operation_id)
        if operation is None:
            _raise(404, "operation_not_found", "File operation was not found in this Run")
        snapshot = journal.snapshot(run_id)
        if snapshot is None:
            _raise(404, "run_not_found", "Run not found")

        def result(status: str, reason: str | None = None) -> dict[str, Any]:
            return {"run_id": run_id, "operation_id": operation_id,
                    "status": status, "reason_code": reason,
                    "revision": int(snapshot["revision"])}

        if (operation.get("status") != "verified"
            or operation.get("effect_class") != "verifiable_write"
            or operation.get("operation_kind") not in {"file_write", "file_move", "file_delete"}
            or not operation.get("snapshot_ref")):
            return result("unavailable", "undo_unavailable")
        manager = UndoManager(journal, store=file_ops._p3_snapshot_store())
        try:
            with _runtime_context(scope):
                original = manager.store.load(
                    str(operation["snapshot_ref"]), run_id=run_id, operation_id=operation_id,
                )
                operands = original.get("operands")
                if not isinstance(operands, list) or not operands or any(not isinstance(item, dict) for item in operands):
                    return result("unavailable", "undo_unavailable")
                manager._authorize_source_inverse(
                    str(operation["operation_kind"]), operands, confirmation_granted=True
                )
                claim = scope["db"].fetchone(
                    """SELECT status FROM run_operations WHERE run_id = ?
                       AND related_operation_id = ? AND operation_kind = 'undo'
                       AND status NOT IN ('failed', 'cancelled')
                       ORDER BY sequence_no DESC LIMIT 1""", (run_id, operation_id),
                )
                if claim is not None:
                    if claim["status"] == "verified":
                        return result("undone")
                    return result("needs_review", "undo_in_progress")
                if not manager._snapshot_is_live(original):
                    return result("expired", "undo_expired")
                if snapshot["status"] not in {"completed", "failed", "cancelled"}:
                    return result("unavailable", "run_not_terminal")
                if (os.name != "nt" and operation["operation_kind"] == "file_write"
                    and len(operands) == 1 and operands[0].get("before", {}).get("exists") is not True):
                    return result("unavailable", "undo_unavailable")
                observed = manager._fingerprint_original_after(operands)
                if not manager._matches_original_after(original, observed):
                    return result("conflict", "undo_conflict")
                return result("available")
        except UndoPermissionError:
            return result("unavailable", "permission_denied")
        except (SnapshotStoreError, UndoUnavailableError):
            return result("unavailable", "undo_unavailable")
        except (FingerprintError, OSError, ValueError, TypeError):
            return result("conflict", "undo_conflict")

    return await asyncio.to_thread(check)


def _resolve_undo_operation_id(identifier: str, scope: dict[str, Any]) -> str:
    with _RECORD_LOCK:
        _purge_expired_records()
        record = _PREVIEWS.get(identifier)
        if record is not None and record.session_id == str(scope["session_id"]):
            if record.run_id != str(scope["scope"]["run_id"]):
                _raise(404, "mutation_not_found", "File mutation was not found or has expired")
            if record.conversation_id != str(scope["scope"]["conversation_id"]):
                _raise(404, "mutation_not_found", "File mutation was not found or has expired")
            if record.status != "applied" or not record.applied_operation_id:
                _raise(409, "undo_unavailable", "This file mutation has not been applied")
            return record.applied_operation_id
    return identifier


@router.post("/{operation_or_mutation_id}/undo-preview")
async def preview_file_mutation_undo(
    operation_or_mutation_id: str,
    body: FileMutationUndoPreviewIn,
    request: Request,
):
    """Prepare a short-lived, server-bound confirmation for one exact inverse."""
    scope = _authorized_run_context(
        request,
        run_id=body.run_id,
        conversation_id=body.conversation_id,
        expected_revision=body.expected_revision,
        require_active=False,
    )
    operation_id = _resolve_undo_operation_id(operation_or_mutation_id, scope)
    if scope["snapshot"]["status"] not in {"completed", "failed", "cancelled"}:
        _raise(409, "run_not_terminal", "Undo is available after the Run has stopped")

    def _do_preview() -> tuple[dict[str, Any], dict[str, Any]]:
        from app.agent.file_mutation.undo_manager import UndoManager
        from app.skills.filesystem import file_ops

        journal = scope["journal"]
        operation = journal.load_operation_internal(body.run_id, operation_id)
        if operation is None:
            _raise(404, "operation_not_found", "File operation was not found in this Run")
        claim = scope["db"].fetchone(
            """SELECT status FROM run_operations WHERE run_id = ?
               AND related_operation_id = ? AND operation_kind = 'undo'
               AND status NOT IN ('failed', 'cancelled')
               ORDER BY sequence_no DESC LIMIT 1""",
            (body.run_id, operation_id),
        )
        if claim is not None:
            if claim["status"] == "verified":
                _raise(409, "undo_already_completed", "This file change has already been undone")
            _raise(409, "undo_in_progress", "This Undo is already in progress or needs review")
        manager = UndoManager(journal, store=file_ops._p3_snapshot_store())
        with _runtime_context(scope):
            preview = manager.preview(body.run_id, operation_id)
        return operation, preview

    try:
        operation, preview = await asyncio.to_thread(_do_preview)
    except HTTPException:
        raise
    except Exception as exc:
        name = type(exc).__name__
        if name == "UndoConflictError":
            _raise(409, "undo_conflict", "The file changed after the Undo preview; it was left untouched")
        if name == "UndoPermissionError":
            _raise(403, "permission_denied", "Current file permissions do not allow Undo")
        if name == "UndoUnavailableError":
            _raise(409, "undo_unavailable", "Undo evidence is unavailable or expired")
        if name == "OperationNotFoundError":
            _raise(404, "operation_not_found", "File operation was not found in this Run")
        _raise(409, "undo_preview_failed", "The Undo preview could not be prepared safely")

    confirmation_id = secrets.token_urlsafe(32)
    expires_at = time.monotonic() + UNDO_CONFIRMATION_TTL_SECONDS
    response = {
        "confirmation_id": confirmation_id,
        "run_id": body.run_id,
        "conversation_id": body.conversation_id,
        "operation_id": operation_id,
        "revision": int(scope["snapshot"]["revision"]),
        "operation": str(operation.get("operation_kind") or "file_write"),
        "files": preview["files"],
        "expires_in_seconds": UNDO_CONFIRMATION_TTL_SECONDS,
    }
    confirmation = UndoConfirmationRecord(
        confirmation_id=confirmation_id,
        session_id=str(scope["session_id"]),
        principal_id=str(scope["principal_id"]),
        run_id=body.run_id,
        conversation_id=body.conversation_id,
        workspace_key=_workspace_key(scope["workspace_dir"]),
        operation_id=operation_id,
        revision=int(scope["snapshot"]["revision"]),
        fingerprint_digest=str(preview["fingerprint_digest"]),
        preview=response,
        expires_at=expires_at,
    )
    with _RECORD_LOCK:
        _purge_expired_records()
        if len(_UNDO_CONFIRMATIONS) >= UNDO_CONFIRMATION_LIMIT:
            _raise(429, "undo_preview_limit", "Too many pending Undo previews; refresh and try again")
        _UNDO_CONFIRMATIONS[confirmation_id] = confirmation
    return response


@router.post("/{operation_or_mutation_id}/undo")
async def undo_file_mutation(
    operation_or_mutation_id: str,
    body: FileMutationUndoIn,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=128),
):
    key = _check_idempotency_key(idempotency_key)
    # Authenticate the caller against a persisted Run first. The route ID may
    # be a mutation ID from this process or a durable P3 operation ID from any
    # completed Agent/API action; the latter survives app restarts.
    scope = _authorized_run_context(
        request,
        run_id=body.run_id,
        conversation_id=body.conversation_id,
        # The opaque confirmation record binds the original revision. A same-key
        # retry after a completed inverse must reach Journal's durable replay.
        expected_revision=None,
        require_active=False,
    )
    operation_id = _resolve_undo_operation_id(operation_or_mutation_id, scope)
    request_digest = _request_digest({
        "run_id": body.run_id,
        "conversation_id": body.conversation_id,
        "operation_id": operation_id,
        "expected_revision": body.expected_revision,
        "confirmation_id": body.confirmation_id,
    })
    with _RECORD_LOCK:
        _purge_expired_records()
        confirmation = _UNDO_CONFIRMATIONS.get(body.confirmation_id)
        if confirmation is None or confirmation.expires_at <= time.monotonic():
            _raise(409, "undo_confirmation_expired", "The Undo preview expired; prepare a new preview")
        if (
            confirmation.session_id != str(scope["session_id"])
            or confirmation.principal_id != str(scope["principal_id"])
            or confirmation.run_id != body.run_id
            or confirmation.conversation_id != body.conversation_id
            or confirmation.workspace_key != _workspace_key(scope["workspace_dir"])
            or confirmation.operation_id != operation_id
            or confirmation.revision != body.expected_revision
        ):
            _raise(409, "undo_confirmation_invalid", "The Undo confirmation does not match this file change")
        if confirmation.state == "used":
            if confirmation.idempotency_key != key or confirmation.request_digest != request_digest:
                _raise(409, "undo_confirmation_used", "This Undo confirmation has already been used")
            replay = True
        else:
            confirmation.state = "used"
            confirmation.idempotency_key = key
            confirmation.request_digest = request_digest
            replay = False

    def _do_undo() -> dict[str, Any]:
        from app.agent.file_mutation.undo_manager import UndoManager, UndoConflictError
        from app.skills.filesystem import file_ops

        with _runtime_context(scope):
            manager = UndoManager(scope["journal"], store=file_ops._p3_snapshot_store())
            if not replay:
                current_preview = manager.preview(body.run_id, operation_id)
                if not hmac.compare_digest(
                    str(current_preview.get("fingerprint_digest") or ""),
                    confirmation.fingerprint_digest,
                ):
                    raise UndoConflictError("file state changed after the Undo preview")
            return manager.undo(
                body.run_id,
                operation_id,
                expected_revision=body.expected_revision,
                idempotency_key=key,
                principal_id=str(scope["principal_id"]),
                confirmed=True,
            )

    try:
        result = await asyncio.to_thread(_do_undo)
    except HTTPException:
        raise
    except Exception as exc:
        name = type(exc).__name__
        if name == "UndoConflictError":
            _raise(409, "undo_conflict", "The file changed after the Undo preview; it was left untouched")
        if name == "UndoPermissionError":
            _raise(403, "permission_denied", "Current file permissions do not allow Undo")
        if name == "UndoUnavailableError":
            _raise(409, "undo_unavailable", "Undo evidence is unavailable or expired")
        if name == "UndoNeedsReviewError":
            _raise(409, "external_effect_unknown", "Undo stopped with an unresolved file effect")
        if name == "UndoIdempotencyConflictError":
            _raise(409, "idempotency_conflict", "Idempotency-Key was reused with a different Undo request")
        if name == "OperationNotFoundError":
            _raise(404, "operation_not_found", "File operation was not found in this Run")
        if name == "RevisionConflictError":
            _raise(409, "stale_preview", "The Run changed before Undo could be claimed")
        _raise(409, "undo_failed", "Undo could not be completed safely")
    return result
