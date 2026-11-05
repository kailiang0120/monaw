"""Shared runtime policy entry point for trusted side-effect adapters."""
from .models import ActionDescriptor, PolicyDecision, PolicyOutcome, attest_action
from .policy_engine import evaluate_action


def authorize_action(action: ActionDescriptor, *, replay_payload: dict | None = None) -> dict | None:
    """None allows exactly this dispatch; structured output must stop execution.

    Call again with freshly collected runtime facts immediately before an
    approved replay. Never bypass this function because an approval ID exists.
    """
    from .approval_binding import replay_validation, current_approved_ticket
    from .audit import public_descriptor, record_decision
    decision = evaluate_action(action)
    if decision.outcome == PolicyOutcome.DENY:
        record_decision(action, decision)
        return {"status": "denied", "reason_code": decision.reason_code, "reason": decision.reason}
    replay_error = replay_validation(action)
    if replay_error is not None:
        if replay_error:
            return {"status": "denied", "reason_code": "action_approval_invalidated", "reason": replay_error}
        from dataclasses import replace
        ticket = current_approved_ticket()
        approved_action_id = (ticket.action_descriptor or {}).get("action_id", action.action_id)
        record_decision(replace(action, action_id=approved_action_id), PolicyDecision(PolicyOutcome.ALLOW,
            "exact_approval", "Exact action approved.", decision.risk_level, decision.human_only), ticket_id=ticket.id)
        return None
    if decision.outcome == PolicyOutcome.ALLOW:
        record_decision(action, decision)
        return None
    from app.agent.approval_broker import create_ticket
    from app.agent.durable.operation_execution import current_operation_execution
    public = public_descriptor(action, decision)
    ticket = create_ticket(tool_name=action.tool_name, action_type=action.action_kind,
        target_app=str(action.target.get("app") or action.target.get("server_id") or action.target.get("origin") or ""),
        risk_level=decision.risk_level, reason=decision.reason, action_description=decision.reason,
        payload=replay_payload or {"args": action.parameters}, authorization_binding=action.authorization_binding,
        action_descriptor=public, human_only=decision.human_only, run_id=action.run_id, operation_id=action.operation_id)
    operation = current_operation_execution()
    if operation is not None:
        if not operation.attest_local_approval_preflight(ticket, expected_tool_name=action.tool_name,
                expected_action_type=action.action_kind, expected_target_app=ticket.target_app,
                expected_payload=ticket.payload):
            from app.agent.approval_broker import reject_ticket
            reject_ticket(ticket.id, resolved_by="system:unattested_preflight")
            return {"status": "denied", "reason_code": "unsafe_action_preflight", "reason": "Approval cannot safely resume this operation."}
    record_decision(action, decision, ticket_id=ticket.id)
    return {"status": "pending_approval", "ticket_id": ticket.id, "action": decision.reason,
        "reason": decision.reason, "action_descriptor": public, "human_only": decision.human_only,
        "authorization_binding": action.authorization_binding}


__all__ = ["ActionDescriptor", "PolicyDecision", "PolicyOutcome", "attest_action", "authorize_action", "evaluate_action"]
