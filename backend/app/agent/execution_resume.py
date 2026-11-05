"""Execution resume engine – replays approved ticket payloads deterministically.

When a ticket is approved, the resume engine locates the original controller
function, validates the payload hash, and executes the exact stored operation.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.agent.run_context import (
    current_control_session_id,
    current_conversation_id,
    current_execution_source,
)
from app.agent.tool_cancellation import check_cancelled, ToolCancelled
from app.agent.action_identity import ticket_ownership_error

from app.agent.approval_broker import (
    ApprovalTicket,
    TicketStatus,
    mark_applied,
    mark_failed,
    ticket_validation_error,
)

# Registry: tool_name -> callable that accepts the original input string
_EXECUTORS: dict[str, Callable[[str], str]] = {}


def register_executor(tool_name: str, fn: Callable[[str], str]) -> None:
    """Register a function that can replay a tool's action."""
    _EXECUTORS[tool_name] = fn


def resume_approved_ticket(ticket: ApprovalTicket) -> ApprovalTicket:
    """Execute the payload of an approved ticket and update its status.

    Returns the ticket with updated status (applied or failed).
    Validates payload hash integrity before execution.
    """
    if ticket.status != TicketStatus.APPROVED:
        return ticket

    ownership_error = ticket_ownership_error(ticket)
    if ownership_error:
        # Never consume or modify a ticket owned by a different live action.
        return ticket.model_copy(update={"execution_result": json.dumps({
            "status": "error", "reason_code": "approval_ticket_mismatch", "error": ownership_error,
        })})

    validation_error = ticket_validation_error(
        ticket,
        expected_session_id=current_control_session_id(),
        expected_conversation_id=current_conversation_id(),
        expected_execution_source=current_execution_source(),
        require_approved=True,
    )
    if validation_error:
        mark_failed(ticket.id, error=json.dumps({"status": "error", "reason_code": "approval_invalidated", "error": f"Approval invalidated: {validation_error}"}))
        return ticket

    # Approval authorizes this payload, never a permission that has since been
    # revoked. Access grants have already been consumed; recheck hard blocks.
    from app.agent.controller_policy import ActionType, resolve_permission
    try:
        action = ActionType(ticket.action_type)
    except ValueError:
        action = None  # External adapters revalidate their own actions.
    if action is not None:
        permission = resolve_permission(action, target_path=ticket.target_path, target_app=ticket.target_app)
        if permission.blocked:
            mark_failed(ticket.id, error=json.dumps({"status": "blocked", "reason": permission.reason}))
            return ticket

    executor = _EXECUTORS.get(ticket.tool_name)
    if not executor:
        mark_failed(ticket.id, error=json.dumps({"status": "error", "error": f"No executor registered for '{ticket.tool_name}'"}))
        return ticket

    input_str = ticket.payload.get("input_str", "")
    if not input_str:
        if "args" in ticket.payload:
            input_str = json.dumps(ticket.payload.get("args") or {})
        else:
            input_str = json.dumps(ticket.payload or {})

    try:
        check_cancelled()
        from app.agent.action_security.approval_binding import approved_action_replay, claim_ticket
        if not claim_ticket(ticket):
            return ticket.model_copy(update={"execution_result": json.dumps({
                "status": "error", "reason_code": "approval_already_used", "error": "Approval has already been consumed.",
            })})
        with approved_action_replay(ticket) as replay:
            result = executor(input_str)
            returned = json.loads(result) if isinstance(result, str) else result
            rejected = isinstance(returned, dict) and returned.get("status") in {
                "denied", "blocked", "error", "failed", "cancelled"}
            if ticket.authorization_binding and not replay.consumed and not rejected:
                result = json.dumps({"status": "denied", "reason_code": "action_approval_not_revalidated",
                    "reason": "The adapter did not revalidate the approved runtime action."})
        result_data = json.loads(result) if isinstance(result, str) else result
        result_status = result_data.get("status") if isinstance(result_data, dict) else None
        if result_status in {
            "error",
            "blocked",
            "denied",
            "failed",
            "cancelled",
            "pending_approval",
            "pending_access_grant",
        }:
            # Keep structured outcomes and partial progress; a bare error string
            # would be interpreted as success by the tool protocol.
            mark_failed(ticket.id, error=result if isinstance(result, str) else json.dumps(result_data))
        else:
            mark_applied(ticket.id, result=result if isinstance(result, str) else json.dumps(result))
    except ToolCancelled as exc:
        mark_failed(ticket.id, error=json.dumps({"status": "cancelled", "reason_code": exc.reason_code, "error": str(exc)}))
    except Exception as e:
        mark_failed(ticket.id, error=json.dumps({"status": "error", "error": str(e)}))

    return ticket


def get_registered_executors() -> list[str]:
    return list(_EXECUTORS.keys())
