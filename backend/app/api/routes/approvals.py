from fastapi import APIRouter, HTTPException

from app.agent.approval_broker import (
    approve_ticket,
    get_history as get_approval_history,
    get_pending_tickets,
    reject_ticket,
    signal_resume as signal_approval_resume,
)
from app.schemas import ApprovalDecisionIn, ApprovalTicketOut

router = APIRouter()


@router.get("/approvals/pending", response_model=list[ApprovalTicketOut])
async def list_pending_approvals(conversation_id: str = ""):
    tickets = get_pending_tickets(conversation_id)
    return [_ticket_to_out(t) for t in tickets]


@router.post("/approvals/{ticket_id}/approve", response_model=ApprovalTicketOut)
async def approve(ticket_id: str, body: ApprovalDecisionIn | None = None):
    resolved_by = body.resolved_by if body else "user"
    ticket = approve_ticket(ticket_id, resolved_by=resolved_by or "user")
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found or not pending")
    signal_approval_resume(ticket_id, "approved")
    return _ticket_to_out(ticket)


@router.post("/approvals/{ticket_id}/reject", response_model=ApprovalTicketOut)
async def reject(ticket_id: str, body: ApprovalDecisionIn | None = None):
    resolved_by = body.resolved_by if body else "user"
    ticket = reject_ticket(ticket_id, resolved_by=resolved_by or "user")
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found or not pending")
    signal_approval_resume(ticket_id, "rejected")
    return _ticket_to_out(ticket)


@router.get("/approvals/history", response_model=list[ApprovalTicketOut])
async def approval_history(conversation_id: str = "", limit: int = 50):
    tickets = get_approval_history(conversation_id, limit)
    return [_ticket_to_out(t) for t in tickets]


def _ticket_to_out(t) -> ApprovalTicketOut:
    return ApprovalTicketOut(
        id=t.id,
        conversation_id=t.conversation_id,
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
        resolved_at=t.resolved_at,
        resolved_by=t.resolved_by,
        execution_result=t.execution_result,
    )
