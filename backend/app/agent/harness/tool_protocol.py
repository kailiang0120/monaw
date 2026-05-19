"""Typed protocol objects for agent tool execution."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ToolStatus = Literal[
    "ok",
    "error",
    "blocked",
    "denied",
    "failed",
    "pending_approval",
    "pending_access_grant",
]


@dataclass(frozen=True)
class ToolCallRequest:
    call_id: str
    name: str
    arguments: dict[str, Any]
    step_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolCallResult:
    call_id: str
    name: str
    status: ToolStatus
    output: str
    normalized_output: dict[str, Any] | None = None
    error: str | None = None
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_output(
        cls,
        *,
        call_id: str,
        name: str,
        output: str,
        fallback_status: ToolStatus = "ok",
        metadata: dict[str, Any] | None = None,
    ) -> "ToolCallResult":
        normalized: dict[str, Any] | None = None
        status: ToolStatus = fallback_status
        try:
            payload = json.loads(output) if isinstance(output, str) else output
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            normalized = payload
            raw_status = str(payload.get("status") or "").lower()
            if raw_status in {"ok", "error", "blocked", "denied", "failed", "pending_approval", "pending_access_grant"}:
                status = raw_status  # type: ignore[assignment]
        error = None
        if normalized and status != "ok":
            error = str(normalized.get("error") or normalized.get("reason") or "") or None
        return cls(
            call_id=call_id,
            name=name,
            status=status,
            output=str(output),
            normalized_output=normalized,
            error=error,
            retryable=status in {"error", "failed", "pending_approval", "pending_access_grant"},
            metadata=dict(metadata or {}),
        )


@dataclass
class HarnessStep:
    index: int
    kind: Literal["model", "tool", "approval", "final", "error"]
    status: str
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
