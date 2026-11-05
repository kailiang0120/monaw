"""Risk derives from adapter facts; labels and third-party hints are not proof."""
from .models import ActionDescriptor


def human_only(action: ActionDescriptor) -> bool:
    return (action.side_effect in {"external_mutation", "irreversible"}
            or action.action_kind in {"upload", "credential_access", "security_change", "payment", "send_message"}
            or action.runtime_facts.get("human_only") is True)


def risk_level(action: ActionDescriptor) -> str:
    if human_only(action) or action.action_kind == "execute_code" or action.side_effect == "unknown":
        return "high"
    return "low" if action.side_effect == "none" else "medium"
