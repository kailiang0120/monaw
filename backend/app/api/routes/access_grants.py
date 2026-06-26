from fastapi import APIRouter, HTTPException, Request

from app.agent.access_grant_broker import (
    get_pending_grants,
    resolve_grant,
    signal_resume as signal_grant_resume,
)
from app.schemas import AccessGrantDecisionIn, AccessGrantTicketOut

router = APIRouter()


@router.get("/access-grants/pending", response_model=list[AccessGrantTicketOut])
async def list_pending_access_grants(conversation_id: str = ""):
    tickets = get_pending_grants(conversation_id)
    return [
        AccessGrantTicketOut(
            id=t.id,
            conversation_id=t.conversation_id,
            control_session_id=t.control_session_id,
            execution_source=t.execution_source,
            target_type=t.target_type,
            target_identifier=t.target_identifier,
            display_name=t.display_name,
            action_context=t.action_context,
            status=t.status,
            created_at=t.created_at,
            expires_at=t.expires_at,
            payload_hash=t.payload_hash,
            superseded_by=t.superseded_by,
            resolved_at=t.resolved_at,
        )
        for t in tickets
    ]


@router.post("/access-grants/{ticket_id}/resolve", response_model=AccessGrantTicketOut)
async def resolve_access_grant(ticket_id: str, body: AccessGrantDecisionIn, request: Request):
    ticket = resolve_grant(
        ticket_id,
        body.decision,
        expected_session_id=request.state.control_session.session_id,
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found or not pending")
    signal_grant_resume(ticket_id, body.decision)
    return AccessGrantTicketOut(
        id=ticket.id,
        conversation_id=ticket.conversation_id,
        control_session_id=ticket.control_session_id,
        execution_source=ticket.execution_source,
        target_type=ticket.target_type,
        target_identifier=ticket.target_identifier,
        display_name=ticket.display_name,
        action_context=ticket.action_context,
        status=ticket.status,
        created_at=ticket.created_at,
        expires_at=ticket.expires_at,
        payload_hash=ticket.payload_hash,
        superseded_by=ticket.superseded_by,
        resolved_at=ticket.resolved_at,
    )
