"""Canonical facts supplied by trusted adapters, never provider annotations."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import hashlib
import hmac
import json
import secrets
import uuid

_ATTESTATION_KEY = secrets.token_bytes(32)


def canonical_json(value: Any) -> str:
    # Unsupported objects and nonfinite numbers are rejected, not stringified.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class PolicyOutcome(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_HUMAN_APPROVAL = "REQUIRE_HUMAN_APPROVAL"


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reason_code: str
    reason: str
    risk_level: str = "low"
    human_only: bool = False


@dataclass(frozen=True)
class ActionDescriptor:
    action_id: str
    run_id: str
    operation_id: str
    tool_name: str
    action_kind: str
    target: dict[str, Any]
    destination: str
    data_scope: dict[str, Any]
    side_effect: str
    context_version: str
    parameters: dict[str, Any]
    expected_effect: dict[str, Any]
    runtime_facts: dict[str, Any]
    identity: dict[str, Any]
    _attestation: str = field(default="", repr=False)

    def material(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in (
            "run_id", "operation_id", "tool_name", "action_kind", "target", "destination",
            "data_scope", "side_effect", "context_version", "parameters", "expected_effect",
            "runtime_facts", "identity",
        )}

    @property
    def authorization_binding(self) -> str:
        return digest(self.material())

    @property
    def attested(self) -> bool:
        try:
            expected = hmac.new(_ATTESTATION_KEY, canonical_json(self.material()).encode(), hashlib.sha256).hexdigest()
            return bool(self._attestation) and hmac.compare_digest(expected, self._attestation)
        except (ValueError, TypeError):
            return False


def attest_action(*, tool_name: str, action_kind: str, parameters: dict,
                  target: dict, destination: str = "", context_version: str = "",
                  side_effect: str = "unknown", data_scope: dict | None = None,
                  expected_effect: dict | None = None, runtime_facts: dict | None = None) -> ActionDescriptor:
    """Only call after adapter validation. Model arguments cannot attest themselves."""
    from app.agent.durable.operation_execution import current_operation_execution
    from app.agent.run_context import (current_conversation_id, current_control_session_id,
        current_execution_source, current_interactive, current_principal_id, current_permission_profile_id)
    operation = current_operation_execution()
    fields = json.loads(canonical_json(dict(
        run_id=operation.run_id if operation else "", operation_id=operation.operation_id if operation else "",
        tool_name=tool_name, action_kind=action_kind, target=target, destination=destination,
        data_scope=data_scope or {}, side_effect=side_effect, context_version=context_version,
        parameters=parameters, expected_effect=expected_effect or {}, runtime_facts=runtime_facts or {},
        identity=dict(conversation_id=current_conversation_id(), control_session_id=current_control_session_id(),
            execution_source=current_execution_source(), interactive=current_interactive(),
            principal_id=current_principal_id(), permission_profile_id=current_permission_profile_id()),
    )))
    seal = hmac.new(_ATTESTATION_KEY, canonical_json(fields).encode(), hashlib.sha256).hexdigest()
    return ActionDescriptor(action_id=uuid.uuid4().hex, **fields, _attestation=seal)
