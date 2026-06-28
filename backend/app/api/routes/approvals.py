from fastapi import APIRouter, HTTPException, Query, Request

from app.agent.approval_broker import (
    approve_ticket,
    get_history as get_approval_history,
    get_pending_tickets,
    get_ticket,
    reject_ticket,
    signal_resume as signal_approval_resume,
    ticket_validation_error,
)
from app.agent.ui_events import publish_ui_event
from app.schemas import ApprovalDecisionIn, ApprovalTicketOut

router = APIRouter()
PENDING_APPROVAL_LIMIT_MAX = 100
APPROVAL_HISTORY_LIMIT_MAX = 200

def _require_ticket_context(ticket_id: str, request: Request):
    ticket = get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    error = ticket_validation_error(
        ticket, expected_session_id=request.state.control_session.session_id
    )
    if error:
        status_code = 403 if "another control session" in error else 409
        raise HTTPException(status_code=status_code, detail=error)
    return ticket



@router.get("/approvals/pending", response_model=list[ApprovalTicketOut])
async def list_pending_approvals(
    conversation_id: str = "",
    limit: int = Query(50, ge=1, le=PENDING_APPROVAL_LIMIT_MAX),
    offset: int = Query(0, ge=0, le=10_000),
):
    tickets = get_pending_tickets(conversation_id)
    return [_ticket_to_out(t) for t in tickets[offset:offset + limit]]


@router.post("/approvals/{ticket_id}/approve", response_model=ApprovalTicketOut)
async def approve(ticket_id: str, request: Request, body: ApprovalDecisionIn | None = None):
    _require_ticket_context(ticket_id, request)
    resolved_by = body.resolved_by if body else "user"
    ticket = approve_ticket(ticket_id, resolved_by=resolved_by or "user")
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found or not pending")
    signal_approval_resume(ticket_id, "approved")
    publish_ui_event(
        "approval_ticket.changed",
        {"ticket_id": ticket.id, "conversation_id": ticket.conversation_id, "status": ticket.status.value},
    )
    return _ticket_to_out(ticket)


@router.post("/approvals/{ticket_id}/reject", response_model=ApprovalTicketOut)
async def reject(ticket_id: str, request: Request, body: ApprovalDecisionIn | None = None):
    _require_ticket_context(ticket_id, request)
    resolved_by = body.resolved_by if body else "user"
    ticket = reject_ticket(ticket_id, resolved_by=resolved_by or "user")
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found or not pending")
    signal_approval_resume(ticket_id, "rejected")
    publish_ui_event(
        "approval_ticket.changed",
        {"ticket_id": ticket.id, "conversation_id": ticket.conversation_id, "status": ticket.status.value},
    )
    return _ticket_to_out(ticket)


@router.get("/approvals/history", response_model=list[ApprovalTicketOut])
async def approval_history(
    conversation_id: str = "",
    limit: int = Query(50, ge=1, le=APPROVAL_HISTORY_LIMIT_MAX),
    offset: int = Query(0, ge=0, le=10_000),
):
    tickets = get_approval_history(conversation_id, limit + offset)
    tickets = tickets[offset:offset + limit]
    return [_ticket_to_out(t) for t in tickets]


def _ticket_to_out(t) -> ApprovalTicketOut:
    return ApprovalTicketOut(
        id=t.id,
        conversation_id=t.conversation_id,
        control_session_id=t.control_session_id,
        execution_source=t.execution_source,
        action_type=t.action_type,
        tool_name=t.tool_name,
        target_path=t.target_path,
        target_app=t.target_app,
        risk_level=t.risk_level,
        reason=t.reason,
        action_description=t.action_description,
        payload=t.payload,
        payload_hash=t.payload_hash,
        status=t.status.value,
        created_at=t.created_at,
        expires_at=t.expires_at,
        superseded_by=t.superseded_by,
        resolved_at=t.resolved_at,
        resolved_by=t.resolved_by,
        execution_result=t.execution_result,
    )
