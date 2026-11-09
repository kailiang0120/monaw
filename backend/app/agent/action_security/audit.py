"""Sanitized projections; raw action parameters never enter public audit."""
from urllib.parse import urlsplit
from pathlib import PureWindowsPath
from .sensitive_data import redact_sensitive


def _safe_destination(value):
    try:
        parsed = urlsplit(str(value))
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    except ValueError:
        pass
    return redact_sensitive(str(value))[:500]


def public_descriptor(action, decision) -> dict:
    target = {key: value for key, value in action.target.items()
              if key in {"origin", "page_id", "app", "server_id", "frame_id"} and isinstance(value, (str, int))}
    if "origin" in target:
        target["origin"] = _safe_destination(target["origin"])
    scope = {key: value for key, value in action.data_scope.items()
             if key in {"file_count", "size_bytes", "estimated_bytes", "byte_count", "sensitivity", "categories"}}
    files = action.data_scope.get("files")
    if isinstance(files, (list, tuple)):
        scope["files"] = [{**{key: item[key] for key in ("size_bytes", "sensitivity") if key in item},
                           "name": PureWindowsPath(str(item.get("name") or item.get("path") or "")).name}
                          for item in files if isinstance(item, dict)]
    facts = action.runtime_facts or {}
    # Opaque: Monaw cannot observe what the action sends or changes, so an
    # approval cannot be limited to the shown destination.
    opaque = bool(facts.get("opaque_execution")) or action.side_effect == "unknown" or action.action_kind == "execute_code"
    pings = action.target.get("ping_urls")
    also = sorted({_safe_destination(url) for url in pings if isinstance(url, str)})[:8] if isinstance(pings, list) else []
    return redact_sensitive(dict(action_id=action.action_id, run_id=action.run_id, operation_id=action.operation_id,
        opaque=opaque, also_sends_to=also,
        tool_name=action.tool_name, action_kind=action.action_kind, target=target,
        destination=_safe_destination(action.destination), data_scope=scope, side_effect=action.side_effect,
        risk_level=decision.risk_level, human_only=decision.human_only,
        context_version=action.context_version[:200], authorization_binding=action.authorization_binding))


def public_ticket(ticket) -> dict:
    result = ticket.model_dump(mode="json")
    # Code, arbitrary tool arguments, content, and private replay receipts are
    # never an approval preview. Only preserve the existing safe sandbox label.
    sandbox = (ticket.payload.get("args") or {}).get("sandbox", {}) if isinstance(ticket.payload.get("args"), dict) else {}
    result["payload"] = {"args": {"sandbox": {key: sandbox[key] for key in
        ("execution_environment", "user_review_only") if key in sandbox}}} if isinstance(sandbox, dict) and sandbox else {}
    result["execution_result"] = ""
    result.pop("approval_material_hash", None)
    result["target_path"] = ""
    result["action_descriptor"] = getattr(ticket, "action_descriptor", {})
    return redact_sensitive(result)


def record_decision(action, decision, *, ticket_id=""):
    from app.agent.durable.operation_execution import current_operation_execution
    operation = current_operation_execution()
    if operation is None:
        return
    operation.journal.record_action_security(action.run_id, operation_id=action.operation_id,
        owner_instance_id=operation.owner_instance_id, attempt=operation.attempt,
        payload=dict(action_id=action.action_id, operation_id=action.operation_id,
            action_kind=action.action_kind, side_effect=action.side_effect,
            outcome=decision.outcome.value, reason_code=decision.reason_code,
            human_only=decision.human_only, authorization_binding=action.authorization_binding,
            ticket_id=ticket_id, action_descriptor=public_descriptor(action, decision)))
