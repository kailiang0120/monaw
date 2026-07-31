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

    validation_error = ticket_validation_error(
        ticket,
        expected_session_id=current_control_session_id(),
        expected_conversation_id=current_conversation_id(),
        expected_execution_source=current_execution_source(),
        require_approved=True,
    )
    if validation_error:
        mark_failed(ticket.id, error=f"Approval invalidated: {validation_error}")
        return ticket

    executor = _EXECUTORS.get(ticket.tool_name)
    if not executor:
        mark_failed(ticket.id, error=f"No executor registered for '{ticket.tool_name}'")
        return ticket

    input_str = ticket.payload.get("input_str", "")
    if not input_str:
        if "args" in ticket.payload:
            input_str = json.dumps(ticket.payload.get("args") or {})
        else:
            input_str = json.dumps(ticket.payload or {})

    try:
        result = executor(input_str)
        result_data = json.loads(result) if isinstance(result, str) else result
        if isinstance(result_data, dict) and result_data.get("status") == "error":
            mark_failed(ticket.id, error=result_data.get("error", "Unknown error"))
        else:
            mark_applied(ticket.id, result=result if isinstance(result, str) else json.dumps(result))
    except Exception as e:
        mark_failed(ticket.id, error=str(e))

    return ticket


def get_registered_executors() -> list[str]:
    return list(_EXECUTORS.keys())
