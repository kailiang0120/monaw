"""Privacy classifications and retention labels for runtime data."""

from __future__ import annotations

from enum import Enum
from typing import Any


class FieldSensitivity(str, Enum):
    PUBLIC_METADATA = "public_metadata"
    OPERATIONAL = "operational"
    SENSITIVE_CONTENT = "sensitive_content"
    SECRET = "secret"


SECRET_FIELD_TOKENS = (
    "api_key",
    "authorization",
    "bearer",
    "cookie",
    "password",
    "secret",
    "session",
    "token",
)
SENSITIVE_FIELD_TOKENS = (
    "content",
    "final_output",
    "input",
    "message",
    "output",
    "prompt",
    "traceback",
    "user_message",
)
OPERATIONAL_FIELD_TOKENS = (
    "duration",
    "error",
    "failure",
    "latency",
    "level",
    "status",
    "tokens",
    "tool",
)


def classify_field(name: str) -> FieldSensitivity:
    normalized = str(name or "").lower()
    if any(token in normalized for token in SECRET_FIELD_TOKENS):
        return FieldSensitivity.SECRET
    if any(token in normalized for token in SENSITIVE_FIELD_TOKENS):
        return FieldSensitivity.SENSITIVE_CONTENT
    if any(token in normalized for token in OPERATIONAL_FIELD_TOKENS):
        return FieldSensitivity.OPERATIONAL
    return FieldSensitivity.PUBLIC_METADATA


def classify_fields(payload: dict[str, Any]) -> dict[str, str]:
    return {str(key): classify_field(str(key)).value for key in payload.keys()}


OBSERVABILITY_RUN_FIELD_CLASSIFICATION = {
    "run_id": FieldSensitivity.PUBLIC_METADATA.value,
    "conversation_id": FieldSensitivity.OPERATIONAL.value,
    "message_id": FieldSensitivity.OPERATIONAL.value,
    "source": FieldSensitivity.PUBLIC_METADATA.value,
    "model": FieldSensitivity.PUBLIC_METADATA.value,
    "provider": FieldSensitivity.PUBLIC_METADATA.value,
    "status": FieldSensitivity.OPERATIONAL.value,
    "failure_reason": FieldSensitivity.OPERATIONAL.value,
    "failure_pattern": FieldSensitivity.OPERATIONAL.value,
    "started_at": FieldSensitivity.PUBLIC_METADATA.value,
    "finished_at": FieldSensitivity.PUBLIC_METADATA.value,
    "duration_ms": FieldSensitivity.OPERATIONAL.value,
    "user_message": FieldSensitivity.SENSITIVE_CONTENT.value,
    "final_output": FieldSensitivity.SENSITIVE_CONTENT.value,
    "usage": FieldSensitivity.OPERATIONAL.value,
    "metadata": FieldSensitivity.OPERATIONAL.value,
}

OBSERVABILITY_EVENT_FIELD_CLASSIFICATION = {
    "event_id": FieldSensitivity.PUBLIC_METADATA.value,
    "run_id": FieldSensitivity.OPERATIONAL.value,
    "conversation_id": FieldSensitivity.OPERATIONAL.value,
    "event_type": FieldSensitivity.PUBLIC_METADATA.value,
    "level": FieldSensitivity.OPERATIONAL.value,
    "status": FieldSensitivity.OPERATIONAL.value,
    "tool_name": FieldSensitivity.PUBLIC_METADATA.value,
    "error_code": FieldSensitivity.OPERATIONAL.value,
    "error_message": FieldSensitivity.OPERATIONAL.value,
    "duration_ms": FieldSensitivity.OPERATIONAL.value,
    "input": FieldSensitivity.SENSITIVE_CONTENT.value,
    "output": FieldSensitivity.SENSITIVE_CONTENT.value,
    "tokens": FieldSensitivity.OPERATIONAL.value,
    "metadata": FieldSensitivity.OPERATIONAL.value,
}
