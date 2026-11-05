from fastapi import APIRouter, Depends, HTTPException, Query, Request

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
from app.agent.action_security.audit import public_ticket
from app.schemas import ApprovalDecisionIn, ApprovalTicketOut

async def _authenticated_approval_scope(request: Request):
    from app.agent.run_context import (
        set_current_principal_id, reset_current_principal_id,
        set_current_permission_profile_id, reset_current_permission_profile_id,
    )
    session = request.state.control_session
    principal = set_current_principal_id(session.profile_id or session.session_id)
    profile = set_current_permission_profile_id("desktop-current")
    try:
        yield
    finally:
        reset_current_permission_profile_id(profile)
        reset_current_principal_id(principal)


router = APIRouter(dependencies=[Depends(_authenticated_approval_scope)])
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
    return ApprovalTicketOut.model_validate(public_ticket(t))
