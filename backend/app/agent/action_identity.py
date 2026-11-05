"""Local invocation ownership for approval actions; never inferred from tool prefixes."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import threading
import uuid


@dataclass
class ActionInvocation:
    tool_name: str
    arguments_hash: str
    action_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    parent_action_id: str = ""
    parent_tool_name: str = ""
    root: ActionInvocation | None = None
    tickets: dict[str, str] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)


_CURRENT: ContextVar[ActionInvocation | None] = ContextVar("action_invocation", default=None)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@contextmanager
def action_invocation(tool_name: str, arguments: dict, *, delegated: bool = False):
    """A trusted local caller explicitly establishes each parent/child edge."""
    parent = _CURRENT.get() if delegated else None
    invocation = ActionInvocation(tool_name, _digest(arguments))
    if parent is not None:
        invocation.parent_action_id = parent.action_id
        invocation.parent_tool_name = parent.tool_name
        invocation.root = parent.root or parent
    token = _CURRENT.set(invocation)
    try:
        yield invocation
    finally:
        _CURRENT.reset(token)


@contextmanager
def delegated_action(tool_name: str, arguments: dict):
    """Declare a concrete trusted adapter call, without granting it permissions."""
    if _CURRENT.get() is None:
        yield None
    else:
        with action_invocation(tool_name, arguments, delegated=True) as invocation:
            yield invocation


def ticket_identity(tool_name: str = "") -> dict:
    invocation = _CURRENT.get()
    if invocation is None:
        return {}
    if tool_name and tool_name != invocation.tool_name:
        raise ValueError("Approval action requires an explicit parent/child invocation; tool-family membership is insufficient.")
    root = invocation.root or invocation
    return {
        "action_id": uuid.uuid4().hex,
        "parent_action_id": invocation.action_id,
        "parent_tool_name": invocation.tool_name,
        "root_action_id": root.action_id,
        "root_tool_name": root.tool_name,
    }


def _binding(ticket) -> str:
    return _digest({key: getattr(ticket, key, None) for key in (
        "action_id", "parent_action_id", "parent_tool_name", "root_action_id", "root_tool_name",
        "tool_name", "action_type", "target_path", "target_app", "target_type", "target_identifier", "payload_hash",
        "conversation_id", "control_session_id", "execution_source", "principal_id", "permission_profile_id", "interactive",
        "run_id", "operation_id", "authorization_binding", "action_descriptor", "human_only", "expires_at",
        "approval_material_hash",
    )})


def bind_ticket(ticket) -> None:
    invocation = _CURRENT.get()
    if invocation is not None:
        root = invocation.root or invocation
        with root.lock:
            root.tickets[ticket.id] = _binding(ticket)


def ticket_ownership_error(ticket, tool_name: str = "", arguments: dict | None = None) -> str:
    invocation = _CURRENT.get()
    if invocation is None:
        if getattr(ticket, "action_id", ""):
            return "ticket has no active parent invocation"
        # Legacy/direct callers retain exact-name checks only.
        if tool_name and getattr(ticket, "tool_name", tool_name) != tool_name:
            return "ticket belongs to another tool"
        return ""
    root = invocation.root or invocation
    if tool_name and tool_name != root.tool_name:
        return "ticket belongs to another parent tool"
    if arguments is not None and _digest(arguments) != root.arguments_hash:
        return "parent action arguments changed"
    with root.lock:
        expected = root.tickets.get(ticket.id)
    if not expected or expected != _binding(ticket) or getattr(ticket, "root_action_id", "") != root.action_id:
        return "ticket belongs to another action invocation or its binding changed"
    return ""
