"""Immutable operation plans and sanitized public file mutation previews.

This module is intentionally pure with respect to the filesystem. The caller
must resolve and authorize every path, and must not supply existing file bytes
unless the real READ permission check has succeeded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.agent.file_mutation.diff_engine import DiffPreview, unified_diff_preview

Identity = tuple[int, int] | None
IdentityStrategy = Literal["same", "replaced", "absent", "unknown"]


@dataclass(frozen=True, slots=True, repr=False)
class PathState:
    """Private path state used for preview binding and effect verification."""

    path: str
    exists: bool
    kind: str
    size: int | None
    sha256: str | None = None
    identity: Identity = None
    modified_ns: int | None = None
    created_ns: int | None = None
    parent_identity: Identity = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("PathState.path must be a non-empty path string")
        if self.size is not None and self.size < 0:
            raise ValueError("PathState.size cannot be negative")
        for field_name in ("identity", "parent_identity"):
            identity = getattr(self, field_name)
            if identity is not None:
                normalized = tuple(identity)
                if len(normalized) != 2 or not all(isinstance(part, int) for part in normalized):
                    raise ValueError(f"PathState.{field_name} must be a two-integer identity")
                object.__setattr__(self, field_name, normalized)
        if self.sha256 is not None and (len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256)):
            raise ValueError("PathState.sha256 must be a lowercase SHA-256 digest")

    @classmethod
    def from_bytes(
        cls,
        path: str | Path,
        data: bytes,
        *,
        identity: Identity = None,
        modified_ns: int | None = None,
        created_ns: int | None = None,
        parent_identity: Identity = None,
    ) -> PathState:
        payload = bytes(data)
        return cls(
            path=str(path),
            exists=True,
            kind="file",
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            identity=identity,
            modified_ns=modified_ns,
            created_ns=created_ns,
            parent_identity=parent_identity,
        )

    @classmethod
    def from_fingerprint(cls, fingerprint: Any) -> PathState:
        """Adapt a fingerprint object without triggering another filesystem read."""
        data = getattr(fingerprint, "data", None)
        if data is not None:
            state = cls.from_bytes(
                fingerprint.path,
                data,
                identity=fingerprint.identity,
                modified_ns=fingerprint.modified_ns,
                created_ns=fingerprint.created_ns,
                parent_identity=fingerprint.parent_identity,
            )
            if fingerprint.sha256 is not None and state.sha256 != fingerprint.sha256:
                raise ValueError("Fingerprint bytes do not match the recorded digest")
            return state
        return cls(
            path=str(fingerprint.path),
            exists=bool(fingerprint.exists),
            kind=str(fingerprint.kind),
            size=fingerprint.size,
            sha256=fingerprint.sha256,
            identity=fingerprint.identity,
            modified_ns=fingerprint.modified_ns,
            created_ns=fingerprint.created_ns,
            parent_identity=fingerprint.parent_identity,
        )

    @classmethod
    def metadata(
        cls,
        path: str | Path,
        *,
        exists: bool,
        kind: str,
        size: int | None,
        identity: Identity = None,
        modified_ns: int | None = None,
        created_ns: int | None = None,
        parent_identity: Identity = None,
    ) -> PathState:
        """Create a stat-only state without reading or hashing file contents."""
        return cls(
            path=str(path),
            exists=exists,
            kind=kind,
            size=size,
            identity=identity,
            modified_ns=modified_ns,
            created_ns=created_ns,
            parent_identity=parent_identity,
        )

    def private_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "exists": self.exists,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
            "identity": list(self.identity) if self.identity is not None else None,
            "modified_ns": self.modified_ns,
            "created_ns": self.created_ns,
            "parent_identity": list(self.parent_identity) if self.parent_identity is not None else None,
        }

    def public_dict(self) -> dict[str, object]:
        return {"exists": self.exists, "kind": self.kind, "size": self.size}


@dataclass(frozen=True, slots=True, repr=False)
class PlanOperand:
    """One affected path and its before/expected-after states."""

    path: str
    role: str
    before: PathState
    after: PathState
    identity_strategy: IdentityStrategy = "unknown"

    def __post_init__(self) -> None:
        if not self.path or not self.role:
            raise ValueError("PlanOperand.path and role are required")
        if self.before.path != self.path or self.after.path != self.path:
            raise ValueError("PlanOperand path must match before and after PathState paths")
        if self.identity_strategy not in {"same", "replaced", "absent", "unknown"}:
            raise ValueError("Unsupported identity strategy")
        if self.identity_strategy == "absent" and self.after.exists:
            raise ValueError("An absent identity strategy requires an absent after state")
        if self.identity_strategy == "replaced" and not self.after.exists:
            raise ValueError("A replaced identity strategy requires an existing after state")
        if self.identity_strategy == "same":
            if not self.before.exists or not self.after.exists:
                raise ValueError("A same identity strategy requires existing before and after states")
            if self.before.identity is None or self.after.identity != self.before.identity:
                raise ValueError("A same identity strategy requires the unchanged file identity")
            if (
                self.before.parent_identity is not None
                and self.after.parent_identity != self.before.parent_identity
            ):
                raise ValueError("A same identity strategy requires the unchanged parent identity")

    def private_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "role": self.role,
            "identity_strategy": self.identity_strategy,
            "before": self.before.private_dict(),
            "after": self.after.private_dict(),
        }

    def public_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "role": self.role,
            "before": self.before.public_dict(),
            "after": self.after.public_dict(),
        }


@dataclass(frozen=True, slots=True, repr=False)
class MutationPlan:
    """Immutable execution evidence with a separate sanitized public view."""

    operation: str
    operands: tuple[PlanOperand, ...]
    undo_capability: str
    binding_digest: str = field(repr=False)
    preview: DiffPreview = field(repr=False)
    version: int = 1

    def to_preview_dict(self) -> dict[str, object]:
        """Return only fields intended for an authorized renderer/tool context."""
        return {
            "version": self.version,
            "operation": self.operation,
            "files": [operand.public_dict() for operand in self.operands],
            "undo_capability": self.undo_capability,
            "preview": self.preview.to_dict(),
        }

    def private_manifest(self) -> dict[str, object]:
        """Return internal evidence; never put this in user/tool output."""
        return {
            "version": self.version,
            "operation": self.operation,
            "binding_digest": self.binding_digest,
            "operands": [operand.private_dict() for operand in self.operands],
        }

    def same_binding(self, other: MutationPlan) -> bool:
        """Compare immutable request, operands, and pre/postconditions."""
        return hmac.compare_digest(self.binding_digest, other.binding_digest)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"$bytes_sha256": hashlib.sha256(value).hexdigest(), "$bytes_size": len(value)}
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"Unsupported mutation request value: {type(value).__name__}")


def _binding_digest(operation: str, request: Mapping[str, Any], operands: tuple[PlanOperand, ...]) -> str:
    binding = {
        "version": 1,
        "operation": operation,
        "request": _canonical_value(request),
        "operands": [operand.private_dict() for operand in operands],
    }
    payload = json.dumps(
        binding,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _metadata_preview(reason: str, before_bytes: bytes | None, after_bytes: bytes | None) -> DiffPreview:
    return DiffPreview(
        kind="summary",
        reason=reason,
        before_bytes=len(before_bytes) if before_bytes is not None else None,
        after_bytes=len(after_bytes) if after_bytes is not None else None,
    )


def build_plan(
    operation: str,
    request: Mapping[str, Any],
    operands: Sequence[PlanOperand],
    *,
    before_bytes: bytes | None = None,
    after_bytes: bytes | None = None,
    read_authorized: bool = False,
    encoding: str = "utf-8",
    undo_capability: str = "available",
) -> MutationPlan:
    """Build an immutable plan from already-authorized states and input.

    This function never opens paths. Existing contents must only be supplied
    after an actual READ permission check. When `read_authorized` is false, all
    bytes are ignored and the public preview contains metadata only.
    """
    normalized_operation = str(operation or "").strip().lower()
    if not normalized_operation:
        raise ValueError("operation is required")
    if not isinstance(request, Mapping):
        raise TypeError("request must be a mapping")
    normalized_operands = tuple(operands)
    if not normalized_operands:
        raise ValueError("at least one affected path operand is required")
    if len({operand.path for operand in normalized_operands}) != len(normalized_operands):
        raise ValueError("each affected path must appear only once in operands")
    if not undo_capability:
        raise ValueError("undo_capability is required")

    safe_before = bytes(before_bytes) if before_bytes is not None else None
    safe_after = bytes(after_bytes) if after_bytes is not None else None
    if not read_authorized:
        preview = _metadata_preview("read_permission_required", None, None)
    elif len(normalized_operands) != 1:
        preview = _metadata_preview("multi_path_operation", safe_before, safe_after)
    else:
        operand = normalized_operands[0]
        before_required = operand.before.exists and operand.before.kind == "file"
        after_required = operand.after.exists and operand.after.kind == "file"
        if (before_required and safe_before is None) or (after_required and safe_after is None):
            preview = _metadata_preview("content_unavailable", safe_before, safe_after)
        else:
            preview = unified_diff_preview(
                safe_before,
                safe_after,
                fromfile=f"a/{os.path.basename(operand.path)}",
                tofile=f"b/{os.path.basename(operand.path)}",
                encoding=encoding,
            )

    return MutationPlan(
        operation=normalized_operation,
        operands=normalized_operands,
        undo_capability=undo_capability,
        binding_digest=_binding_digest(normalized_operation, request, normalized_operands),
        preview=preview,
    )
