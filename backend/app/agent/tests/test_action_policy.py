"""Main authorization paths: runtime provenance, modes and consequential effects."""
from dataclasses import replace
from types import SimpleNamespace
import pytest

from app.agent.action_security import attest_action, evaluate_action, PolicyOutcome
from app.agent.settings_store import PermissionSettings


def action(**changes):
    values = dict(tool_name="trusted_adapter", action_kind="click", parameters={"uid": "1"},
        target={"origin": "https://example.com", "page_id": "1"}, context_version="document:1",
        side_effect="local_mutation", runtime_facts={"target_verified": True, "context_verified": True})
    values.update(changes)
    return attest_action(**values)


@pytest.mark.parametrize("mode", ["default", "custom", "auto_review", "full_access"])
def test_proven_consequential_actions_always_need_human_review(mode):
    decision = evaluate_action(action(action_kind="submit", side_effect="external_mutation",
        destination="https://example.com/send"), PermissionSettings(mode=mode))
    assert decision.outcome == PolicyOutcome.REQUIRE_HUMAN_APPROVAL
    assert decision.human_only


def test_full_access_runs_ordinary_arbitrary_scripts_but_does_not_trust_remote_annotations():
    permissions = PermissionSettings(mode="full_access")
    script = action(action_kind="execute_code", side_effect="unknown", parameters={"script": "opaque_code()"})
    assert evaluate_action(script, permissions).outcome == PolicyOutcome.ALLOW
    opaque_remote = action(side_effect="unknown", runtime_facts={"target_verified": True,
        "context_verified": True, "untrusted_remote": True, "readOnlyHint": True})
    assert evaluate_action(opaque_remote, permissions).outcome == PolicyOutcome.REQUIRE_HUMAN_APPROVAL


@pytest.mark.parametrize("mode", ["default", "auto_review"])
def test_unknown_effects_need_explicit_review_outside_full_access(mode):
    assert evaluate_action(action(side_effect="unknown"), PermissionSettings(mode=mode)).outcome == PolicyOutcome.REQUIRE_HUMAN_APPROVAL


@pytest.mark.parametrize("changes", [
    {"target": {}}, {"context_version": ""},
    {"runtime_facts": {"target_verified": False, "context_verified": True}},
    {"runtime_facts": {"target_verified": True, "context_verified": False}},
    {"runtime_facts": {"target_verified": True, "context_verified": True, "scope_allowed": False}},
    {"action_kind": "credential_access"},
    {"side_effect": "external_mutation", "destination": ""},
])
def test_unknown_or_denied_critical_context_fails_closed_even_full_access(changes):
    assert evaluate_action(action(**changes), PermissionSettings(mode="full_access")).outcome == PolicyOutcome.DENY


def test_runtime_attestation_cannot_survive_mutation_or_descriptor_relabeling():
    original = action()
    assert original.attested
    assert not replace(original, side_effect="none").attested
    original.parameters["uid"] = "different-target"
    assert not original.attested
    assert evaluate_action(original, PermissionSettings(mode="full_access")).outcome == PolicyOutcome.DENY


def test_unattended_source_cannot_inherit_interactive_consequential_approval():
    from app.agent.run_context import set_current_interactive, reset_current_interactive
    token = set_current_interactive(False)
    try:
        descriptor = action(side_effect="external_mutation", destination="https://example.com/send")
    finally:
        reset_current_interactive(token)
    assert evaluate_action(descriptor).outcome == PolicyOutcome.DENY


def test_custom_interaction_confirmation_switch_is_respected():
    permissions = PermissionSettings(mode="custom")
    permissions.confirmations.click = True
    assert evaluate_action(action(), permissions).outcome == PolicyOutcome.REQUIRE_HUMAN_APPROVAL
    permissions.confirmations.click = False
    assert evaluate_action(action(), permissions).outcome == PolicyOutcome.ALLOW


@pytest.mark.parametrize("mode", ["default", "custom", "auto_review", "full_access"])
@pytest.mark.parametrize("effect", ["local_mutation", "irreversible"])
def test_explicit_delete_disable_precedes_mode_and_human_approval(mode, effect):
    permissions = PermissionSettings(mode=mode, allow_delete=False)
    decision = evaluate_action(action(action_kind="delete", side_effect=effect), permissions)
    assert decision.outcome == PolicyOutcome.DENY
    assert decision.reason_code == "delete_denied"
