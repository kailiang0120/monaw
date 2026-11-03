from __future__ import annotations

import fnmatch
import io
import json
import os
import hashlib
import shutil
import stat
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent.access_grant_broker import create_grant_ticket
from app.agent.conversation_workspace import workspace_path
from app.agent.approval_broker import create_ticket
from app.agent.controller_policy import ActionType, blocked_roots_below, canonical, resolve_permission
from app.agent.execution_resume import register_executor
from app.agent.durable.operation_execution import current_operation_execution
from app.agent.file_mutation.fingerprint import FingerprintError, fingerprint_path
from app.agent.file_mutation.planner import MutationPlan, PathState, PlanOperand, build_plan
from app.agent.response_attachments import ATTACHMENT_HANDLE_PREFIX, resolve_attachment_handle_path
from app.agent.run_context import (
    current_control_session_id,
    current_conversation_id,
    current_execution_source,
)
from app.agent.tool_cancellation import current_call_id, register_cancellation, unregister_cancellation

_MAX_READ_BYTES = 1024 * 1024
_DEFAULT_READ_BYTES = 65536
_MAX_LIST_LIMIT = 1000
_MAX_SEARCH_RESULTS = 200
_MAX_WRITE_BYTES = 16 * 1024 * 1024
_MAX_RECURSIVE_ITEMS = 5000
_MAX_RECURSIVE_BYTES = 512 * 1024 * 1024
_WALK_TIME_BUDGET_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class FilesystemTarget:
    raw: str
    resolved: Path
    parent: Path
    exists: bool
    is_symlink: bool

def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _error(reason_code: str, error: str, **extra: Any) -> str:
    return _json({"status": "error", "reason_code": reason_code, "error": error, **extra})

def _safe_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _stat_payload(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "type": "directory" if path.is_dir() else "file" if path.is_file() else "other",
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
        "size": stat.st_size,
        "modified_at": _iso_from_timestamp(stat.st_mtime),
        "created_at": _iso_from_timestamp(stat.st_ctime),
    }


def _missing_stat_payload(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": False,
        "type": "missing",
        "is_file": False,
        "is_dir": False,
        "size": 0,
        "modified_at": "",
        "created_at": "",
    }


def _blocked_result(decision) -> str:
    return _json(
        {
            "status": "blocked",
            "reason": decision.reason,
            "reason_code": decision.reason_code,
            "policy_source": decision.policy_source,
        }
    )


def _pending_access_grant_result(
    decision,
    *,
    path: str,
    action_context: str,
    requested_access: str = "read",
) -> str:
    ticket = create_grant_ticket(
        target_type="path",
        target_identifier=path,
        display_name=path,
        action_context=action_context or decision.reason,
        requested_access=requested_access,
    )
    return _json(
        {
            "status": "pending_access_grant",
            "ticket_id": ticket.id,
            "target_type": ticket.target_type,
            "target_identifier": ticket.target_identifier,
            "display_name": ticket.display_name,
            "action_context": ticket.action_context,
            "requested_access": ticket.requested_access,
            "reason": decision.reason,
            "reason_code": decision.reason_code,
            "policy_source": decision.policy_source,
        }
    )


def _pending_approval_result(
    decision,
    *,
    tool_name: str,
    action_type: str,
    path: str,
    action_description: str,
    payload_args: dict[str, Any],
) -> str:
    # Approval can resume in a different request context. Pin relative targets
    # to the folder the user was shown when the ticket was created.
    payload_args = {
        key: _policy_target_path(value) if key in {"path", "source", "destination"} and isinstance(value, str) else value
        for key, value in payload_args.items()
    }
    input_str = json.dumps(payload_args, ensure_ascii=False, sort_keys=True)
    ticket = create_ticket(
        action_type=action_type,
        tool_name=tool_name,
        target_path=path,
        risk_level="medium",
        reason=decision.reason,
        action_description=action_description,
        payload={"input_str": input_str, "args": payload_args},
    )
    return _json(
        {
            "status": "pending_approval",
            "ticket_id": ticket.id,
            "action": action_description,
            "reason": decision.reason,
            "reason_code": decision.reason_code,
            "policy_source": decision.policy_source,
        }
    )


def _gate_path(
    action: ActionType,
    *,
    path: str,
    tool_name: str,
    action_description: str,
    payload_args: dict[str, Any],
) -> str | None:
    target_path = _policy_target_path(path)
    decision = resolve_permission(action, target_path=target_path)
    if decision.blocked:
        return _blocked_result(decision)
    if decision.requires_access_grant:
        requested_access = (
            "delete" if action == ActionType.DELETE
            else "write" if action in {ActionType.MUTATE, ActionType.EXEC}
            else "launch" if action == ActionType.LAUNCH_APP
            else "read"
        )
        return _pending_access_grant_result(
            decision,
            path=target_path,
            action_context=action_description,
            requested_access=requested_access,
        )
    if decision.requires_confirmation:
        return _pending_approval_result(
            decision,
            tool_name=tool_name,
            action_type=action.value,
            path=target_path,
            action_description=action_description,
            payload_args=payload_args,
        )
    return None


def _resolve_path(path: str) -> Path:
    attachment_path = resolve_attachment_handle_path(path)
    if attachment_path is not None:
        return attachment_path
    return Path(canonical(str(workspace_path(path))))


def _policy_target_path(path: str) -> str:
    attachment_path = resolve_attachment_handle_path(path)
    return str(attachment_path) if attachment_path is not None else str(workspace_path(path))


def _same_resolved_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def _filesystem_target(path: str) -> FilesystemTarget:
    raw_path = workspace_path(path)
    resolved = _resolve_path(path)
    parent_source = raw_path.parent if str(raw_path.parent) else Path(".")
    return FilesystemTarget(
        raw=path,
        resolved=resolved,
        parent=_resolve_path(str(parent_source)),
        exists=resolved.exists(),
        is_symlink=raw_path.is_symlink() or resolved.is_symlink(),
    )


def _has_path_traversal(path: str) -> bool:
    return any(part == ".." for part in Path(path).parts)


def _validate_path_shape(path: str, *, operation: str) -> str | None:
    if str(path or "").strip().startswith(ATTACHMENT_HANDLE_PREFIX):
        if resolve_attachment_handle_path(path) is None:
            return _error("attachment_not_found", f"{operation} attachment handle is not available in this context.")
        return None
    if _has_path_traversal(path):
        return _error("path_traversal_rejected", f"{operation} path contains parent traversal segments.")
    return None


def _validate_glob_pattern(pattern: str, *, operation: str) -> str | None:
    value = str(pattern or "")
    normalized = value.replace("\\", "/")
    if Path(value).is_absolute() or Path(value).drive or normalized.startswith("/"):
        return _error("glob_pattern_rejected", f"{operation} pattern must be relative to the base path.")
    if ".." in normalized.split("/"):
        return _error("path_traversal_rejected", f"{operation} pattern contains parent traversal segments.")
    return None


def _within_base(item: Path, base: Path) -> bool:
    """True when ``item`` resolves inside ``base``; walks use it to drop links that leave the gated base."""
    try:
        resolved_item = os.path.normcase(canonical(str(item)))
        resolved_base = os.path.normcase(canonical(str(base)))
        return os.path.commonpath([resolved_item, resolved_base]) == resolved_base
    except (OSError, ValueError):
        return False


def _is_link_entry(entry: os.DirEntry) -> bool:
    """True for symlinks and Windows junctions, which walks must not descend."""
    try:
        if entry.is_symlink():
            return True
        is_junction = getattr(entry, "is_junction", None)  # Python 3.12+
        if is_junction is not None:
            return bool(is_junction())
        reparse_tag = getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0)
        return reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    except OSError:
        return True


class _WalkBudget:
    """Time budget plus Stop/timeout cancellation shared by directory walks."""

    def __init__(self) -> None:
        self._deadline = time.monotonic() + _WALK_TIME_BUDGET_SECONDS
        self._cancelled = threading.Event()
        self._call_id = current_call_id()
        self.exhausted_reason = ""

    def __enter__(self) -> _WalkBudget:
        register_cancellation(self._call_id, self._cancelled.set)
        return self

    def __exit__(self, *_exc_info: object) -> None:
        unregister_cancellation(self._call_id)

    def exhausted(self) -> bool:
        if not self.exhausted_reason:
            if self._cancelled.is_set():
                self.exhausted_reason = "cancelled"
            elif time.monotonic() > self._deadline:
                self.exhausted_reason = "time_budget"
        return bool(self.exhausted_reason)


@dataclass(frozen=True, slots=True)
class _WalkEntry:
    entry: os.DirEntry
    relative_parts: tuple[str, ...]
    restricted: bool


def _walk(base: Path, budget: _WalkBudget, *, max_depth: int | None = None) -> Iterator[_WalkEntry]:
    """Breadth-first walk of ``base`` in name order.

    Links and junctions are yielded but never followed (and dropped when they
    leave ``base``), blocked roots nested under ``base`` are yielded as
    restricted but not entered, nothing below ``max_depth`` is read, and the
    walk ends as soon as ``budget`` is exhausted.
    """
    if max_depth is not None and max_depth < 1:
        return
    restricted_dirs = {os.path.normcase(root) for root in blocked_roots_below(str(base))}
    pending: deque[tuple[str, tuple[str, ...]]] = deque([(str(base), ())])
    while pending:
        directory, parent_parts = pending.popleft()
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name.lower())
        except OSError:
            continue
        for entry in children:
            if budget.exhausted():
                return
            is_link = _is_link_entry(entry)
            if is_link and not _within_base(Path(entry.path), base):
                continue
            parts = (*parent_parts, entry.name)
            restricted = os.path.normcase(entry.path) in restricted_dirs
            yield _WalkEntry(entry, parts, restricted)
            if is_link or restricted or (max_depth is not None and len(parts) >= max_depth):
                continue
            try:
                if entry.is_dir():
                    pending.append((entry.path, parts))
            except OSError:
                continue


def _glob_parts(pattern: str) -> tuple[str, ...]:
    return tuple(part for part in pattern.replace("\\", "/").split("/") if part not in ("", "."))


def _glob_max_depth(pattern: tuple[str, ...]) -> int | None:
    return None if "**" in pattern else len(pattern)


def _match_glob_parts(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_match_glob_parts(parts[index:], rest) for index in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatch(parts[0], head) and _match_glob_parts(parts[1:], rest)


def _glob_matches(item: _WalkEntry, pattern: tuple[str, ...]) -> bool:
    """Match like ``Path.glob``: ``**`` spans directories and a trailing ``**`` selects directories."""
    if pattern and pattern[-1] == "**" and not item.entry.is_dir():
        return False
    return _match_glob_parts(item.relative_parts, pattern)


def _entry_payload(item: _WalkEntry) -> dict[str, Any]:
    entry = item.entry
    info = entry.stat()
    is_dir = entry.is_dir()
    is_file = entry.is_file()
    payload = {
        "path": entry.path,
        "exists": True,
        "type": "directory" if is_dir else "file" if is_file else "other",
        "is_file": is_file,
        "is_dir": is_dir,
        "size": info.st_size,
        "modified_at": _iso_from_timestamp(info.st_mtime),
        "created_at": _iso_from_timestamp(info.st_ctime),
        "depth": len(item.relative_parts),
        "relative_path": os.path.join(*item.relative_parts),
    }
    if item.restricted:
        payload["restricted"] = True
    return payload


def _walk_entries(
    base: Path,
    *,
    max_items: int,
    max_depth: int | None = None,
    pattern: tuple[str, ...] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Collect up to ``max_items`` walk payloads in path order.

    Returns the entries and why the walk stopped early ("" when it completed).
    """
    collected: list[tuple[list[str], dict[str, Any]]] = []
    truncated_reason = ""
    with _WalkBudget() as budget:
        for item in _walk(base, budget, max_depth=max_depth):
            if pattern is not None and not _glob_matches(item, pattern):
                continue
            if len(collected) >= max_items:
                truncated_reason = "limit"
                break
            try:
                collected.append(([part.lower() for part in item.relative_parts], _entry_payload(item)))
            except OSError:
                continue
        truncated_reason = truncated_reason or budget.exhausted_reason
    collected.sort(key=lambda pair: pair[0])
    return [payload for _key, payload in collected], truncated_reason


def _truncated_result(result: dict[str, Any], truncated_reason: str) -> str:
    result["truncated"] = bool(truncated_reason)
    if truncated_reason:
        result["truncated_reason"] = truncated_reason
    return _json(result)


def _validate_write_budget(content: str, encoding: str) -> str | None:
    size = len(str(content or "").encode(encoding or "utf-8", errors="replace"))
    if size > _MAX_WRITE_BYTES:
        return _error(
            "write_size_limit_exceeded",
            "Write content exceeds filesystem write budget.",
            bytes=size,
            max_bytes=_MAX_WRITE_BYTES,
        )
    return None


def _native_file_execution():
    execution = current_operation_execution()
    return execution if execution is not None and execution.can_promote_native_file_mutation() else None


def _p3_snapshot_store():
    from app.agent.file_mutation.snapshot_store import SnapshotStore

    return SnapshotStore()


def _p3_verifier(store):
    from app.agent.file_mutation.verifier import FileMutationVerifier

    return FileMutationVerifier(store)


def _p3_metadata_state(path: Path) -> PathState:
    """Read path metadata without opening or hashing file content."""
    absolute = Path(os.path.abspath(str(path)))
    try:
        value = os.lstat(absolute)
    except FileNotFoundError:
        try:
            parent = os.stat(absolute.parent)
            parent_identity = (int(parent.st_dev), int(parent.st_ino))
        except OSError:
            parent_identity = None
        return PathState.metadata(
            str(absolute), exists=False, kind="absent", size=0,
            parent_identity=parent_identity,
        )
    if (
        stat.S_ISLNK(value.st_mode)
        or bool(int(getattr(value, "st_file_attributes", 0)) & 0x0400)
        or not stat.S_ISREG(value.st_mode)
        or int(getattr(value, "st_nlink", 1)) != 1
    ):
        raise FingerprintError("only ordinary single-link regular files are supported")
    try:
        parent = os.stat(absolute.parent)
    except OSError as exc:
        raise FingerprintError("file parent is unavailable") from exc
    return PathState.metadata(
        str(absolute),
        exists=True,
        kind="file",
        size=int(value.st_size),
        identity=(int(value.st_dev), int(value.st_ino)),
        modified_ns=int(getattr(value, "st_mtime_ns", 0)),
        created_ns=int(getattr(value, "st_ctime_ns", 0)),
        parent_identity=(int(parent.st_dev), int(parent.st_ino)),
    )


def _p3_read_access(
    paths: list[Path],
    *,
    operation: str,
    tool_name: str,
    action_description: str,
    payload_args: dict[str, Any],
    dry_run: bool,
    execution=None,
) -> tuple[bool, str | None]:
    """Return whether contents may be read, or a current access-gate result."""
    read_authorized = True
    for path in paths:
        target = str(path)
        decision = resolve_permission(
            ActionType.READ, target_path=target, consume_once_grants=False
        )
        if decision.blocked:
            if dry_run:
                read_authorized = False
                continue
            return False, _blocked_result(decision)
        if not decision.allowed:
            if dry_run:
                read_authorized = False
                continue
            return False, _error("read_permission_required", "READ permission is required before this file mutation.")
        if decision.requires_confirmation:
            if dry_run:
                read_authorized = False
                continue
            return False, _error("read_permission_required", "READ confirmation is required before this file mutation.")
        if decision.requires_access_grant:
            read_authorized = False
            if dry_run:
                continue
            pending = _gate_path(
                ActionType.READ,
                path=target,
                tool_name=tool_name,
                action_description=f"Read permission for {operation}: {action_description}",
                payload_args=payload_args,
            )
            return False, pending or _blocked_result(decision)
    return read_authorized, None


def _p3_initial_mutation_access(
    *,
    execution,
    action_targets: list[tuple[ActionType, str]],
    tool_name: str,
    action_description: str,
    payload_args: dict[str, Any],
) -> str | None:
    """Resolve hard blocks and access grants before planning reads file bytes."""
    for action, path in action_targets:
        decision = resolve_permission(
            action, target_path=path, consume_once_grants=False
        )
        if decision.blocked:
            if execution is not None:
                execution.native_file_no_effect(decision.reason_code or "operation_failed")
            return _blocked_result(decision)
        if decision.requires_access_grant:
            return _gate_path(
                action,
                path=path,
                tool_name=tool_name,
                action_description=action_description,
                payload_args=payload_args,
            )
    return None


def _p3_grant_access_name(action: ActionType) -> str:
    if action == ActionType.READ:
        return "read"
    if action == ActionType.DELETE:
        return "delete"
    return "write"


def _p3_legacy_grant_binding(
    execution,
    *,
    action: ActionType,
    path: str,
    tool_name: str,
    payload_args: dict[str, Any],
) -> str:
    normalized_args = {
        key: _policy_target_path(value)
        if key in {"path", "source", "destination"} and isinstance(value, str)
        else value
        for key, value in payload_args.items()
    }
    binding = {
        "run_id": str(execution.run_id),
        "operation_id": str(execution.operation_id),
        "tool_name": tool_name,
        "action": action.value,
        "path": _policy_target_path(path),
        "args": normalized_args,
        "control_session_id": current_control_session_id(),
        "conversation_id": current_conversation_id(),
        "execution_source": current_execution_source(),
    }
    return hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _p3_legacy_grant_approval_is_current(
    execution,
    *,
    action: ActionType,
    path: str,
    tool_name: str,
    payload_args: dict[str, Any],
) -> bool:
    ticket_id = str(getattr(execution, "approved_ticket_id", "") or "")
    binding_digest = _p3_legacy_grant_binding(
        execution,
        action=action,
        path=path,
        tool_name=tool_name,
        payload_args=payload_args,
    )
    if not ticket_id or not execution.has_native_transient_grant_approval(
        ticket_id, binding_digest
    ):
        return False
    from app.agent.approval_broker import get_ticket, ticket_validation_error

    ticket = get_ticket(ticket_id)
    if (
        ticket is None
        or ticket.tool_name != tool_name
        or ticket.action_type != action.value
        or ticket.target_path != _policy_target_path(path)
    ):
        return False
    expected_args = {
        key: _policy_target_path(value)
        if key in {"path", "source", "destination"} and isinstance(value, str)
        else value
        for key, value in payload_args.items()
    }
    return bool(
        ticket.payload.get("args") == expected_args
        and not ticket_validation_error(
            ticket,
            expected_session_id=current_control_session_id(),
            expected_conversation_id=current_conversation_id(),
            expected_execution_source=current_execution_source(),
            require_approved=True,
        )
    )


def _p3_durable_eligibility(
    *,
    execution,
    action_targets: list[tuple[ActionType, str]],
    read_paths: list[Path],
    tool_name: str,
    action_description: str,
    payload_args: dict[str, Any],
) -> tuple[bool, str | None]:
    """Use P3 only under durable path policy; preserve one-time grant semantics."""
    from app.agent.access_grant_broker import session_grant_kind

    for action, path in action_targets:
        stable = resolve_permission(
            action,
            target_path=path,
            allow_session_grants=False,
            consume_once_grants=False,
        )
        if stable.blocked:
            execution.native_file_no_effect(stable.reason_code or "operation_failed")
            return False, _blocked_result(stable)
        if stable.requires_access_grant:
            if _p3_legacy_grant_approval_is_current(
                execution,
                action=action,
                path=path,
                tool_name=tool_name,
                payload_args=payload_args,
            ):
                # P2's approved continuation remains on its original opaque
                # native path after the exact once grant was consumed.
                return False, None
            if session_grant_kind("path", path, _p3_grant_access_name(action)):
                # P2's legacy boundary remains conservative for an operation
                # authorized only by a transient/session path grant. Do not
                # consume the grant during P3 preview or verification.
                execution.note_native_transient_grant_candidate(
                    _p3_legacy_grant_binding(
                        execution,
                        action=action,
                        path=path,
                        tool_name=tool_name,
                        payload_args=payload_args,
                    )
                )
                return False, None
            return False, _gate_path(
                action,
                path=path,
                tool_name=tool_name,
                action_description=action_description,
                payload_args=payload_args,
            )

    for path in read_paths:
        stable = resolve_permission(
            ActionType.READ,
            target_path=str(path),
            allow_session_grants=False,
            consume_once_grants=False,
        )
        if stable.blocked:
            execution.native_file_no_effect(stable.reason_code or "operation_failed")
            return False, _blocked_result(stable)
        if stable.requires_access_grant:
            if session_grant_kind("path", str(path), "read"):
                return False, None
            return False, _gate_path(
                ActionType.READ,
                path=str(path),
                tool_name=tool_name,
                action_description=f"Read permission for {action_description}",
                payload_args=payload_args,
            )
        if stable.requires_confirmation or not stable.allowed:
            execution.native_file_no_effect("operation_failed")
            return False, _error(
                "read_permission_required",
                "Stable READ permission is required before this file mutation can be verified.",
            )
    return True, None


def _p3_plan_file_write(
    *,
    path: Path,
    operation: str,
    request: dict[str, Any],
    after_bytes: bytes,
    encoding: str,
    read_authorized: bool,
) -> tuple[MutationPlan, dict[str, bytes], Any]:
    if read_authorized:
        fingerprint = fingerprint_path(path, include_bytes=True)
        before = PathState.from_fingerprint(fingerprint)
        before_bytes = fingerprint.data or b"" if fingerprint.exists else None
    else:
        fingerprint = None
        before = _p3_metadata_state(path)
        before_bytes = None
    if before.exists and before.kind != "file":
        raise FingerprintError("target is not a regular file")
    after = PathState.from_bytes(
        path,
        after_bytes,
        identity=before.identity if before.exists else None,
        parent_identity=before.parent_identity,
    )
    operand = PlanOperand(
        path=str(path),
        role="target",
        before=before,
        after=after,
        identity_strategy="same" if before.exists else "replaced",
    )
    plan = build_plan(
        operation,
        request,
        [operand],
        before_bytes=before_bytes,
        after_bytes=after_bytes,
        read_authorized=read_authorized,
        encoding=encoding,
        undo_capability="available",
    )
    before_bytes_by_path = (
        {str(path): before_bytes} if before.exists and before_bytes is not None else {}
    )
    return plan, before_bytes_by_path, fingerprint


def _p3_legacy_text_bytes(text: str, encoding: str) -> bytes:
    """Encode exactly as the legacy text stream with newline='' would."""
    buffer = io.BytesIO()
    writer = io.TextIOWrapper(buffer, encoding=encoding or "utf-8", newline="")
    writer.write(text)
    writer.flush()
    result = buffer.getvalue()
    writer.detach()
    return result


def _p3_dry_run_text_plan(
    *,
    path: Path,
    operation: str,
    tool_name: str,
    action_description: str,
    payload_args: dict[str, Any],
    request: dict[str, Any],
    encoding: str,
    append_content: str | None = None,
    patch_old: str | None = None,
    patch_new: str | None = None,
    patch_count: int = 1,
) -> tuple[MutationPlan, PathState, int | None, str | None]:
    """Build a read-gated, content-bounded Dry Run plan for append or patch."""
    read_authorized, read_error = _p3_read_access(
        [path],
        operation=operation,
        tool_name=tool_name,
        action_description=action_description,
        payload_args=payload_args,
        dry_run=True,
        execution=None,
    )
    if read_error:
        raise FingerprintError("READ policy could not be checked for preview")

    before = _p3_metadata_state(path)
    before_bytes: bytes | None = None
    if read_authorized and before.exists:
        try:
            fingerprint = fingerprint_path(path, include_bytes=True)
            before = PathState.from_fingerprint(fingerprint)
            before_bytes = fingerprint.data
        except FingerprintError:
            # Keep a useful content-unavailable preview for oversized or
            # changing files without relaxing the Dry Run's READ gate.
            before_bytes = None

    replacements: int | None = None
    after_bytes: bytes | None = None
    appended_size: int | None = None
    if operation == "append":
        appended = _p3_legacy_text_bytes(str(append_content or ""), encoding)
        appended_size = len(appended)
        if read_authorized and (not before.exists or before_bytes is not None):
            after_bytes = (before_bytes or b"") + appended
    elif operation == "patch":
        if before.exists and read_authorized and before_bytes is not None:
            reader = io.TextIOWrapper(
                io.BytesIO(before_bytes), encoding=encoding or "utf-8", newline=None
            )
            try:
                text_before = reader.read()
            finally:
                reader.detach()
            old_text = str(patch_old or "")
            new_text = str(patch_new or "")
            occurrences = text_before.count(old_text)
            if occurrences == 0:
                return (
                    build_plan(
                        operation,
                        request,
                        [PlanOperand(str(path), "target", before, before, identity_strategy="same")],
                        read_authorized=False,
                        encoding=encoding,
                    ),
                    before,
                    0,
                    "patch_text_not_found",
                )
            max_count = _safe_int(patch_count, 1, minimum=1, maximum=100000)
            replacements = min(occurrences, max_count)
            text_after = text_before.replace(old_text, new_text, max_count)
            after_bytes = _p3_legacy_text_bytes(text_after, encoding)
    else:
        raise ValueError("Dry Run text planner only supports append and patch")

    if after_bytes is not None:
        after = PathState.from_bytes(
            path,
            after_bytes,
            identity=before.identity if before.exists else None,
            modified_ns=before.modified_ns,
            created_ns=before.created_ns,
            parent_identity=before.parent_identity,
        )
    elif operation == "append":
        after = PathState.metadata(
            path,
            exists=True,
            kind="file",
            size=(before.size + (appended_size or 0)) if before.size is not None else None,
            identity=before.identity if before.exists else None,
            parent_identity=before.parent_identity,
        )
    else:
        after = PathState.metadata(
            path,
            exists=True,
            kind="file",
            size=None,
            identity=before.identity,
            modified_ns=before.modified_ns,
            created_ns=before.created_ns,
            parent_identity=before.parent_identity,
        )

    identity_strategy = "same" if before.exists else "replaced"
    plan = build_plan(
        operation,
        request,
        [PlanOperand(str(path), "target", before, after, identity_strategy=identity_strategy)],
        before_bytes=before_bytes,
        after_bytes=after_bytes,
        read_authorized=read_authorized,
        encoding=encoding,
    )
    return plan, before, replacements, None


def _p3_plan_action_description(description: str, plan: MutationPlan) -> str:
    preview = plan.to_preview_dict().get("preview")
    if not isinstance(preview, dict):
        return description
    reason = str(preview.get("reason") or "metadata_only")
    return f"{description} (preview available; {reason})"


def _p3_attach_preview(output: str, plan: MutationPlan) -> str:
    """Keep the sanitized preview transient in the gate output, not its ticket."""
    try:
        payload = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return output
    if not isinstance(payload, dict) or payload.get("status") not in {
        "pending_approval", "pending_access_grant"
    }:
        return output
    payload["preview"] = plan.to_preview_dict()
    return _json(payload)


def _p3_approval_is_current(execution, plan: MutationPlan, *, tool_name: str, action: ActionType) -> bool:
    ticket_id = str(getattr(execution, "approved_ticket_id", "") or "")
    if not ticket_id or getattr(execution, "approved_binding_digest", "") != plan.binding_digest:
        return False
    from app.agent.approval_broker import get_ticket, ticket_validation_error

    ticket = get_ticket(ticket_id)
    if ticket is None or ticket.tool_name != tool_name or ticket.action_type != action.value:
        return False
    return not ticket_validation_error(
        ticket,
        expected_session_id=current_control_session_id(),
        expected_conversation_id=current_conversation_id(),
        expected_execution_source=current_execution_source(),
        require_approved=True,
    )


def _p3_gate_plan(
    *,
    execution,
    plan: MutationPlan,
    action_targets: list[tuple[ActionType, str]],
    tool_name: str,
    action_description: str,
    payload_args: dict[str, Any],
    bypass_gate: bool,
) -> str | None:
    """Gate current path policy and bind any approval to this private plan."""
    preview_description = _p3_plan_action_description(action_description, plan)
    first_confirmation: tuple[ActionType, str, Any] | None = None
    for action, path in action_targets:
        decision = resolve_permission(
            action, target_path=path, consume_once_grants=False
        )
        if decision.blocked:
            if execution is not None:
                execution.native_file_no_effect(decision.reason_code or "operation_failed")
            return _blocked_result(decision)
        if decision.requires_access_grant:
            return _gate_path(
                action,
                path=path,
                tool_name=tool_name,
                action_description=preview_description,
                payload_args=payload_args,
            )
        if decision.requires_confirmation and first_confirmation is None:
            first_confirmation = (action, path, decision)

    approval_action = first_confirmation[0] if first_confirmation else action_targets[0][0]
    approval_current = bool(
        execution is not None
        and execution.approved_ticket_id
        and _p3_approval_is_current(
            execution, plan, tool_name=tool_name, action=approval_action
        )
    )
    active_ticket_is_mutation_approval = False
    if execution is not None and execution.approved_ticket_id:
        from app.agent.approval_broker import get_ticket

        active_ticket = get_ticket(execution.approved_ticket_id)
        active_ticket_is_mutation_approval = bool(
            active_ticket is not None
            and active_ticket.action_type in {action.value for action, _path in action_targets}
        )
    stale_approval = bool(active_ticket_is_mutation_approval and not approval_current)
    if stale_approval:
        execution.native_file_no_effect("operation_failed")
        return _error(
            "stale_preview",
            "The file changed after approval. No file change was made; review a fresh preview before retrying.",
        )
    if first_confirmation is not None and not approval_current:
        action, path, decision = first_confirmation or (
            action_targets[0][0],
            action_targets[0][1],
            resolve_permission(
                action_targets[0][0],
                target_path=action_targets[0][1],
                consume_once_grants=False,
            ),
        )
        if execution is not None:
            execution.bind_pending_approval_to_plan(plan.binding_digest)
        review_decision = decision.model_copy(
            update={"requires_confirmation": True, "reason_code": "confirmation_required"}
        )
        return _p3_attach_preview(_pending_approval_result(
            review_decision,
            tool_name=tool_name,
            action_type=action.value,
            path=path,
            action_description=preview_description,
            payload_args=payload_args,
        ), plan)
    if first_confirmation is not None and not approval_current:
        action, path, decision = first_confirmation
        return _p3_attach_preview(_pending_approval_result(
            decision,
            tool_name=tool_name,
            action_type=action.value,
            path=path,
            action_description=preview_description,
            payload_args=payload_args,
        ), plan)
    if execution is not None and first_confirmation is not None and not bypass_gate:
        execution.bind_pending_approval_to_plan(plan.binding_digest)
    if bypass_gate and execution is None and first_confirmation is not None:
        action, path, decision = first_confirmation
        return _p3_attach_preview(_pending_approval_result(
            decision,
            tool_name=tool_name,
            action_type=action.value,
            path=path,
            action_description=preview_description,
            payload_args=payload_args,
        ), plan)
    return None


def _p3_states_match(plan: MutationPlan) -> bool:
    for operand in plan.operands:
        current = PathState.from_fingerprint(fingerprint_path(operand.path, include_bytes=True))
        if current != operand.before:
            return False
    return True


def _p3_capture_and_promote(
    *,
    execution,
    plan: MutationPlan,
    before_bytes_by_path: dict[str, bytes],
    authorized_roots: list[str],
    cleanup_before_abort=None,
) -> tuple[Any | None, str | None]:
    """Persist the private snapshot and verifiable classification before I/O."""
    store = None
    try:
        store = _p3_snapshot_store()
        snapshot_ref = store.capture(
            plan,
            before_bytes_by_path,
            run_id=execution.run_id,
            operation_id=execution.operation_id,
            authorized_roots=authorized_roots,
        )
        execution.promote_native_file_mutation(
            snapshot_ref=snapshot_ref,
            input_summary={
                "target_count": len(plan.operands),
                "byte_length": sum(
                    int(operand.before.size or 0) + int(operand.after.size or 0)
                    for operand in plan.operands
                ),
                "snapshot_available": True,
            },
            precondition_evidence={
                "target_count": len(plan.operands),
                "source_exists": any(
                    operand.role == "source" and operand.before.exists for operand in plan.operands
                ),
                "destination_exists": any(
                    operand.role == "destination" and operand.before.exists for operand in plan.operands
                ),
                "verification_result": "not_checked",
            },
        )
    except Exception:
        if callable(cleanup_before_abort):
            try:
                cleanup_before_abort()
            except OSError:
                pass
        if execution is not None:
            execution.native_file_no_effect("operation_failed")
        return None, _error(
            "snapshot_unavailable",
            "The native file mutation could not commit its private snapshot and journal intent; no file change was made.",
        )

    try:
        if not _p3_states_match(plan):
            if callable(cleanup_before_abort):
                try:
                    cleanup_before_abort()
                except OSError:
                    pass
            execution.native_file_no_effect("operation_failed")
            return store, _error(
                "stale_precondition",
                "The file changed after its preview; no file change was made. Review the operation again.",
            )
    except Exception:
        if callable(cleanup_before_abort):
            try:
                cleanup_before_abort()
            except OSError:
                pass
        execution.native_file_no_effect("operation_failed")
        return store, _error(
            "stale_precondition",
            "The file preconditions could not be rechecked; no file change was made.",
        )
    return store, None


def _p3_stage_new_file(path: Path, data: bytes) -> tuple[Path, PathState]:
    """Create an owned same-directory stage whose identity can be journaled."""
    for _attempt in range(3):
        staged = path.parent / f".{path.name}.monaw-{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(staged, flags, 0o666)
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(descriptor)
        except Exception:
            try:
                staged.unlink(missing_ok=True)
            finally:
                os.close(descriptor)
            raise
        else:
            os.close(descriptor)
        fingerprint = fingerprint_path(staged, include_bytes=True)
        if fingerprint.data != data:
            staged.unlink(missing_ok=True)
            raise FingerprintError("staged file bytes do not match the requested output")
        return staged, PathState.from_fingerprint(fingerprint)
    raise OSError("could not allocate a private staging file")


def _p3_promote_staged_file(staged: Path, destination: Path) -> None:
    """Publish the staged inode only if the destination is still absent."""
    os.link(staged, destination, follow_symlinks=False)
    staged.unlink()


def _p3_recheck_effect_boundary(
    *,
    execution,
    plan: MutationPlan,
    action_targets: list[tuple[ActionType, str]],
    tool_name: str,
    action_description: str,
) -> str | None:
    """Recheck approval, policy, run ownership, and file state just before I/O."""
    try:
        if not _p3_states_match(plan):
            execution.native_file_no_effect("operation_failed")
            return _error("stale_precondition", "File preconditions changed; no file change was made.")
        for action, path in action_targets:
            decision = resolve_permission(
                action, target_path=path, consume_once_grants=False
            )
            if decision.blocked or decision.requires_access_grant:
                execution.native_file_no_effect("operation_failed")
                return _blocked_result(decision)
            if decision.requires_confirmation and not _p3_approval_is_current(
                execution, plan, tool_name=tool_name, action=action
            ):
                execution.native_file_no_effect("operation_failed")
                return _error(
                    "approval_stale",
                    "The file changed or approval expired; review the operation again before retrying.",
                )
        execution.assert_native_file_effect_allowed()
    except Exception:
        execution.native_file_no_effect("operation_failed")
        return _error("stale_precondition", "The native file effect boundary could not be revalidated.")
    return None


def _p3_verify_effect(execution, store) -> tuple[bool, dict[str, Any]]:
    evidence = execution.verify_native_file_mutation(_p3_verifier(store))
    return evidence.get("verification_result") == "completed", evidence


def _p3_write_bytes(path: Path, data: bytes, *, expected_before: PathState) -> None:
    """Write bytes through the existing inode or create the leaf exclusively."""
    existing = expected_before.exists
    expected_identity = expected_before.identity
    flags = getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    if existing:
        flags |= os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (int(opened.st_dev), int(opened.st_ino)) != expected_identity
                or int(opened.st_size) != expected_before.size
                or int(getattr(opened, "st_mtime_ns", 0)) != expected_before.modified_ns
                or (
                    expected_before.created_ns is not None
                    and int(getattr(opened, "st_ctime_ns", 0)) != expected_before.created_ns
                )
            ):
                raise FingerprintError("target identity changed before write")
            current = PathState.from_fingerprint(fingerprint_path(path, include_bytes=True))
            if current != expected_before:
                raise FingerprintError("target preconditions changed before write")
            with os.fdopen(descriptor, "r+b", closefd=False) as handle:
                handle.seek(0)
                handle.truncate(0)
                handle.write(data)
                handle.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return

    current = PathState.from_fingerprint(fingerprint_path(path, include_bytes=True))
    if current != expected_before:
        raise FingerprintError("target preconditions changed before create")
    flags |= os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o666)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tree_budget(path: Path) -> tuple[int, int, str | None]:
    if path.is_symlink():
        return 0, 0, f"Symlink or junction recursion is not allowed: {path}"
    if not path.is_dir():
        try:
            return 1, path.stat().st_size, None
        except OSError as exc:
            return 0, 0, str(exc)
    count = 0
    total = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            return count, total, f"Symlink or junction recursion is not allowed: {item}"
        count += 1
        if count > _MAX_RECURSIVE_ITEMS:
            return count, total, "Recursive filesystem item limit exceeded."
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError as exc:
                return count, total, str(exc)
            if total > _MAX_RECURSIVE_BYTES:
                return count, total, "Recursive filesystem byte limit exceeded."
    return count, total, None


def _validate_recursive_budget(path: Path, *, operation: str) -> str | None:
    count, total, error = _tree_budget(path)
    if error:
        reason = "symlink_target_rejected" if "Symlink" in error else "recursive_budget_exceeded"
        return _error(
            reason,
            error,
            operation=operation,
            items=count,
            bytes=total,
            max_items=_MAX_RECURSIVE_ITEMS,
            max_bytes=_MAX_RECURSIVE_BYTES,
        )
    return None


def _revalidate_mutation_target(path: str, action: ActionType = ActionType.MUTATE) -> str | None:
    target = _filesystem_target(path)
    # Re-check hard blocks against the resolved target right before mutating.
    # Grants were already settled by the gate (or by the approval that
    # replays this call), and a one-time grant is consumed there, so asking
    # again here would re-prompt forever.
    decision = resolve_permission(
        action,
        target_path=str(target.parent if not target.exists else target.resolved),
        consume_once_grants=False,
    )
    if decision.blocked:
        return _blocked_result(decision)
    if target.is_symlink:
        return _error("symlink_target_rejected", f"Refusing to mutate a symlink or junction: {target.resolved}")
    return None


def file_stat(path: str) -> str:
    if not path:
        return _json({"status": "error", "error": "path is required"})
    shape_error = _validate_path_shape(path, operation="filesystem")
    if shape_error:
        return shape_error
    pending = _gate_path(
        ActionType.READ,
        path=path,
        tool_name="fs_stat",
        action_description=f"Stat file path: {path}",
        payload_args={"path": path},
    )
    if pending:
        return pending
    resolved = _resolve_path(path)
    stat = _stat_payload(resolved) if resolved.exists() else _missing_stat_payload(resolved)
    return _json({"status": "ok", "operation": "stat", "path": str(resolved), "exists": stat["exists"], "stat": stat})


def file_list(path: str, pattern: str = "*", recursive: bool = False, limit: int = 200) -> str:
    if not path:
        return _json({"status": "error", "error": "path is required"})
    shape_error = _validate_path_shape(path, operation="filesystem") or _validate_glob_pattern(pattern, operation="list")
    if shape_error:
        return shape_error
    pending = _gate_path(
        ActionType.READ,
        path=path,
        tool_name="fs_list",
        action_description=f"List files under: {path}",
        payload_args={"path": path, "pattern": pattern, "recursive": recursive, "limit": limit},
    )
    if pending:
        return pending

    base = _resolve_path(path)
    if not base.exists():
        return _json({"status": "error", "error": f"Path does not exist: {base}"})
    if not base.is_dir():
        return _json({"status": "error", "error": f"Not a directory: {base}"})

    max_items = _safe_int(limit, 200, minimum=1, maximum=_MAX_LIST_LIMIT)
    glob_pattern = _glob_parts(pattern or "*")
    if recursive:
        glob_pattern = ("**", *glob_pattern)
    entries, truncated_reason = _walk_entries(
        base,
        max_items=max_items,
        max_depth=_glob_max_depth(glob_pattern),
        pattern=glob_pattern,
    )
    return _truncated_result(
        {"status": "ok", "operation": "list", "path": str(base), "count": len(entries), "entries": entries},
        truncated_reason,
    )


def file_read(path: str, start: int = 0, max_bytes: int = _DEFAULT_READ_BYTES, encoding: str = "utf-8") -> str:
    if not path:
        return _json({"status": "error", "error": "path is required"})
    shape_error = _validate_path_shape(path, operation="filesystem")
    if shape_error:
        return shape_error
    pending = _gate_path(
        ActionType.READ,
        path=path,
        tool_name="fs_read",
        action_description=f"Read file: {path}",
        payload_args={"path": path, "start": start, "max_bytes": max_bytes, "encoding": encoding},
    )
    if pending:
        return pending

    resolved = _resolve_path(path)
    if not resolved.exists():
        return _json({"status": "error", "error": f"File does not exist: {resolved}"})
    if not resolved.is_file():
        return _json({"status": "error", "error": f"Not a file: {resolved}"})

    offset = _safe_int(start, 0, minimum=0, maximum=max(os.path.getsize(resolved), 0))
    byte_limit = _safe_int(max_bytes, _DEFAULT_READ_BYTES, minimum=1, maximum=_MAX_READ_BYTES)
    with resolved.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(byte_limit)
    content = data.decode(encoding or "utf-8", errors="replace")
    next_start = offset + len(data)
    size = resolved.stat().st_size
    return _json(
        {
            "status": "ok",
            "operation": "read",
            "path": str(resolved),
            "start": offset,
            "bytes_read": len(data),
            "size": size,
            "truncated": next_start < size,
            "next_start": next_start if next_start < size else None,
            "content": content,
        }
    )


def file_write(
    path: str,
    content: str,
    overwrite: bool = False,
    create_parents: bool = True,
    encoding: str = "utf-8",
    dry_run: bool = False,
    *,
    _bypass_gate: bool = False,
) -> str:
    if not path:
        return _json({"status": "error", "error": "path is required"})
    shape_error = _validate_path_shape(path, operation="write")
    if shape_error:
        return shape_error
    budget_error = _validate_write_budget(content, encoding)
    if budget_error:
        return budget_error
    payload_args = {
        "path": path,
        "content": content,
        "overwrite": overwrite,
        "create_parents": create_parents,
        "encoding": encoding,
        "dry_run": dry_run,
    }
    execution = _native_file_execution()
    use_preview_plan = execution is not None or dry_run
    if use_preview_plan:
        resolved = _resolve_path(path)
        action_targets = [(ActionType.MUTATE, str(resolved))]
        description = f"Write file: {resolved}"
        if execution is not None and not dry_run:
            eligible, gate_result = _p3_durable_eligibility(
                execution=execution,
                action_targets=action_targets,
                read_paths=[resolved],
                tool_name="fs_write",
                action_description=description,
                payload_args=payload_args,
            )
            if gate_result:
                return gate_result
            if not eligible:
                # A session/once grant keeps the pre-P3 native path opaque so
                # its established one-time consumption semantics stay intact.
                use_preview_plan = False
        if use_preview_plan:
            revalidate = _revalidate_mutation_target(path)
            if revalidate:
                if execution is not None:
                    execution.native_file_no_effect("operation_failed")
                return revalidate
            pending = _p3_initial_mutation_access(
                execution=execution,
                action_targets=action_targets,
                tool_name="fs_write",
                action_description=description,
                payload_args=payload_args,
            )
            if pending:
                return pending
            if resolved.exists() and resolved.is_dir():
                if execution is not None:
                    execution.native_file_no_effect("operation_failed")
                return _json({"status": "error", "error": f"Path is a directory: {resolved}"})
            if not dry_run and not resolved.parent.exists():
                if execution is not None:
                    execution.native_file_no_effect("operation_failed")
                return _error(
                    "parent_directory_unavailable",
                    "A verifiable native write requires an existing parent directory; no file change was made.",
                )

            read_authorized, read_result = _p3_read_access(
                [resolved],
                operation="write",
                tool_name="fs_write",
                action_description=description,
                payload_args=payload_args,
                dry_run=dry_run,
                execution=execution,
            )
            if read_result:
                try:
                    read_status = str(json.loads(read_result).get("status") or "")
                except (TypeError, json.JSONDecodeError):
                    read_status = ""
                if execution is not None and read_status not in {
                    "pending_approval", "pending_access_grant"
                }:
                    execution.native_file_no_effect("operation_failed")
                return read_result
            try:
                after_bytes = str(content or "").encode(encoding or "utf-8")
                plan, before_bytes_by_path, before_fingerprint = _p3_plan_file_write(
                    path=resolved,
                    operation="write",
                    request={
                        "path": str(resolved),
                        "content": content,
                        "overwrite": overwrite,
                        "create_parents": create_parents,
                        "encoding": encoding or "utf-8",
                        "dry_run": bool(dry_run),
                    },
                    after_bytes=after_bytes,
                    encoding=encoding or "utf-8",
                    read_authorized=read_authorized,
                )
            except Exception:
                if dry_run and read_authorized:
                    # A bounded diff/fingerprint can be unavailable for a
                    # large or unusual file. Dry Run still reports metadata.
                    try:
                        plan, before_bytes_by_path, before_fingerprint = _p3_plan_file_write(
                            path=resolved,
                            operation="write",
                            request={
                                "path": str(resolved), "content": content, "overwrite": overwrite,
                                "create_parents": create_parents, "encoding": encoding or "utf-8",
                                "dry_run": True,
                            },
                            after_bytes=after_bytes,
                            encoding=encoding or "utf-8",
                            read_authorized=False,
                        )
                        read_authorized = False
                    except Exception:
                        return _error("preview_unavailable", "A metadata-only preview could not be built.")
                else:
                    if execution is not None:
                        execution.native_file_no_effect("operation_failed")
                    return _error(
                        "precondition_unavailable",
                        "The native file preconditions could not be captured; no file change was made.",
                    )

            pending = _p3_gate_plan(
                execution=execution,
                plan=plan,
                action_targets=action_targets,
                tool_name="fs_write",
                action_description=description,
                payload_args=payload_args,
                bypass_gate=_bypass_gate,
            )
            if pending:
                return pending

            before = _stat_payload(resolved) if resolved.exists() else _missing_stat_payload(resolved)
            if dry_run:
                if execution is not None:
                    execution.native_file_no_effect("operation_failed")
                return _json({
                    "status": "ok",
                    "operation": "write",
                    "dry_run": True,
                    "path": str(resolved),
                    "before": before,
                    "would_write_bytes": len(after_bytes),
                    "preview": plan.to_preview_dict(),
                })
            if before["exists"] and not overwrite:
                if execution is not None:
                    execution.native_file_no_effect("operation_failed")
                return _json({
                    "status": "error",
                    "error": f"File already exists: {resolved}",
                    "reason_code": "file_exists",
                })

            # Existing files are updated in place to preserve ACLs, streams,
            # mode bits, and the file's stable identity. New files are staged
            # and published with an exclusive hard link so the expected inode
            # is part of the private manifest before the target appears.
            expected_before = plan.operands[0].before
            if execution is None:
                # Non-durable Dry Runs returned above; durable execution is
                # required for snapshot-backed mutations.
                return _error("durable_context_required", "A durable operation is required for this native write.")
            if expected_before.exists and before_bytes_by_path.get(str(resolved)) == after_bytes:
                execution.native_file_no_effect("operation_failed")
                return _json({
                    "status": "ok", "operation": "write", "path": str(resolved),
                    "before": before, "after": before, "changed": False,
                    "preview": plan.to_preview_dict(),
                })

            staged: Path | None = None
            snapshot_plan = plan
            if not expected_before.exists:
                try:
                    execution.assert_native_file_preflight_allowed()
                    staged, staged_state = _p3_stage_new_file(resolved, after_bytes)
                    staged_after = replace(
                        plan.operands[0].after,
                        identity=staged_state.identity,
                        parent_identity=staged_state.parent_identity,
                    )
                    staged_operand = replace(
                        plan.operands[0],
                        after=staged_after,
                        identity_strategy="replaced",
                    )
                    snapshot_plan = replace(plan, operands=(staged_operand,))
                except Exception:
                    if staged is not None:
                        staged.unlink(missing_ok=True)
                    execution.native_file_no_effect("operation_failed")
                    return _error("staging_failed", "The new file could not be staged safely; no target file was changed.")

            store, snapshot_error = _p3_capture_and_promote(
                execution=execution,
                plan=snapshot_plan,
                before_bytes_by_path=before_bytes_by_path,
                authorized_roots=[str(resolved)],
                cleanup_before_abort=(lambda: staged.unlink(missing_ok=True)) if staged is not None else None,
            )
            if snapshot_error:
                return snapshot_error
            boundary_error = _p3_recheck_effect_boundary(
                execution=execution,
                plan=snapshot_plan,
                action_targets=action_targets,
                tool_name="fs_write",
                action_description=description,
            )
            if boundary_error:
                if staged is not None:
                    staged.unlink(missing_ok=True)
                return boundary_error
            try:
                if staged is not None:
                    _p3_promote_staged_file(staged, resolved)
                else:
                    _p3_write_bytes(
                        resolved,
                        after_bytes,
                        expected_before=expected_before,
                    )
                verified, evidence = _p3_verify_effect(execution, store)
            except Exception:
                if staged is not None:
                    try:
                        staged.unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    _p3_verify_effect(execution, store)
                except Exception:
                    execution.native_file_verification_unavailable()
                return _error(
                    "native_write_failed",
                    "The native write did not complete cleanly. Its effect was checked and may need review.",
                )
            if not verified:
                return _error(
                    "verification_failed",
                    "The native write could not be verified; inspect the file before retrying.",
                    verification_result=str(evidence.get("verification_result") or "unknown"),
                )
            return _json({
                "status": "ok",
                "operation": "write",
                "path": str(resolved),
                "before": before,
                "after": _stat_payload(resolved),
                "verification_result": "completed",
                "preview": plan.to_preview_dict(),
            })

    if not _bypass_gate:
        pending = _gate_path(
            ActionType.MUTATE,
            path=path,
            tool_name="fs_write",
            action_description=f"Write file: {path}",
            payload_args=payload_args,
        )
        if pending:
            if execution is not None:
                try:
                    pending_payload = json.loads(pending)
                except (TypeError, json.JSONDecodeError):
                    pending_payload = {}
                if isinstance(pending_payload, dict) and pending_payload.get("status") == "pending_approval":
                    execution.bind_native_transient_grant_to_approval(
                        str(pending_payload.get("ticket_id") or ""),
                        _p3_legacy_grant_binding(
                            execution,
                            action=ActionType.MUTATE,
                            path=str(_resolve_path(path)),
                            tool_name="fs_write",
                            payload_args=payload_args,
                        ),
                    )
            return pending

    revalidate = _revalidate_mutation_target(path)
    if revalidate:
        return revalidate
    resolved = _resolve_path(path)
    before = _stat_payload(resolved) if resolved.exists() else _missing_stat_payload(resolved)
    if resolved.exists() and resolved.is_dir():
        return _json({"status": "error", "error": f"Path is a directory: {resolved}"})
    if resolved.exists() and not overwrite:
        return _json({"status": "error", "error": f"File already exists: {resolved}", "reason_code": "file_exists"})
    if not resolved.parent.exists():
        if not create_parents:
            return _json({"status": "error", "error": f"Parent directory does not exist: {resolved.parent}"})
        if not dry_run:
            resolved.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return _json({"status": "ok", "operation": "write", "dry_run": True, "path": str(resolved), "before": before, "would_write_bytes": len(content.encode(encoding or "utf-8"))})

    with resolved.open("w", encoding=encoding or "utf-8", newline="") as handle:
        handle.write(content)
    return _json({"status": "ok", "operation": "write", "path": str(resolved), "before": before, "after": _stat_payload(resolved)})


def file_append(
    path: str,
    content: str,
    create_parents: bool = True,
    encoding: str = "utf-8",
    dry_run: bool = False,
    *,
    _bypass_gate: bool = False,
) -> str:
    if not path:
        return _json({"status": "error", "error": "path is required"})
    shape_error = _validate_path_shape(path, operation="append")
    if shape_error:
        return shape_error
    budget_error = _validate_write_budget(content, encoding)
    if budget_error:
        return budget_error
    payload_args = {
        "path": path,
        "content": content,
        "create_parents": create_parents,
        "encoding": encoding,
        "dry_run": dry_run,
    }
    if not _bypass_gate:
        pending = _gate_path(
            ActionType.MUTATE,
            path=path,
            tool_name="fs_append",
            action_description=f"Append file: {path}",
            payload_args=payload_args,
        )
        if pending:
            return pending

    revalidate = _revalidate_mutation_target(path)
    if revalidate:
        return revalidate
    resolved = _resolve_path(path)
    before = _stat_payload(resolved) if resolved.exists() else _missing_stat_payload(resolved)
    if resolved.exists() and resolved.is_dir():
        return _json({"status": "error", "error": f"Path is a directory: {resolved}"})
    if not resolved.parent.exists():
        if not create_parents:
            return _json({"status": "error", "error": f"Parent directory does not exist: {resolved.parent}"})
        if not dry_run:
            resolved.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        try:
            plan, _before_state, _replacements, preview_error = _p3_dry_run_text_plan(
                path=resolved,
                operation="append",
                tool_name="fs_append",
                action_description=f"Append file: {resolved}",
                payload_args=payload_args,
                request={**payload_args, "path": str(resolved)},
                encoding=encoding or "utf-8",
                append_content=content,
            )
        except Exception:
            return _error("preview_unavailable", "An append preview could not be built safely.")
        if preview_error:
            return _error(preview_error, "The append preview could not be built safely.")
        return _json({
            "status": "ok",
            "operation": "append",
            "dry_run": True,
            "path": str(resolved),
            "before": before,
            "would_append_bytes": len(_p3_legacy_text_bytes(content, encoding or "utf-8")),
            "preview": plan.to_preview_dict(),
        })

    with resolved.open("a", encoding=encoding or "utf-8", newline="") as handle:
        handle.write(content)
    return _json({"status": "ok", "operation": "append", "path": str(resolved), "before": before, "after": _stat_payload(resolved)})


def file_search(
    path: str,
    query: str,
    glob: str = "**/*",
    max_results: int = 50,
    case_sensitive: bool = False,
) -> str:
    if not path:
        return _json({"status": "error", "error": "path is required"})
    if not query:
        return _json({"status": "error", "error": "query is required"})
    shape_error = _validate_path_shape(path, operation="filesystem") or _validate_glob_pattern(glob, operation="search")
    if shape_error:
        return shape_error
    pending = _gate_path(
        ActionType.READ,
        path=path,
        tool_name="fs_search",
        action_description=f"Search files under: {path}",
        payload_args={
            "path": path,
            "query": query,
            "glob": glob,
            "max_results": max_results,
            "case_sensitive": case_sensitive,
        },
    )
    if pending:
        return pending

    base = _resolve_path(path)
    if not base.exists():
        return _json({"status": "error", "error": f"Path does not exist: {base}"})

    needle = query if case_sensitive else query.lower()
    limit = _safe_int(max_results, 50, minimum=1, maximum=_MAX_SEARCH_RESULTS)
    glob_pattern = _glob_parts(glob or "**/*")
    matches: list[dict[str, Any]] = []
    with _WalkBudget() as budget:
        candidates: Iterable[Path] = (
            [base]
            if base.is_file()
            else (
                Path(item.entry.path)
                for item in _walk(base, budget, max_depth=_glob_max_depth(glob_pattern))
                if not item.restricted and _glob_matches(item, glob_pattern) and item.entry.is_file()
            )
        )
        for file_path in candidates:
            if len(matches) >= limit or budget.exhausted():
                break
            matches.extend(_file_matches(file_path, needle, case_sensitive=case_sensitive, limit=limit - len(matches)))
    return _truncated_result(
        {"status": "ok", "operation": "search", "path": str(base), "query": query, "count": len(matches), "matches": matches},
        budget.exhausted_reason or ("limit" if len(matches) >= limit else ""),
    )


def _file_matches(file_path: Path, needle: str, *, case_sensitive: bool, limit: int) -> list[dict[str, Any]]:
    """Return up to ``limit`` line matches from the first read slice of one file."""
    try:
        with file_path.open("rb") as handle:
            text = handle.read(_DEFAULT_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return []
    if needle not in (text if case_sensitive else text.lower()):
        return []
    matches: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        column = (line if case_sensitive else line.lower()).find(needle)
        if column < 0:
            continue
        matches.append({"path": str(file_path), "line": line_number, "column": column + 1, "preview": line.strip()[:300]})
        if len(matches) >= limit:
            break
    return matches


def file_exists(path: str) -> str:
    if not path:
        return _json({"status": "error", "error": "path is required"})
    shape_error = _validate_path_shape(path, operation="filesystem")
    if shape_error:
        return shape_error
    pending = _gate_path(
        ActionType.READ,
        path=path,
        tool_name="fs_stat",
        action_description=f"Check file path: {path}",
        payload_args={"path": path},
    )
    if pending:
        return pending
    resolved = _resolve_path(path)
    stat = _stat_payload(resolved) if resolved.exists() else _missing_stat_payload(resolved)
    return _json({"status": "ok", "operation": "stat", "path": str(resolved), "exists": stat["exists"], "stat": stat})


def file_glob(path: str, pattern: str = "**/*", limit: int = 200) -> str:
    return file_list(path, pattern=pattern or "**/*", recursive=True, limit=limit)


def file_tree(path: str, depth: int = 2, limit: int = 200) -> str:
    if not path:
        return _json({"status": "error", "error": "path is required"})
    shape_error = _validate_path_shape(path, operation="filesystem")
    if shape_error:
        return shape_error
    pending = _gate_path(
        ActionType.READ,
        path=path,
        tool_name="fs_tree",
        action_description=f"Read file tree under: {path}",
        payload_args={"path": path, "depth": depth, "limit": limit},
    )
    if pending:
        return pending
    base = _resolve_path(path)
    if not base.exists():
        return _json({"status": "error", "error": f"Path does not exist: {base}"})
    if not base.is_dir():
        return _json({"status": "error", "error": f"Not a directory: {base}"})
    max_depth = _safe_int(depth, 2, minimum=0, maximum=8)
    max_items = _safe_int(limit, 200, minimum=1, maximum=_MAX_LIST_LIMIT)
    entries, truncated_reason = _walk_entries(base, max_items=max_items, max_depth=max_depth)
    return _truncated_result(
        {"status": "ok", "operation": "tree", "path": str(base), "count": len(entries), "entries": entries},
        truncated_reason,
    )


def file_hash(path: str, algorithm: str = "sha256") -> str:
    if not path:
        return _json({"status": "error", "error": "path is required"})
    shape_error = _validate_path_shape(path, operation="filesystem")
    if shape_error:
        return shape_error
    pending = _gate_path(
        ActionType.READ,
        path=path,
        tool_name="fs_hash",
        action_description=f"Hash file: {path}",
        payload_args={"path": path, "algorithm": algorithm},
    )
    if pending:
        return pending
    resolved = _resolve_path(path)
    if not resolved.exists():
        return _json({"status": "error", "error": f"File does not exist: {resolved}"})
    if not resolved.is_file():
        return _json({"status": "error", "error": f"Not a file: {resolved}"})
    normalized = (algorithm or "sha256").lower()
    if normalized not in hashlib.algorithms_available:
        return _json({"status": "error", "error": f"Unsupported hash algorithm: {algorithm}"})
    digest = hashlib.new(normalized)
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return _json({"status": "ok", "operation": "hash", "path": str(resolved), "algorithm": normalized, "hash": digest.hexdigest(), "stat": _stat_payload(resolved)})


def file_patch(
    path: str,
    old: str,
    new: str,
    count: int = 1,
    encoding: str = "utf-8",
    dry_run: bool = False,
    *,
    _bypass_gate: bool = False,
) -> str:
    if not path:
        return _json({"status": "error", "error": "path is required"})
    if old == "":
        return _json({"status": "error", "error": "old text is required"})
    shape_error = _validate_path_shape(path, operation="patch")
    if shape_error:
        return shape_error
    budget_error = _validate_write_budget(new, encoding)
    if budget_error:
        return budget_error
    payload_args = {"path": path, "old": old, "new": new, "count": count, "encoding": encoding, "dry_run": dry_run}
    if not _bypass_gate:
        pending = _gate_path(
            ActionType.MUTATE,
            path=path,
            tool_name="fs_patch",
            action_description=f"Patch file: {path}",
            payload_args=payload_args,
        )
        if pending:
            return pending
    revalidate = _revalidate_mutation_target(path)
    if revalidate:
        return revalidate
    resolved = _resolve_path(path)
    if not resolved.exists():
        return _json({"status": "error", "error": f"File does not exist: {resolved}"})
    if not resolved.is_file():
        return _json({"status": "error", "error": f"Not a file: {resolved}"})
    before = _stat_payload(resolved)
    if dry_run:
        try:
            plan, _before_state, replacements, preview_error = _p3_dry_run_text_plan(
                path=resolved,
                operation="patch",
                tool_name="fs_patch",
                action_description=f"Patch file: {resolved}",
                payload_args=payload_args,
                request={**payload_args, "path": str(resolved)},
                encoding=encoding or "utf-8",
                patch_old=old,
                patch_new=new,
                patch_count=count,
            )
        except Exception:
            return _error("preview_unavailable", "A patch preview could not be built safely.")
        if preview_error:
            return _error(preview_error, "The requested text was not found in the file.")
        return _json({
            "status": "ok",
            "operation": "patch",
            "dry_run": True,
            "path": str(resolved),
            **({"replacements": replacements} if replacements is not None else {}),
            "before": before,
            "preview": plan.to_preview_dict(),
        })
    content = resolved.read_text(encoding=encoding or "utf-8")
    max_count = _safe_int(count, 1, minimum=1, maximum=100000)
    occurrences = content.count(old)
    if occurrences == 0:
        return _json({"status": "error", "error": "old text was not found", "reason_code": "patch_text_not_found"})
    updated = content.replace(old, new, max_count)
    replaced = min(occurrences, max_count)
    with resolved.open("w", encoding=encoding or "utf-8", newline="") as handle:
        handle.write(updated)
    return _json({"status": "ok", "operation": "patch", "path": str(resolved), "replacements": replaced, "before": before, "after": _stat_payload(resolved)})


def fs_mkdir(
    path: str,
    parents: bool = True,
    exist_ok: bool = True,
    dry_run: bool = False,
    *,
    _bypass_gate: bool = False,
) -> str:
    if not path:
        return _json({"status": "error", "error": "path is required"})
    shape_error = _validate_path_shape(path, operation="mkdir")
    if shape_error:
        return shape_error
    payload_args = {"path": path, "parents": parents, "exist_ok": exist_ok, "dry_run": dry_run}
    if not _bypass_gate:
        pending = _gate_path(
            ActionType.MUTATE,
            path=path,
            tool_name="fs_mkdir",
            action_description=f"Create directory: {path}",
            payload_args=payload_args,
        )
        if pending:
            return pending
    resolved = _resolve_path(path)
    before = _stat_payload(resolved) if resolved.exists() else _missing_stat_payload(resolved)
    if resolved.exists() and not resolved.is_dir():
        return _json({"status": "error", "operation": "mkdir", "error": f"Path exists and is not a directory: {resolved}"})
    if resolved.exists() and not exist_ok:
        return _json({"status": "error", "operation": "mkdir", "error": f"Directory already exists: {resolved}", "reason_code": "directory_exists"})
    if dry_run:
        return _json({"status": "ok", "operation": "mkdir", "dry_run": True, "path": str(resolved), "before": before})
    resolved.mkdir(parents=parents, exist_ok=exist_ok)
    return _json({"status": "ok", "operation": "mkdir", "path": str(resolved), "before": before, "after": _stat_payload(resolved)})


def _gate_transfer_paths(
    *,
    operation: str,
    source: str,
    destination: str,
    source_action: ActionType,
    destination_action: ActionType,
    payload_args: dict[str, Any],
) -> str | None:
    source_pending = _gate_path(
        source_action,
        path=source,
        tool_name=f"fs_{operation}",
        action_description=f"{operation.title()} source: {source} -> {destination}",
        payload_args=payload_args,
    )
    if source_pending:
        return source_pending
    destination_pending = _gate_path(
        destination_action,
        path=destination,
        tool_name=f"fs_{operation}",
        action_description=f"{operation.title()} destination: {source} -> {destination}",
        payload_args=payload_args,
    )
    if destination_pending:
        return destination_pending
    return None


def _gate_directory_replacement(
    *,
    operation: str,
    destination: str,
    overwrite: bool,
    payload_args: dict[str, Any],
    bypass_confirmation: bool,
) -> str | None:
    """Replacing an existing directory removes its whole tree, so gate it as a delete."""
    if not overwrite or not _resolve_path(destination).is_dir():
        return None
    if not bypass_confirmation:
        return _gate_path(
            ActionType.DELETE,
            path=destination,
            tool_name=f"fs_{operation}",
            action_description=f"{operation.title()} replaces existing directory: {destination}",
            payload_args=payload_args,
        )
    decision = resolve_permission(ActionType.DELETE, target_path=_policy_target_path(destination))
    if decision.blocked or decision.requires_access_grant:
        return _blocked_result(decision)
    return None


def fs_copy(
    source: str,
    destination: str,
    overwrite: bool = False,
    create_parents: bool = True,
    dry_run: bool = False,
    *,
    _bypass_gate: bool = False,
) -> str:
    if not source or not destination:
        return _json({"status": "error", "error": "source and destination are required"})
    for label, candidate in (("source", source), ("destination", destination)):
        shape_error = _validate_path_shape(candidate, operation=f"copy {label}")
        if shape_error:
            return shape_error
    payload_args = {
        "source": source,
        "destination": destination,
        "overwrite": overwrite,
        "create_parents": create_parents,
        "dry_run": dry_run,
    }
    if not _bypass_gate:
        pending = _gate_transfer_paths(
            operation="copy",
            source=source,
            destination=destination,
            source_action=ActionType.READ,
            destination_action=ActionType.MUTATE,
            payload_args=payload_args,
        )
        if pending:
            return pending
    revalidate = _revalidate_mutation_target(destination)
    if revalidate:
        return revalidate
    src = _resolve_path(source)
    dst = _resolve_path(destination)
    if not src.exists():
        return _json({"status": "error", "operation": "copy", "error": f"Source not found: {src}"})
    if _same_resolved_path(src, dst):
        stat = _stat_payload(src)
        return _json({"status": "ok", "operation": "copy", "source": str(src), "destination": str(dst), "same_path": True, "before": stat, "after": stat})
    budget_error = _validate_recursive_budget(src, operation="copy")
    if budget_error:
        return budget_error
    if dst.exists() and not overwrite:
        return _json({"status": "error", "operation": "copy", "error": f"Destination already exists: {dst}", "reason_code": "destination_exists"})
    before = _stat_payload(dst) if dst.exists() else _missing_stat_payload(dst)
    if dry_run:
        return _json({"status": "ok", "operation": "copy", "dry_run": True, "source": str(src), "destination": str(dst), "before": before})
    if not dst.parent.exists():
        if not create_parents:
            return _json({"status": "error", "operation": "copy", "error": f"Parent directory does not exist: {dst.parent}"})
        dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=overwrite)
    else:
        shutil.copy2(src, dst)
    return _json({"status": "ok", "operation": "copy", "source": str(src), "destination": str(dst), "before": before, "after": _stat_payload(dst)})


def _p3_move_metadata_state(path: Path) -> PathState:
    """Inspect one lexical path without opening its contents or following links."""
    absolute = Path(os.path.abspath(str(path)))
    if absolute.name in {"", ".", ".."}:
        raise FingerprintError("move path does not name a file")

    current = Path(absolute.anchor)

    def directory_identity(directory: Path) -> tuple[int, int]:
        try:
            info = os.lstat(directory)
        except OSError as exc:
            raise FingerprintError("move parent directory is unavailable") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or bool(int(getattr(info, "st_file_attributes", 0)) & 0x0400)
        ):
            raise FingerprintError("move path contains a symlink or reparse point")
        identity = (int(info.st_dev), int(info.st_ino))
        if identity[0] < 0 or identity[1] <= 0:
            raise FingerprintError("move directory identity is unavailable")
        return identity

    parent_identity = directory_identity(current)
    for part in absolute.parent.parts[1:]:
        current = current / part
        parent_identity = directory_identity(current)

    try:
        info = os.lstat(absolute)
    except FileNotFoundError:
        if directory_identity(absolute.parent) != parent_identity:
            raise FingerprintError("move parent changed while checking the path")
        return PathState.metadata(
            str(absolute), exists=False, kind="absent", size=0,
            parent_identity=parent_identity,
        )
    except OSError as exc:
        raise FingerprintError("move path cannot be inspected") from exc

    if stat.S_ISLNK(info.st_mode) or bool(int(getattr(info, "st_file_attributes", 0)) & 0x0400):
        raise FingerprintError("move target is a symlink or reparse point")
    identity = (int(info.st_dev), int(info.st_ino))
    if identity[0] < 0 or identity[1] <= 0:
        raise FingerprintError("move target identity is unavailable")
    if stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file" if int(getattr(info, "st_nlink", 1)) == 1 else "hardlinked_file"
    else:
        kind = "other"

    if directory_identity(absolute.parent) != parent_identity:
        raise FingerprintError("move parent changed while checking the path")
    return PathState.metadata(
        str(absolute),
        exists=True,
        kind=kind,
        size=max(0, int(info.st_size)),
        identity=identity,
        modified_ns=int(getattr(info, "st_mtime_ns", 0)),
        created_ns=int(getattr(info, "st_ctime_ns", 0)),
        parent_identity=parent_identity,
    )


def _p3_move_parent_is_missing(path: Path) -> bool:
    """Return whether a lexical parent component is absent without following links."""
    absolute = Path(os.path.abspath(str(path)))
    current = Path(absolute.anchor)
    for part in absolute.parent.parts[1:]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or bool(int(getattr(info, "st_file_attributes", 0)) & 0x0400)
        ):
            return False
    return False


def _p3_move_unsupported_preview(
    source: PathState,
    destination: PathState | None,
    reason: str,
) -> dict[str, object]:
    """Describe an opaque legacy move without content or private identities."""
    return {
        "version": 1,
        "operation": "move",
        "files": [
            {
                "path": source.path,
                "role": "source",
                "before": source.public_dict(),
                "after": {"exists": False, "kind": "absent", "size": 0},
            },
            {
                "path": destination.path if destination is not None else "",
                "role": "destination",
                "before": destination.public_dict() if destination is not None else {"exists": False, "kind": "unknown", "size": None},
                "after": {"exists": None, "kind": "unsupported", "size": None},
            },
        ],
        "undo_capability": "unavailable",
        "preview": {"kind": "summary", "reason": reason},
    }


def _p3_move_attach_preview(
    output: str,
    preview: dict[str, object] | None,
) -> str:
    if preview is None:
        return output
    try:
        payload = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return output
    if not isinstance(payload, dict) or payload.get("status") not in {
        "pending_approval", "pending_access_grant", "ok"
    }:
        return output
    payload["preview"] = preview
    return _json(payload)


def _p3_move_stat_payload(state: PathState) -> dict[str, Any]:
    if not state.exists:
        return {
            "path": state.path,
            "exists": False,
            "type": "missing",
            "is_file": False,
            "is_dir": False,
            "size": 0,
            "modified_at": "",
            "created_at": "",
        }
    return {
        "path": state.path,
        "exists": True,
        "type": "directory" if state.kind == "directory" else "file" if state.kind == "file" else "other",
        "is_file": state.kind == "file",
        "is_dir": state.kind == "directory",
        "size": state.size or 0,
        "modified_at": _iso_from_timestamp(state.modified_ns / 1_000_000_000) if state.modified_ns is not None else "",
        "created_at": _iso_from_timestamp(state.created_ns / 1_000_000_000) if state.created_ns is not None else "",
    }


def _p3_move_no_replace(
    source: Path,
    destination: Path,
    *,
    expected_source: PathState,
    expected_destination: PathState,
) -> None:
    """Move one regular file without replacing an unexpected destination."""
    current_source = PathState.from_fingerprint(fingerprint_path(source))
    current_destination = PathState.from_fingerprint(fingerprint_path(destination))
    if current_source != expected_source or current_destination != expected_destination:
        raise FingerprintError("move operands changed at the effect boundary")

    if expected_destination.exists:
        os.replace(source, destination)
        return
    if os.name == "nt":
        # Windows rename fails atomically when the destination already exists.
        os.rename(source, destination)
        return

    # POSIX rename replaces an existing leaf. An exclusive hard link followed
    # by source unlink supplies no-replace behavior for a regular same-volume file.
    os.link(source, destination, follow_symlinks=False)
    source_info = os.lstat(source)
    destination_info = os.lstat(destination)
    expected_identity = expected_source.identity
    if (
        expected_identity is None
        or not stat.S_ISREG(source_info.st_mode)
        or not stat.S_ISREG(destination_info.st_mode)
        or (int(source_info.st_dev), int(source_info.st_ino)) != expected_identity
        or (int(destination_info.st_dev), int(destination_info.st_ino)) != expected_identity
        or int(getattr(source_info, "st_nlink", 0)) != 2
        or int(getattr(destination_info, "st_nlink", 0)) != 2
    ):
        # Preserve both names and let verification classify the mixed state.
        raise FingerprintError("move source identity changed while linking")
    # If unlink fails, leave both names in place. The verifier reports the
    # mixed state instead of deleting a destination that may be the last copy.
    os.unlink(source)


def fs_move(
    source: str,
    destination: str,
    overwrite: bool = False,
    create_parents: bool = True,
    dry_run: bool = False,
    *,
    _bypass_gate: bool = False,
) -> str:
    if not source or not destination:
        return _json({"status": "error", "error": "source and destination are required"})
    for label, candidate in (("source", source), ("destination", destination)):
        shape_error = _validate_path_shape(candidate, operation=f"move {label}")
        if shape_error:
            return shape_error
    payload_args = {
        "source": source,
        "destination": destination,
        "overwrite": overwrite,
        "create_parents": create_parents,
        "dry_run": dry_run,
    }
    execution = _native_file_execution()
    fallback_preview: dict[str, object] | None = None
    if execution is not None or dry_run:
        source_policy_path = _policy_target_path(source)
        destination_policy_path = _policy_target_path(destination)
        source_lexical = Path(os.path.abspath(source_policy_path))
        destination_lexical = Path(os.path.abspath(destination_policy_path))
        action_targets = [
            (ActionType.MUTATE, source_policy_path),
            (ActionType.MUTATE, destination_policy_path),
        ]
        description = f"Move file: {source_lexical} -> {destination_lexical}"
        pending = _p3_initial_mutation_access(
            execution=execution,
            action_targets=action_targets,
            tool_name="fs_move",
            action_description=description,
            payload_args=payload_args,
        )
        if pending:
            return pending

        try:
            source_metadata = _p3_move_metadata_state(source_lexical)
        except FingerprintError:
            if execution is not None:
                execution.native_file_no_effect("unsafe_target")
            return _error(
                "unsafe_move_path",
                "The move source or one of its parent directories cannot be inspected safely.",
            )
        if not source_metadata.exists:
            if execution is not None:
                execution.native_file_no_effect("operation_failed")
            return _json({"status": "error", "operation": "move", "error": f"Source not found: {source_lexical}"})

        if os.path.normcase(str(source_lexical)) == os.path.normcase(str(destination_lexical)):
            if execution is not None:
                execution.native_file_no_effect("operation_failed")
            stat_payload = _p3_move_stat_payload(source_metadata)
            return _json({
                "status": "ok",
                "operation": "move",
                "source": str(source_lexical),
                "destination": str(destination_lexical),
                "same_path": True,
                "before_source": stat_payload,
                "before_destination": stat_payload,
                "after": stat_payload,
                **({"dry_run": True} if dry_run else {}),
            })

        destination_metadata: PathState | None
        try:
            destination_metadata = _p3_move_metadata_state(destination_lexical)
        except FingerprintError:
            if not _p3_move_parent_is_missing(destination_lexical):
                if execution is not None:
                    execution.native_file_no_effect("unsafe_target")
                return _error(
                    "unsafe_move_path",
                    "The move destination or one of its parent directories cannot be inspected safely.",
                )
            destination_metadata = None

        if destination_metadata is not None and destination_metadata.exists and not overwrite:
            if execution is not None:
                execution.native_file_no_effect("operation_failed")
            return _json({
                "status": "error",
                "operation": "move",
                "error": f"Destination already exists: {destination_lexical}",
                "reason_code": "destination_exists",
            })

        unsupported_reason = ""
        if source_metadata.kind != "file":
            unsupported_reason = "regular_file_move_required"
        elif destination_metadata is None:
            unsupported_reason = "destination_parent_unavailable"
        elif destination_metadata.exists and destination_metadata.kind != "file":
            unsupported_reason = "regular_file_move_required"
        elif destination_metadata.parent_identity is None or source_metadata.identity is None:
            unsupported_reason = "file_identity_unavailable"
        elif source_metadata.identity[0] != destination_metadata.parent_identity[0]:
            unsupported_reason = "cross_volume_move_unsupported"

        if unsupported_reason:
            fallback_preview = _p3_move_unsupported_preview(
                source_metadata,
                destination_metadata,
                unsupported_reason,
            )
        else:
            read_authorized = False
            use_native_plan = True
            if execution is not None and not dry_run:
                eligible, read_gate_result = _p3_durable_eligibility(
                    execution=execution,
                    action_targets=action_targets,
                    read_paths=[source_lexical, destination_lexical],
                    tool_name="fs_move",
                    action_description=description,
                    payload_args=payload_args,
                )
                if read_gate_result:
                    return read_gate_result
                if not eligible:
                    # Session and one-time grants retain the opaque P2 path.
                    use_native_plan = False
            if dry_run and use_native_plan:
                read_authorized, read_gate_result = _p3_read_access(
                    [source_lexical, destination_lexical],
                    operation="move",
                    tool_name="fs_move",
                    action_description=description,
                    payload_args=payload_args,
                    dry_run=True,
                    execution=execution,
                )
                if read_gate_result:
                    return read_gate_result
            elif execution is not None and use_native_plan:
                read_authorized = True

            if use_native_plan:
                source_fingerprint = None
                destination_fingerprint = None
                if read_authorized:
                    try:
                        source_fingerprint = fingerprint_path(source_lexical)
                        destination_fingerprint = fingerprint_path(
                            destination_lexical,
                            include_bytes=bool(destination_metadata.exists),
                        )
                        source_before = PathState.from_fingerprint(source_fingerprint)
                        destination_before = PathState.from_fingerprint(destination_fingerprint)
                    except FingerprintError as exc:
                        if dry_run:
                            source_before = source_metadata
                            destination_before = destination_metadata
                            read_authorized = False
                            fallback_preview = _p3_move_unsupported_preview(
                                source_metadata,
                                destination_metadata,
                                "bounded_fingerprint_unavailable",
                            )
                        elif "exceeds the fingerprint size limit" in str(exc):
                            use_native_plan = False
                            fallback_preview = _p3_move_unsupported_preview(
                                source_metadata,
                                destination_metadata,
                                "bounded_fingerprint_unavailable",
                            )
                        else:
                            if execution is not None:
                                execution.native_file_no_effect("stale_precondition")
                            return _error(
                                "precondition_unavailable",
                                "The move operands changed or could not be fingerprinted safely; no file change was made.",
                            )
                else:
                    source_before = source_metadata
                    destination_before = destination_metadata

                if use_native_plan:
                    if source_before.identity != source_metadata.identity or (
                        source_before.size != source_metadata.size
                        or source_before.parent_identity != source_metadata.parent_identity
                    ):
                        if execution is not None:
                            execution.native_file_no_effect("stale_precondition")
                        return _error(
                            "stale_precondition",
                            "The move source changed while its preview was being prepared.",
                        )
                    if destination_before.exists != destination_metadata.exists or (
                        destination_before.identity != destination_metadata.identity
                        or destination_before.parent_identity != destination_metadata.parent_identity
                        or destination_before.size != destination_metadata.size
                    ):
                        if execution is not None:
                            execution.native_file_no_effect("stale_precondition")
                        return _error(
                            "stale_precondition",
                            "The move destination changed while its preview was being prepared.",
                        )

                    source_after = PathState.metadata(
                        str(source_lexical),
                        exists=False,
                        kind="absent",
                        size=0,
                        parent_identity=source_before.parent_identity,
                    )
                    destination_after = PathState(
                        path=str(destination_lexical),
                        exists=True,
                        kind="file",
                        size=source_before.size,
                        sha256=source_before.sha256,
                        identity=source_before.identity,
                        modified_ns=source_before.modified_ns,
                        created_ns=source_before.created_ns,
                        parent_identity=destination_before.parent_identity,
                    )
                    plan = build_plan(
                        "move",
                        {
                            "source": source,
                            "destination": destination,
                            "overwrite": bool(overwrite),
                            "create_parents": bool(create_parents),
                            "dry_run": bool(dry_run),
                        },
                        [
                            PlanOperand(
                                str(source_lexical), "source", source_before, source_after,
                                identity_strategy="absent",
                            ),
                            PlanOperand(
                                str(destination_lexical), "destination", destination_before, destination_after,
                                identity_strategy="replaced",
                            ),
                        ],
                        read_authorized=read_authorized,
                        undo_capability="available" if read_authorized else "unavailable",
                    )
                    pending = _p3_gate_plan(
                        execution=execution,
                        plan=plan,
                        action_targets=action_targets,
                        tool_name="fs_move",
                        action_description=description,
                        payload_args=payload_args,
                        bypass_gate=_bypass_gate,
                    )
                    if pending:
                        return pending

                    if dry_run:
                        if execution is not None:
                            execution.native_file_no_effect("operation_failed")
                        return _json({
                            "status": "ok",
                            "operation": "move",
                            "dry_run": True,
                            "source": str(source_lexical),
                            "destination": str(destination_lexical),
                            "before_source": _p3_move_stat_payload(source_before),
                            "before_destination": _p3_move_stat_payload(destination_before),
                            "preview": plan.to_preview_dict(),
                        })

                    if execution is None:
                        return _error(
                            "durable_context_required",
                            "A durable operation is required for this native move.",
                        )
                    before_bytes_by_path: dict[str, bytes] = {}
                    if destination_before.exists:
                        before_image = destination_fingerprint.data if destination_fingerprint is not None else None
                        if before_image is None:
                            execution.native_file_no_effect("operation_failed")
                            return _error(
                                "snapshot_unavailable",
                                "The destination before-image could not be captured; no file change was made.",
                            )
                        before_bytes_by_path[str(destination_lexical)] = before_image
                    store, capture_error = _p3_capture_and_promote(
                        execution=execution,
                        plan=plan,
                        before_bytes_by_path=before_bytes_by_path,
                        authorized_roots=[str(source_lexical), str(destination_lexical)],
                    )
                    if capture_error:
                        return capture_error
                    boundary_error = _p3_recheck_effect_boundary(
                        execution=execution,
                        plan=plan,
                        action_targets=action_targets,
                        tool_name="fs_move",
                        action_description=description,
                    )
                    if boundary_error:
                        return boundary_error
                    try:
                        _p3_move_no_replace(
                            source_lexical,
                            destination_lexical,
                            expected_source=source_before,
                            expected_destination=destination_before,
                        )
                    except Exception:
                        try:
                            completed, evidence = _p3_verify_effect(execution, store)
                        except Exception:
                            execution.native_file_verification_unavailable()
                            return _error(
                                "move_verification_failed",
                                "The move result could not be verified; review the file state before continuing.",
                            )
                        if not completed:
                            reason = str(evidence.get("verification_result") or "unknown")
                            return _error(
                                "move_failed" if reason == "unchanged" else "move_verification_failed",
                                "The move did not complete safely; inspect the source and destination before retrying.",
                            )
                    else:
                        try:
                            completed, evidence = _p3_verify_effect(execution, store)
                        except Exception:
                            execution.native_file_verification_unavailable()
                            return _error(
                                "move_verification_failed",
                                "The move completed but its result could not be verified.",
                            )
                        if not completed:
                            return _error(
                                "move_verification_failed",
                                "The move completed but its result did not match the preview.",
                            )
                    return _json({
                        "status": "ok",
                        "operation": "move",
                        "source": str(source_lexical),
                        "destination": str(destination_lexical),
                        "before_source": _p3_move_stat_payload(source_before),
                        "before_destination": _p3_move_stat_payload(destination_before),
                        "after": _p3_move_stat_payload(destination_after),
                        "verification": evidence,
                        "preview": plan.to_preview_dict(),
                    })

    if not _bypass_gate:
        pending = _gate_transfer_paths(
            operation="move",
            source=source,
            destination=destination,
            source_action=ActionType.MUTATE,
            destination_action=ActionType.MUTATE,
            payload_args=payload_args,
        )
        if pending:
            return _p3_move_attach_preview(pending, fallback_preview)
    replacement = _gate_directory_replacement(
        operation="move",
        destination=destination,
        overwrite=overwrite,
        payload_args=payload_args,
        bypass_confirmation=_bypass_gate,
    )
    if replacement:
        return _p3_move_attach_preview(replacement, fallback_preview)
    revalidate = _revalidate_mutation_target(source) or _revalidate_mutation_target(destination)
    if revalidate:
        return revalidate
    src = _resolve_path(source)
    dst = _resolve_path(destination)
    if not src.exists():
        return _json({"status": "error", "operation": "move", "error": f"Source not found: {src}"})
    if _same_resolved_path(src, dst):
        stat = _stat_payload(src)
        return _json({"status": "ok", "operation": "move", "source": str(src), "destination": str(dst), "same_path": True, "before_source": stat, "before_destination": stat, "after": stat})
    budget_error = _validate_recursive_budget(src, operation="move")
    if budget_error:
        return budget_error
    if dst.exists() and not overwrite:
        return _json({"status": "error", "operation": "move", "error": f"Destination already exists: {dst}", "reason_code": "destination_exists"})
    before_source = _stat_payload(src)
    before_destination = _stat_payload(dst) if dst.exists() else _missing_stat_payload(dst)
    if dry_run:
        if execution is not None:
            execution.native_file_no_effect("operation_failed")
        return _p3_move_attach_preview(_json({"status": "ok", "operation": "move", "dry_run": True, "source": str(src), "destination": str(dst), "before_source": before_source, "before_destination": before_destination}), fallback_preview)
    if not dst.parent.exists():
        if not create_parents:
            return _json({"status": "error", "operation": "move", "error": f"Parent directory does not exist: {dst.parent}"})
        dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and overwrite:
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    shutil.move(str(src), str(dst))
    return _p3_move_attach_preview(_json({"status": "ok", "operation": "move", "source": str(src), "destination": str(dst), "before_source": before_source, "before_destination": before_destination, "after": _stat_payload(dst)}), fallback_preview)


def fs_rename(path: str, new_name: str, overwrite: bool = False, dry_run: bool = False, *, _bypass_gate: bool = False) -> str:
    if not path or not new_name:
        return _json({"status": "error", "error": "path and new_name are required"})
    shape_error = _validate_path_shape(path, operation="rename")
    if shape_error:
        return shape_error
    if Path(new_name).name != new_name or _has_path_traversal(new_name):
        return _error("invalid_target_name", "new_name must be a single path segment.")
    source_label = path
    destination_label = str(Path(path).with_name(new_name))
    payload_args = {"path": path, "new_name": new_name, "overwrite": overwrite, "dry_run": dry_run}
    if not _bypass_gate:
        pending = _gate_transfer_paths(
            operation="rename",
            source=source_label,
            destination=destination_label,
            source_action=ActionType.MUTATE,
            destination_action=ActionType.MUTATE,
            payload_args=payload_args,
        )
        if pending:
            return pending
    replacement = _gate_directory_replacement(
        operation="rename",
        destination=destination_label,
        overwrite=overwrite,
        payload_args=payload_args,
        bypass_confirmation=_bypass_gate,
    )
    if replacement:
        return replacement
    revalidate = _revalidate_mutation_target(path)
    if revalidate:
        return revalidate
    source = _resolve_path(path)
    destination = source.with_name(new_name)
    if not source.exists():
        return _json({"status": "error", "operation": "rename", "error": f"Path not found: {source}"})
    if _same_resolved_path(source, destination):
        stat = _stat_payload(source)
        return _json({"status": "ok", "operation": "rename", "source": str(source), "destination": str(destination), "new_path": str(destination), "same_path": True, "before_source": stat, "before_destination": stat, "after": stat})
    if destination.exists() and not overwrite:
        return _json({"status": "error", "operation": "rename", "error": f"Destination already exists: {destination}", "reason_code": "destination_exists"})
    before_source = _stat_payload(source)
    before_destination = _stat_payload(destination) if destination.exists() else _missing_stat_payload(destination)
    if dry_run:
        return _json({"status": "ok", "operation": "rename", "dry_run": True, "source": str(source), "destination": str(destination), "before_source": before_source, "before_destination": before_destination})
    if destination.exists() and overwrite:
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    source.rename(destination)
    return _json({"status": "ok", "operation": "rename", "source": str(source), "destination": str(destination), "new_path": str(destination), "before_source": before_source, "before_destination": before_destination, "after": _stat_payload(destination)})


def _p3_delete_regular_file(path: Path, expected_before: PathState) -> None:
    """Delete only the authorized regular file after a final stable-policy check."""
    for action in (ActionType.READ, ActionType.DELETE):
        decision = resolve_permission(
            action,
            target_path=str(path),
            allow_session_grants=False,
            consume_once_grants=False,
        )
        if (
            decision.blocked
            or decision.requires_access_grant
            or not decision.allowed
            or (action == ActionType.READ and decision.requires_confirmation)
        ):
            raise PermissionError("stable READ and DELETE permission changed before deletion")

    current = PathState.from_fingerprint(fingerprint_path(path, include_bytes=True))
    if current != expected_before:
        raise FingerprintError("target preconditions changed before delete")

    if os.name == "nt":
        # Delete by the opened file handle so a path replacement between the
        # final fingerprint and the effect cannot redirect the deletion.
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        class _ByHandleInfo(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("creation", _FileTime),
                ("access", _FileTime),
                ("write", _FileTime),
                ("volume", wintypes.DWORD),
                ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD),
                ("links", wintypes.DWORD),
                ("index_high", wintypes.DWORD),
                ("index_low", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_ByHandleInfo),
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        ]
        kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        invalid_handle = ctypes.c_void_p(-1).value
        parent_handle = kernel32.CreateFileW(
            str(path.parent),
            0x00000080,  # FILE_READ_ATTRIBUTES
            0x00000001 | 0x00000002,  # share read/write; deny parent rename/delete
            None,
            3,  # OPEN_EXISTING
            0x00200000 | 0x02000000,  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
            None,
        )
        if parent_handle == invalid_handle:
            raise OSError(ctypes.get_last_error(), "could not pin the target parent directory")
        parent_info = _ByHandleInfo()
        if not kernel32.GetFileInformationByHandle(parent_handle, ctypes.byref(parent_info)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(parent_handle)
            raise OSError(error, "could not inspect the target parent directory")
        parent_identity = (
            int(parent_info.volume),
            (int(parent_info.index_high) << 32) | int(parent_info.index_low),
        )
        if (
            not parent_info.attributes & 0x00000010
            or parent_info.attributes & 0x00000400
            or parent_identity != expected_before.parent_identity
        ):
            kernel32.CloseHandle(parent_handle)
            raise FingerprintError("target parent changed before delete")

        handle = kernel32.CreateFileW(
            str(path),
            0x80000000 | 0x00010000 | 0x00000080,  # GENERIC_READ | DELETE | READ_ATTRIBUTES
            0x00000001,  # share read only; deny concurrent writes and rename/delete
            None,
            3,  # OPEN_EXISTING
            0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
            None,
        )
        if handle == invalid_handle:
            kernel32.CloseHandle(parent_handle)
            raise OSError(ctypes.get_last_error(), "could not open target for identity-bound deletion")
        descriptor = -1

        def filetime_ns(value: _FileTime) -> int:
            ticks = (int(value.high) << 32) | int(value.low)
            return (ticks - 116444736000000000) * 100

        def read_info() -> _ByHandleInfo:
            info = _ByHandleInfo()
            raw_handle = msvcrt.get_osfhandle(descriptor) if descriptor >= 0 else handle
            if not kernel32.GetFileInformationByHandle(raw_handle, ctypes.byref(info)):
                raise OSError(ctypes.get_last_error(), "could not inspect target handle")
            return info

        def check_info(info: _ByHandleInfo) -> None:
            identity = (
                int(info.volume),
                (int(info.index_high) << 32) | int(info.index_low),
            )
            size = (int(info.size_high) << 32) | int(info.size_low)
            if (
                info.attributes & (0x00000010 | 0x00000400)
                or int(info.links) != 1
                or identity != expected_before.identity
                or size != expected_before.size
                or filetime_ns(info.write) != expected_before.modified_ns
                or filetime_ns(info.creation) != expected_before.created_ns
            ):
                raise FingerprintError("target handle no longer matches the planned file")

        try:
            descriptor = msvcrt.open_osfhandle(
                int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
            )
            handle = None
            initial_info = read_info()
            check_info(initial_info)
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                digest.update(chunk)
            finished_info = read_info()
            check_info(finished_info)
            if total != expected_before.size or digest.hexdigest() != expected_before.sha256:
                raise FingerprintError("target bytes changed before delete")
            disposition = ctypes.c_ubyte(1)
            if not kernel32.SetFileInformationByHandle(
                msvcrt.get_osfhandle(descriptor),
                4,  # FileDispositionInfo
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            ):
                raise OSError(ctypes.get_last_error(), "could not delete the verified file handle")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            elif handle not in (None, invalid_handle):
                kernel32.CloseHandle(handle)
            kernel32.CloseHandle(parent_handle)
        return

    # POSIX has no conditional unlink-by-inode primitive. Pin the parent and
    # recheck the leaf through that directory handle immediately before unlink.
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_fd = os.open(path.parent, parent_flags)
    try:
        parent_stat = os.fstat(parent_fd)
        if (int(parent_stat.st_dev), int(parent_stat.st_ino)) != expected_before.parent_identity:
            raise FingerprintError("parent directory changed before delete")
        leaf_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(leaf_stat.st_mode)
            or int(getattr(leaf_stat, "st_nlink", 1)) != 1
            or (int(leaf_stat.st_dev), int(leaf_stat.st_ino)) != expected_before.identity
            or int(leaf_stat.st_size) != expected_before.size
            or int(getattr(leaf_stat, "st_mtime_ns", 0)) != expected_before.modified_ns
        ):
            raise FingerprintError("target changed before delete")
        if PathState.from_fingerprint(fingerprint_path(path, include_bytes=True)) != expected_before:
            raise FingerprintError("target path changed before delete")
        os.unlink(path.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _p3_delete_dry_run_is_stable(path: Path) -> bool:
    """Avoid reading file bytes when P3 eligibility depends on a transient grant."""
    for action in (ActionType.READ, ActionType.DELETE):
        decision = resolve_permission(
            action,
            target_path=str(path),
            allow_session_grants=False,
            consume_once_grants=False,
        )
        if decision.blocked or decision.requires_access_grant or not decision.allowed:
            return False
    return True


def fs_delete(path: str, recursive: bool = True, dry_run: bool = False, *, _bypass_gate: bool = False) -> str:
    if not path:
        return _json({"status": "error", "error": "path is required"})
    shape_error = _validate_path_shape(path, operation="delete")
    if shape_error:
        return shape_error
    payload_args = {"path": path, "recursive": recursive, "dry_run": dry_run}
    execution = _native_file_execution()
    resolved = _resolve_path(path)
    use_preview_plan = execution is not None or dry_run
    candidate_before = None
    if use_preview_plan:
        try:
            candidate_before = _p3_metadata_state(resolved)
        except (FingerprintError, OSError, ValueError):
            candidate_before = None
        # Directories and unsupported leaf types retain the established opaque
        # path. The recursive flag does not change regular-file semantics.
        if candidate_before is None or not candidate_before.exists:
            use_preview_plan = False

    if use_preview_plan:
        action_targets = [(ActionType.DELETE, str(resolved))]
        description = f"Delete file: {resolved}"
        if execution is not None and not dry_run:
            eligible, gate_result = _p3_durable_eligibility(
                execution=execution,
                action_targets=action_targets,
                read_paths=[resolved],
                tool_name="fs_delete",
                action_description=description,
                payload_args=payload_args,
            )
            if gate_result:
                return gate_result
            if not eligible:
                use_preview_plan = False
        elif dry_run and not _p3_delete_dry_run_is_stable(resolved):
            use_preview_plan = False

    if use_preview_plan:
        if not dry_run and execution is None:
            use_preview_plan = False

    if use_preview_plan:
        pending = _p3_initial_mutation_access(
            execution=execution,
            action_targets=action_targets,
            tool_name="fs_delete",
            action_description=description,
            payload_args=payload_args,
        )
        if pending:
            return pending
        revalidate = _revalidate_mutation_target(path, ActionType.DELETE)
        if revalidate:
            if execution is not None:
                execution.native_file_no_effect("operation_failed")
            return revalidate
        if not resolved.parent.is_dir():
            if execution is not None:
                execution.native_file_no_effect("operation_failed")
            return _error(
                "parent_directory_unavailable",
                "A verifiable file delete requires an existing parent directory; no file change was made.",
            )

        read_authorized, read_result = _p3_read_access(
            [resolved],
            operation="delete",
            tool_name="fs_delete",
            action_description=description,
            payload_args=payload_args,
            dry_run=dry_run,
            execution=execution,
        )
        if read_result:
            try:
                read_status = str(json.loads(read_result).get("status") or "")
            except (TypeError, json.JSONDecodeError):
                read_status = ""
            if execution is not None and read_status not in {
                "pending_approval", "pending_access_grant"
            }:
                execution.native_file_no_effect("operation_failed")
            return read_result
        try:
            if read_authorized:
                fingerprint = fingerprint_path(resolved, include_bytes=True)
                before_state = PathState.from_fingerprint(fingerprint)
                before_bytes = fingerprint.data
            else:
                fingerprint = None
                before_state = _p3_metadata_state(resolved)
                before_bytes = None
            if not before_state.exists or before_state.kind != "file":
                raise FingerprintError("delete target is no longer an ordinary regular file")
            after_state = PathState.metadata(
                str(resolved),
                exists=False,
                kind="absent",
                size=0,
                parent_identity=before_state.parent_identity,
            )
            operand = PlanOperand(
                str(resolved), "target", before_state, after_state, identity_strategy="absent"
            )
            plan = build_plan(
                "delete",
                {"path": str(resolved), "recursive": bool(recursive), "dry_run": bool(dry_run)},
                [operand],
                before_bytes=before_bytes,
                read_authorized=read_authorized,
                undo_capability="available",
            )
            before_bytes_by_path = (
                {str(resolved): before_bytes}
                if before_state.exists and before_bytes is not None
                else {}
            )
        except Exception:
            if dry_run and read_authorized:
                try:
                    before_state = _p3_metadata_state(resolved)
                    after_state = PathState.metadata(
                        str(resolved),
                        exists=False,
                        kind="absent",
                        size=0,
                        parent_identity=before_state.parent_identity,
                    )
                    plan = build_plan(
                        "delete",
                        {"path": str(resolved), "recursive": bool(recursive), "dry_run": True},
                        [PlanOperand(str(resolved), "target", before_state, after_state, "absent")],
                        read_authorized=False,
                        undo_capability="available",
                    )
                    before_bytes_by_path = {}
                    read_authorized = False
                except Exception:
                    return _error("preview_unavailable", "A metadata-only delete preview could not be built.")
            else:
                if execution is not None:
                    execution.native_file_no_effect("operation_failed")
                return _error(
                    "precondition_unavailable",
                    "The native delete preconditions could not be captured; no file change was made.",
                )

        pending = _p3_gate_plan(
            execution=execution,
            plan=plan,
            action_targets=action_targets,
            tool_name="fs_delete",
            action_description=description,
            payload_args=payload_args,
            bypass_gate=_bypass_gate,
        )
        if pending:
            return pending

        before = _stat_payload(resolved)
        if dry_run:
            if execution is not None:
                execution.native_file_no_effect("operation_failed")
            return _json({
                "status": "ok",
                "operation": "delete",
                "dry_run": True,
                "path": str(resolved),
                "before": before,
                "preview": plan.to_preview_dict(),
            })

        store, snapshot_error = _p3_capture_and_promote(
            execution=execution,
            plan=plan,
            before_bytes_by_path=before_bytes_by_path,
            authorized_roots=[str(resolved)],
        )
        if snapshot_error:
            return snapshot_error
        boundary_error = _p3_recheck_effect_boundary(
            execution=execution,
            plan=plan,
            action_targets=action_targets,
            tool_name="fs_delete",
            action_description=description,
        )
        if boundary_error:
            return boundary_error
        try:
            _p3_delete_regular_file(resolved, plan.operands[0].before)
            verified, evidence = _p3_verify_effect(execution, store)
        except Exception:
            try:
                _p3_verify_effect(execution, store)
            except Exception:
                execution.native_file_verification_unavailable()
            return _error(
                "native_delete_failed",
                "The native delete did not complete cleanly. Its effect was checked and may need review.",
            )
        if not verified:
            return _error(
                "verification_failed",
                "The native delete could not be verified; inspect the file before retrying.",
                verification_result=str(evidence.get("verification_result") or "unknown"),
            )
        return _json({
            "status": "ok",
            "operation": "delete",
            "path": str(resolved),
            "before": before,
            "after": _missing_stat_payload(resolved),
            "verification_result": "completed",
            "preview": plan.to_preview_dict(),
        })

    if not _bypass_gate:
        pending = _gate_path(
            ActionType.DELETE,
            path=path,
            tool_name="fs_delete",
            action_description=f"Delete path: {path}",
            payload_args=payload_args,
        )
        if pending:
            return pending
    revalidate = _revalidate_mutation_target(path, ActionType.DELETE)
    if revalidate:
        return revalidate
    if not resolved.exists():
        return _json({"status": "error", "operation": "delete", "error": f"Path not found: {resolved}"})
    budget_error = _validate_recursive_budget(resolved, operation="delete")
    if budget_error:
        return budget_error
    before = _stat_payload(resolved)
    if dry_run:
        return _json({"status": "ok", "operation": "delete", "dry_run": True, "path": str(resolved), "before": before})
    if resolved.is_dir():
        if not recursive:
            resolved.rmdir()
        else:
            shutil.rmtree(resolved)
    else:
        resolved.unlink()
    return _json({"status": "ok", "operation": "delete", "path": str(resolved), "before": before, "after": _missing_stat_payload(resolved)})


def create_file_compat(path: str, content: str = "", file_type: str = "text", *, _bypass_gate: bool = False) -> str:
    normalized_type = (file_type or "text").strip().lower()
    if normalized_type == "text":
        if not _bypass_gate:
            pending = _gate_path(
                ActionType.MUTATE,
                path=path,
                tool_name="create_file",
                action_description=f"Create text file: {path}",
                payload_args={"path": path, "content": content, "file_type": normalized_type},
            )
            if pending:
                return pending
        return file_write(path, content, overwrite=True, create_parents=True, _bypass_gate=True)

    payload_args = {"path": path, "content": content, "file_type": normalized_type}
    if not _bypass_gate:
        pending = _gate_path(
            ActionType.MUTATE,
            path=path,
            tool_name="create_file",
            action_description=f"Create {normalized_type} file: {path}",
            payload_args=payload_args,
        )
        if pending:
            return pending

    resolved = _resolve_path(path)
    before = _stat_payload(resolved) if resolved.exists() else _missing_stat_payload(resolved)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if normalized_type == "xlsx":
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Sheet1"
        if content:
            for row_idx, row in enumerate(content.strip().split("\n"), start=1):
                for col_idx, cell in enumerate(row.split(","), start=1):
                    ws.cell(row=row_idx, column=col_idx, value=cell.strip())
        wb.save(resolved)
    elif normalized_type == "docx":
        from docx import Document

        doc = Document()
        if content:
            doc.add_paragraph(content)
        doc.save(resolved)
    else:
        return _json({"status": "error", "error": f"Unsupported file_type '{file_type}'"})
    return _json({"status": "ok", "path": str(resolved), "file_type": normalized_type, "before": before, "after": _stat_payload(resolved)})


def read_file_compat(path: str) -> str:
    return file_read(path, max_bytes=8192)


def _parse_json_object(input_str: str) -> dict[str, Any]:
    try:
        value = json.loads(input_str) if input_str else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _resume_file_write(input_str: str) -> str:
    args = _parse_json_object(input_str)
    return file_write(
        str(args.get("path", "")),
        str(args.get("content", "")),
        bool(args.get("overwrite", False)),
        bool(args.get("create_parents", True)),
        str(args.get("encoding", "utf-8")),
        bool(args.get("dry_run", False)),
        _bypass_gate=True,
    )


def _resume_file_append(input_str: str) -> str:
    args = _parse_json_object(input_str)
    return file_append(
        str(args.get("path", "")),
        str(args.get("content", "")),
        bool(args.get("create_parents", True)),
        str(args.get("encoding", "utf-8")),
        bool(args.get("dry_run", False)),
        _bypass_gate=True,
    )


def _resume_create_file(input_str: str) -> str:
    args = _parse_json_object(input_str)
    return create_file_compat(
        str(args.get("path", "")),
        str(args.get("content", "")),
        str(args.get("file_type", "text")),
        _bypass_gate=True,
    )


def _resume_file_patch(input_str: str) -> str:
    args = _parse_json_object(input_str)
    return file_patch(
        str(args.get("path", "")),
        str(args.get("old", "")),
        str(args.get("new", "")),
        int(args.get("count", 1) or 1),
        str(args.get("encoding", "utf-8")),
        bool(args.get("dry_run", False)),
        _bypass_gate=True,
    )


def _resume_fs_mkdir(input_str: str) -> str:
    args = _parse_json_object(input_str)
    return fs_mkdir(
        str(args.get("path", "")),
        bool(args.get("parents", True)),
        bool(args.get("exist_ok", True)),
        bool(args.get("dry_run", False)),
        _bypass_gate=True,
    )


def _resume_fs_copy(input_str: str) -> str:
    args = _parse_json_object(input_str)
    return fs_copy(
        str(args.get("source", "")),
        str(args.get("destination", "")),
        bool(args.get("overwrite", False)),
        bool(args.get("create_parents", True)),
        bool(args.get("dry_run", False)),
        _bypass_gate=True,
    )


def _resume_fs_move(input_str: str) -> str:
    args = _parse_json_object(input_str)
    return fs_move(
        str(args.get("source", "")),
        str(args.get("destination", "")),
        bool(args.get("overwrite", False)),
        bool(args.get("create_parents", True)),
        bool(args.get("dry_run", False)),
        _bypass_gate=True,
    )


def _resume_fs_rename(input_str: str) -> str:
    args = _parse_json_object(input_str)
    return fs_rename(
        str(args.get("path", "")),
        str(args.get("new_name", "")),
        bool(args.get("overwrite", False)),
        bool(args.get("dry_run", False)),
        _bypass_gate=True,
    )


def _resume_fs_delete(input_str: str) -> str:
    args = _parse_json_object(input_str)
    return fs_delete(
        str(args.get("path", "")),
        bool(args.get("recursive", True)),
        bool(args.get("dry_run", False)),
        _bypass_gate=True,
    )


register_executor("fs_write", _resume_file_write)
register_executor("fs_append", _resume_file_append)
register_executor("fs_patch", _resume_file_patch)
register_executor("fs_mkdir", _resume_fs_mkdir)
register_executor("fs_copy", _resume_fs_copy)
register_executor("fs_move", _resume_fs_move)
register_executor("fs_rename", _resume_fs_rename)
register_executor("fs_delete", _resume_fs_delete)

# Legacy pending approvals may still exist after upgrade; keep these replay-only.
register_executor("file_write", _resume_file_write)
register_executor("file_append", _resume_file_append)
register_executor("create_file", _resume_create_file)
register_executor("file_patch", _resume_file_patch)
