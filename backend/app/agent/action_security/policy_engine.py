"""One action policy evaluated at an observable side-effect boundary."""
from .classifier import human_only, risk_level
from .models import ActionDescriptor, PolicyDecision, PolicyOutcome


def evaluate_action(action: ActionDescriptor, permissions=None) -> PolicyDecision:
    from app.agent.controller_policy import _load_runtime_settings
    from app.agent.permission_actions import ActionType, requires_confirmation
    permissions = permissions or _load_runtime_settings().permissions
    risk = risk_level(action)
    human = human_only(action)
    def decision(outcome, code, reason):
        return PolicyDecision(outcome, code, reason, risk, human)
    if not action.attested:
        return decision(PolicyOutcome.DENY, "unattested_action", "Runtime action facts are missing or changed.")
    if not action.tool_name or not action.target or action.runtime_facts.get("target_verified") is not True:
        return decision(PolicyOutcome.DENY, "unknown_action_target", "The actual action target could not be established.")
    if action.runtime_facts.get("context_verified") is not True or not action.context_version:
        return decision(PolicyOutcome.DENY, "unknown_action_context", "The current action context could not be established.")
    if action.side_effect not in {"none", "local_mutation", "external_mutation", "irreversible", "unknown"}:
        return decision(PolicyOutcome.DENY, "invalid_action_effect", "The action effect is invalid.")
    if action.runtime_facts.get("scope_allowed") is False or action.runtime_facts.get("destination_allowed") is False:
        return decision(PolicyOutcome.DENY, "action_scope_denied", "The action exceeds its configured data or destination scope.")
    if action.action_kind == "credential_access" and action.runtime_facts.get("dedicated_credential_flow") is not True:
        return decision(PolicyOutcome.DENY, "credential_access_denied", "Credential access requires a dedicated trusted flow.")
    if action.action_kind == "delete" and not permissions.allow_delete:
        return decision(PolicyOutcome.DENY, "delete_denied", "Deletion is disabled by current policy.")
    if action.side_effect == "external_mutation" and not action.destination:
        return decision(PolicyOutcome.DENY, "unknown_action_destination", "The consequential action destination is unknown.")
    if human:
        if action.identity.get("interactive") is not True:
            return decision(PolicyOutcome.DENY, "human_review_unavailable", "This action requires direct user review.")
        return decision(PolicyOutcome.REQUIRE_HUMAN_APPROVAL, "consequential_action", "Review the exact consequential action before execution.")
    if action.side_effect == "unknown" and action.runtime_facts.get("explicit_tool_trust") is True:
        # This is an explicit user-configured capability for this exact server,
        # schema and tool; it does not certify its internals as read-only.
        confirmation = action.runtime_facts.get("adapter_permission_requires_confirmation")
        if confirmation is False:
            return decision(PolicyOutcome.ALLOW, "explicit_tool_capability", "Allowed by the configured exact tool capability.")
        if confirmation is True:
            return decision(PolicyOutcome.REQUIRE_HUMAN_APPROVAL, "configured_tool_review", "Current permissions require review of this tool capability.")
        return decision(PolicyOutcome.DENY, "unknown_tool_capability", "The configured tool capability is incomplete.")
    # Full Access deliberately permits ordinary scripts with opaque internals.
    # Untrusted remote tools still need explicit review of unknown capabilities.
    if action.side_effect == "unknown" and action.runtime_facts.get("untrusted_remote") is True:
        return decision(PolicyOutcome.REQUIRE_HUMAN_APPROVAL, "unknown_remote_effect", "The remote tool effects could not be established.")
    if permissions.mode == "full_access":
        return decision(PolicyOutcome.ALLOW, "full_access", "Allowed by Full Access.")
    if action.runtime_facts.get("adapter_permission_requires_confirmation") is True:
        return decision(PolicyOutcome.REQUIRE_HUMAN_APPROVAL, "mode_requires_review", "Current permissions require review of this action.")
    if action.action_kind == "execute_code" and action.side_effect == "unknown":
        confirmation = action.runtime_facts.get("adapter_permission_requires_confirmation")
        if confirmation is False and permissions.mode == "custom":
            return decision(PolicyOutcome.ALLOW, "custom_execution_capability", "Allowed by the configured execution capability.")
    if action.side_effect == "unknown":
        return decision(PolicyOutcome.REQUIRE_HUMAN_APPROVAL, "unknown_action_effect", "Review this action's unknown effects.")
    vocabulary = {"read": ActionType.READ, "navigate": ActionType.LAUNCH_APP,
        "click": ActionType.CLICK, "type": ActionType.TYPE, "delete": ActionType.DELETE,
        "execute_code": ActionType.EXEC}
    kind = vocabulary.get(action.action_kind, ActionType.MUTATE)
    if requires_confirmation(kind, permissions):
        return decision(PolicyOutcome.REQUIRE_HUMAN_APPROVAL, "mode_requires_review", "Current permissions require review of this action.")
    return decision(PolicyOutcome.ALLOW, "action_allowed", "Allowed by current action policy.")
