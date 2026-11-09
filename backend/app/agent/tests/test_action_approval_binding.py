"""Exact replay and sanitized durable correlation are the security main path."""
import json
from types import SimpleNamespace
import pytest

from app.agent.action_security import attest_action, authorize_action
from app.agent.action_security.approval_binding import approved_action_replay, replay_validation
from app.agent.action_security.audit import public_descriptor, public_ticket
from app.agent.action_security import evaluate_action
from app.agent.action_identity import action_invocation, delegated_action, ticket_ownership_error
from app.agent.approval_broker import create_ticket, get_ticket, approve_ticket, apply_auto_review
from app.agent.settings_store import AgentSettings


def descriptor(**changes):
    values = dict(tool_name="send_adapter", action_kind="submit", parameters={"recipient": "one", "body": "private text"},
        target={"origin": "https://example.com", "page_id": "1"}, destination="https://example.com/send",
        context_version="document:1", side_effect="external_mutation",
        data_scope={"files": [{"path": "C:/private/report.txt", "size_bytes": 7, "sha256": "a" * 64}]},
        runtime_facts={"target_verified": True, "context_verified": True, "schema_hash": "schema1"})
    values.update(changes)
    return attest_action(**values)


@pytest.mark.parametrize("changed", [
    {"parameters": {"recipient": "two", "body": "private text"}},
    {"parameters": {"recipient": "one", "method": "POST"}},
    {"destination": "https://other.example/send"},
    {"target": {"origin": "https://example.com", "page_id": "2"}},
    {"context_version": "document:2"},
    {"data_scope": {"files": [{"path": "C:/private/other.txt"}]}},
    {"runtime_facts": {"target_verified": True, "context_verified": True, "schema_hash": "schema2"}},
    {"tool_name": "different_adapter"},
    {"action_kind": "press_key"},
])
def test_approval_cannot_replay_changed_material_action(changed):
    original = descriptor()
    result = authorize_action(original)
    ticket = get_ticket(result["ticket_id"])
    approve_ticket(ticket.id)
    with approved_action_replay(ticket):
        denied = authorize_action(descriptor(**changed))
    assert denied["reason_code"] == "action_approval_invalidated"


def test_exact_approval_dispatches_once_and_auto_review_cannot_approve_it():
    original = descriptor()
    ticket = get_ticket(authorize_action(original)["ticket_id"])
    assert apply_auto_review(ticket.id, ticket.model_dump(), "approve", "Model says yes") is None
    assert ticket.status.value == "pending"
    approve_ticket(ticket.id)
    with approved_action_replay(ticket):
        assert authorize_action(descriptor()) is None
        assert authorize_action(descriptor())["reason_code"] == "action_approval_invalidated"


def test_actual_resume_executes_only_after_fresh_adapter_authorization(monkeypatch):
    from app.agent.execution_resume import register_executor, resume_approved_ticket
    calls = []
    def executor(raw):
        args = json.loads(raw)
        denied = authorize_action(descriptor(parameters=args))
        if denied:
            return json.dumps(denied)
        calls.append(args)
        return '{"status":"ok"}'
    register_executor("send_adapter", executor)
    original = descriptor()
    ticket = get_ticket(authorize_action(original)["ticket_id"])
    approve_ticket(ticket.id)
    resume_approved_ticket(ticket)
    resume_approved_ticket(ticket)
    assert calls == [original.parameters]
    assert ticket.status.value == "applied"


def test_expiry_and_principal_change_invalidate_replay():
    from app.agent.run_context import set_current_principal_id, reset_current_principal_id
    ticket = get_ticket(authorize_action(descriptor())["ticket_id"])
    approve_ticket(ticket.id)
    token = set_current_principal_id("another-user")
    try:
        with approved_action_replay(ticket):
            assert "principal" in replay_validation(descriptor())
    finally:
        reset_current_principal_id(token)
    ticket.expires_at = "2000-01-01T00:00:00+00:00"
    with approved_action_replay(ticket):
        assert "expired" in replay_validation(descriptor())


def test_ticket_requires_explicit_parent_child_edge_and_exact_original_arguments():
    with action_invocation("computer_functions_act", {"op": "click"}):
        with pytest.raises(ValueError, match="parent/child"):
            create_ticket(tool_name="computer_functions_click")
        with delegated_action("computer_functions_click", {"uid": 1}):
            ticket = create_ticket(tool_name="computer_functions_click")
        assert not ticket_ownership_error(ticket, "computer_functions_act", {"op": "click"})
        assert ticket_ownership_error(ticket, "computer_functions_type")
        assert ticket_ownership_error(ticket, "computer_functions_act", {"op": "type"})
    with action_invocation("computer_functions_act", {"op": "click"}):
        assert ticket_ownership_error(ticket, "computer_functions_act", {"op": "click"})


def test_public_preview_excludes_code_payload_paths_url_query_and_content():
    action = descriptor(parameters={"script": "private_code(password='secret')"},
        destination="https://user:secret@example.com/send?token=private-token")
    ticket = get_ticket(authorize_action(action)["ticket_id"])
    public = public_ticket(ticket)
    text = json.dumps(public)
    for secret in ("private_code", "private-token", "C:/private", "sha256", "private text"):
        assert secret not in text
    assert public["action_descriptor"]["data_scope"]["files"] == [{"name": "report.txt", "size_bytes": 7}]


def test_journal_security_event_is_allowlisted_and_linked_to_existing_operation(tmp_path):
    from app.agent.database import Database
    from app.agent.durable import Journal
    db = Database(tmp_path / "journal.db")
    db.init_db()
    db.create_conversation("chat", "Security", workspace_dir="C:/work")
    journal = Journal(db)
    journal.create_run(run_id="run", conversation_id="chat", execution_source="desktop",
        owner_instance_id="owner", turn_id="turn", workspace_dir="C:/work")
    journal.prepare_operation("run", operation_id="op", owner_instance_id="owner", attempt=1,
        operation_kind="mcp_call", effect_class="opaque_effect", input_summary={})
    journal.record_action_security("run", operation_id="op", owner_instance_id="owner", attempt=1,
        payload={"outcome": "DENY", "action_id": "action", "reason_code": "action_scope_denied",
                 "authorization_binding": "a" * 64, "script": "private_code", "password": "secret"})
    events = journal.read_events("run")["events"]
    assert events[-1]["type"] == "action.security"
    assert events[-1]["run_id"] == "run"
    assert events[-1]["payload"]["operation_id"] == "op"
    assert "private_code" not in json.dumps(events)
    assert "password" not in json.dumps(events)


@pytest.mark.parametrize("browser_status,executed,expected", [
    ("uncertain", True, "uncertain"), ("failed", True, "uncertain"),
    ("failed", False, "failed"), ("verified", True, "returned"),
])
def test_browser_runtime_outcome_preserves_uncertainty_and_prevents_replay(tmp_path, monkeypatch,
        browser_status, executed, expected):
    from app.agent.database import Database
    from app.agent.durable import Journal
    from app.agent.durable.operation_execution import (OperationExecution, OperationDispatchBlocked,
        set_current_operation_execution, reset_current_operation_execution)
    from app.agent.durable import recovery
    monkeypatch.setattr(recovery, "assert_run_dispatch_allowed", lambda *_args: None)
    db = Database(tmp_path / "browser.db")
    db.init_db()
    db.create_conversation("chat", "Browser", workspace_dir="C:/work")
    journal = Journal(db)
    journal.create_run(run_id="run", conversation_id="chat", execution_source="desktop",
        owner_instance_id="owner", turn_id="turn", workspace_dir="C:/work")
    review = []
    operation = OperationExecution(durable_run=SimpleNamespace(journal=journal, run_id="run",
        owner_instance_id="owner", attempt=1), lifecycle=SimpleNamespace(request_review=review.append),
        operation_id="op", tool_name="mcp_browser_click", trusted_builtin=False,
        journal_tool_name="mcp_browser_click", operation_kind="browser_action", effect_class="opaque_effect")
    operation.prepare()
    token = set_current_operation_execution(operation)
    try:
        operation.begin_effect()
        operation.attest_browser_outcome({"status": browser_status, "action_id": "action-1",
            "action_executed": executed, "verification": {"type": "url_changed", "passed": browser_status == "verified"},
            "recovery": {"reason_code": "action_effect_uncertain"}})
        operation.settle('{"status":"ok","action_outcome":{"status":"verified"}}')
        row = journal.load_operation("run", "op")
        assert row["status"] == expected
        assert row["evidence"]["browser_status"] == browser_status
        assert bool(review) == (expected == "uncertain")
        with pytest.raises(OperationDispatchBlocked):
            operation.begin_effect()
    finally:
        reset_current_operation_execution(token)


def test_public_descriptor_flags_opaque_actions_and_extra_destinations():
    decision = SimpleNamespace(risk_level="medium", human_only=False)
    bound = public_descriptor(descriptor(), decision)
    assert bound["opaque"] is False and bound["also_sends_to"] == []

    script = public_descriptor(descriptor(action_kind="execute_code", side_effect="unknown"), decision)
    assert script["opaque"] is True
    remote = public_descriptor(descriptor(runtime_facts={"target_verified": True, "context_verified": True, "opaque_execution": True}), decision)
    assert remote["opaque"] is True

    link = public_descriptor(descriptor(action_kind="click", target={"page_id": 1, "ping_urls": [
        "https://collector.example.net/ping?id=secret", "https://collector.example.net/other"]}), decision)
    assert link["also_sends_to"] == ["https://collector.example.net"]
