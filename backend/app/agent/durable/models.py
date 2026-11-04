"""Small, typed DTOs and state constants for the durable journal."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, TypedDict

RunStatus = Literal[
    "running",
    "waiting_approval",
    "pausing",
    "paused",
    "interrupted",
    "needs_review",
    "completed",
    "failed",
    "cancelled",
]
OperationStatus = Literal[
    "prepared",
    "authorized",
    "executing",
    "verified",
    "failed",
    "cancelled",
    "uncertain",
    "needs_review",
    "returned",
]
EffectClass = Literal["read_only", "verifiable_write", "opaque_effect"]


class RunDTO(TypedDict):
    run_id: str
    conversation_id: str
    attempt: int
    revision: int
    status: RunStatus
    reason_code: str | None
    available_actions: list[str]
    last_seq: int
    created_at: str
    updated_at: str
    finished_at: str | None
    assistant_message_id: int | None


class OperationDTO(TypedDict):
    run_id: str
    operation_id: str
    sequence: int
    attempt: int
    operation_kind: str
    related_operation_id: str | None
    effect_class: EffectClass
    status: OperationStatus
    reason_code: str | None
    input_summary: dict[str, bool | int | str]
    evidence: dict[str, bool | int | str]
    snapshot_available: bool
    created_at: str
    updated_at: str


class EventDTO(TypedDict):
    run_id: str
    seq: int
    type: str
    at: str
    payload: dict[str, object]


class EventPageDTO(TypedDict):
    run_id: str
    events: list[EventDTO]
    next_seq: int
    last_seq: int


class JournalConflictError(RuntimeError):
    """Base class for state, ownership, and compare-and-swap conflicts."""


class IdempotencyConflictError(JournalConflictError):
    """A stable ID was reused with incompatible immutable metadata."""


class RevisionConflictError(JournalConflictError):
    """The caller's expected run revision is stale."""


class LeaseConflictError(JournalConflictError):
    """The current owner or attempt does not match the caller."""


class InvalidTransitionError(JournalConflictError):
    """A requested run or operation state transition is not allowed."""


class InFlightOperationsError(JournalConflictError):
    """A run cannot pause or finish while a write is still unresolved."""


class RunNotFoundError(LookupError):
    """The requested run does not exist."""


class OperationNotFoundError(LookupError):
    """The requested operation does not exist in the run."""


class EventsExpiredError(LookupError):
    """The requested cursor predates the first retained event."""


class InvalidCursorError(ValueError):
    """An event cursor is malformed or beyond the latest event."""


class TurnCheckpointDTO(TypedDict):
    run_id: str
    attempt: int
    turn_id: str
    conversation_id: str
    user_message_ids: list[int]


RUN_STATUSES = frozenset(
    {
        "running",
        "waiting_approval",
        "pausing",
        "paused",
        "interrupted",
        "needs_review",
        "completed",
        "failed",
        "cancelled",
    }
)
OPERATION_STATUSES = frozenset(
    {
        "prepared",
        "authorized",
        "executing",
        "verified",
        "failed",
        "cancelled",
        "uncertain",
        "needs_review",
        "returned",
    }
)
EFFECT_CLASSES = frozenset({"read_only", "verifiable_write", "opaque_effect"})
OPERATION_KINDS = frozenset(
    {
        "file_move",
        "file_write",
        "file_delete",
        "file_copy",
        "file_mkdir",
        "file_mutation",
        "undo",
        "shell_command",
        "browser_action",
        "mcp_call",
        "tool_call",
        "other",
    }
)
EVENT_TYPES = frozenset(
    {"run.created", "run.updated", "operation.prepared", "operation.updated"}
)
REASON_CODES = frozenset(
    {
        "approval_rejected",
        "approval_required",
        "budget_exceeded",
        "cancelled",
        "checkpoint_committed",
        "checkpoint_missing",
        "completed",
        "external_effect_unknown",
        "interrupted",
        "lease_expired",
        "operation_failed",
        "owner_conflict",
        "pause_requested",
        "recovery_checked",
        "recovery_incomplete",
        "run_failed",
        "safe_to_retry",
        "skipped",
        "steering",
        "timeout",
        "undo_conflict",
        "unknown",
        "user_pause",
        "user_stop",
        "user_cancelled",
        "verification_failed",
        "verified",
        "worker_lost",
    }
)

_BOOL_FIELDS = frozenset(
    {
        "source_exists",
        "destination_exists",
        "target_exists",
        "source_identity_matches",
        "destination_identity_matches",
        "identity_matches",
        "sha256_matches",
        "source_sha256_matches",
        "destination_sha256_matches",
        "before_exists",
        "after_exists",
        "undo_available",
        "snapshot_available",
        "is_binary",
    }
)
_INT_FIELDS = frozenset({"target_count", "byte_length", "line_count", "file_count"})
_HASH_FIELDS = frozenset(
    {"content_sha256", "source_sha256", "destination_sha256", "before_sha256", "after_sha256"}
)
_ENUM_FIELDS = {
    "verification_result": frozenset(
        {"completed", "unchanged", "mixed", "unknown", "conflict", "not_checked"}
    ),
}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def sanitize_evidence(value: object) -> dict[str, bool | int | str]:
    """Keep only bounded, typed evidence fields that are safe to expose."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, bool | int | str] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if key in _BOOL_FIELDS and isinstance(item, bool):
            result[key] = item
        elif key in _INT_FIELDS and isinstance(item, int) and not isinstance(item, bool):
            if 0 <= item <= 1_000_000_000_000:
                result[key] = item
        elif key in _HASH_FIELDS and isinstance(item, str) and _HASH_RE.fullmatch(item):
            result[key] = item
        elif key in _ENUM_FIELDS and isinstance(item, str) and item in _ENUM_FIELDS[key]:
            result[key] = item
        if len(result) >= 24:
            break
    return result


def sanitize_public_evidence(value: object) -> dict[str, bool | int | str]:
    """Project internal evidence without raw content digests."""
    return {key: item for key, item in sanitize_evidence(value).items() if key not in _HASH_FIELDS}


def sanitize_event_payload(event_type: str, payload: object) -> dict[str, object]:
    """Project an event body onto its bounded public DTO shape."""
    if event_type not in EVENT_TYPES or not isinstance(payload, Mapping):
        return {}

    result: dict[str, object] = {}

    def safe_id(key: str) -> None:
        value = payload.get(key)
        if isinstance(value, str) and _SAFE_ID_RE.fullmatch(value):
            result[key] = value

    def safe_int(key: str, *, minimum: int = 0) -> None:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= 2**53:
            result[key] = value

    def safe_enum(key: str, choices: frozenset[str]) -> None:
        value = payload.get(key)
        if isinstance(value, str) and value in choices:
            result[key] = value

    if event_type in {"run.created", "run.updated"}:
        safe_enum("status", RUN_STATUSES)
        safe_int("attempt", minimum=1)
        safe_int("revision", minimum=1)
        if payload.get("reason_code") is None:
            result["reason_code"] = None
        else:
            safe_enum("reason_code", REASON_CODES)
    else:
        safe_id("operation_id")
        safe_int("sequence", minimum=1)
        safe_int("attempt", minimum=1)
        if event_type == "operation.prepared":
            safe_enum("operation_kind", OPERATION_KINDS)
            safe_enum("effect_class", EFFECT_CLASSES)
        elif event_type == "operation.updated":
            # A native file operation can commit its pre-effect snapshot and
            # promotion independently of a status transition. Expose only the
            # bounded classification, never its private manifest reference.
            safe_enum("effect_class", EFFECT_CLASSES)
        safe_enum("status", OPERATION_STATUSES)
        if "revision" in payload:
            safe_int("revision", minimum=1)
        if payload.get("reason_code") is None:
            if event_type == "operation.updated":
                result["reason_code"] = None
        else:
            safe_enum("reason_code", REASON_CODES)
        if event_type == "operation.updated":
            result["evidence"] = sanitize_public_evidence(payload.get("evidence"))

    return result


def is_safe_identifier(value: object) -> bool:
    return isinstance(value, str) and _SAFE_ID_RE.fullmatch(value) is not None
