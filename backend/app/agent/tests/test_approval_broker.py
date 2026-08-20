"""Tests for the approval broker – ticket lifecycle, persistence, hash integrity."""

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path

import pytest

from app.agent.approval_broker import (
    ApprovalTicket,
    TicketStatus,
    create_ticket,
    get_ticket,
    get_pending_tickets,
    approve_ticket,
    reject_ticket,
    mark_applied,
    mark_failed,
    register_pending_resume,
    get_history,
    get_resume_decision,
    reload_from_disk,
    signal_resume,
    _all_tickets,
    _pending_index,
    _resume_decisions,
    _resume_events,
    _rewrite_tickets,
    _TICKETS_FILE,
    _APPROVAL_LOG,
)
from app.agent.run_context import (
    reset_current_conversation_id,
    reset_current_control_session_id,
    reset_current_execution_source,
    reset_current_interactive,
    reset_current_permission_profile_id,
    reset_current_principal_id,
    set_current_conversation_id,
    set_current_control_session_id,
    set_current_execution_source,
    set_current_interactive,
    set_current_permission_profile_id,
    set_current_principal_id,
)


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """Redirect persistence to temp dir and reset in-memory state."""
    tickets_file = tmp_path / "tickets.jsonl"
    log_file = tmp_path / "approval_log.md"

    import app.agent.approval_broker as mod
    monkeypatch.setattr(mod, "_TICKETS_FILE", tickets_file)
    monkeypatch.setattr(mod, "_APPROVAL_LOG", log_file)

    _all_tickets.clear()
    _pending_index.clear()
    _resume_events.clear()
    _resume_decisions.clear()
    yield


class TestTicketCreation:
    def test_create_ticket_returns_pending(self):
        t = create_ticket(
            action_type="delete",
            tool_name="ctrl_delete",
            target_path=r"C:\Users\Test\Downloads\file.txt",
            reason="Default mode: confirmation required",
            action_description="Delete: file.txt",
            payload={"path": r"C:\Users\Test\Downloads\file.txt"},
        )
        assert t.status == TicketStatus.PENDING
        assert t.id
        assert t.payload_hash

    def test_create_ticket_persists_to_pending_index(self):
        t = create_ticket(tool_name="ctrl_move", action_description="Move file")
        assert t.id in _pending_index

    def test_payload_hash_deterministic(self):
        payload = {"path": "test.txt", "destination": "moved.txt"}
        t1 = create_ticket(payload=payload)
        t2 = ApprovalTicket(payload=payload)
        assert t1.payload_hash == t2.compute_hash()

    def test_payload_hash_changes_on_different_payload(self):
        t1 = create_ticket(payload={"a": 1})
        t2 = create_ticket(payload={"a": 2})
        assert t1.payload_hash != t2.payload_hash

    def test_create_ticket_inherits_current_conversation_context(self):
        token = set_current_conversation_id("conv-context")
        try:
            t = create_ticket(tool_name="ctx_tool")
        finally:
            reset_current_conversation_id(token)

        assert t.conversation_id == "conv-context"

    def test_create_ticket_inherits_execution_principal_context(self):
        tokens = [
            (reset_current_conversation_id, set_current_conversation_id("conv-source")),
            (reset_current_control_session_id, set_current_control_session_id("session-1")),
            (reset_current_execution_source, set_current_execution_source("telegram")),
            (reset_current_principal_id, set_current_principal_id("telegram:chat:user")),
            (reset_current_permission_profile_id, set_current_permission_profile_id("telegram:restricted")),
            (reset_current_interactive, set_current_interactive(False)),
        ]
        try:
            ticket = create_ticket(tool_name="ctx_tool")
        finally:
            for reset, token in reversed(tokens):
                reset(token)

        assert ticket.conversation_id == "conv-source"
        assert ticket.control_session_id == "session-1"
        assert ticket.execution_source == "telegram"
        assert ticket.principal_id == "telegram:chat:user"
        assert ticket.permission_profile_id == "telegram:restricted"
        assert ticket.interactive is False


class TestTicketRetrieval:
    def test_get_ticket_returns_ticket(self):
        t = create_ticket(tool_name="ctrl_delete")
        result = get_ticket(t.id)
        assert result is not None
        assert result.id == t.id

    def test_get_ticket_returns_none_for_unknown(self):
        assert get_ticket("nonexistent") is None

    def test_get_pending_filters_by_conversation(self):
        create_ticket(conversation_id="conv1", tool_name="a")
        create_ticket(conversation_id="conv2", tool_name="b")
        create_ticket(conversation_id="conv1", tool_name="c")
        result = get_pending_tickets("conv1")
        assert len(result) == 2
        assert all(t.conversation_id == "conv1" for t in result)

    def test_get_pending_returns_all_when_no_filter(self):
        create_ticket(conversation_id="c1")
        create_ticket(conversation_id="c2")
        assert len(get_pending_tickets()) == 2


class TestTicketResolution:
    def test_approve_ticket(self):
        t = create_ticket(tool_name="ctrl_delete")
        result = approve_ticket(t.id)
        assert result is not None
        assert result.status == TicketStatus.APPROVED
        assert result.resolved_by == "user"
        assert result.resolved_at != ""
        assert t.id not in _pending_index

    def test_reject_ticket(self):
        t = create_ticket(tool_name="ctrl_move")
        result = reject_ticket(t.id)
        assert result is not None
        assert result.status == TicketStatus.REJECTED
        assert t.id not in _pending_index

    def test_approve_nonexistent_returns_none(self):
        assert approve_ticket("bogus") is None

    def test_reject_already_approved_returns_none(self):
        t = create_ticket()
        approve_ticket(t.id)
        assert reject_ticket(t.id) is None

    def test_approve_idempotent_on_already_approved(self):
        t = create_ticket()
        approve_ticket(t.id)
        assert approve_ticket(t.id) is None

    def test_register_pending_resume_handles_decision_signaled_before_waiter(self):
        async def scenario():
            signal_resume("ticket-race", "approved")
            event = register_pending_resume("ticket-race")
            assert event.is_set()
            assert get_resume_decision("ticket-race") == "approved"

        asyncio.run(scenario())

    def test_superseded_ticket_wakes_existing_waiter(self):
        first = create_ticket(tool_name="same_tool", action_type="same_action")
        event = register_pending_resume(first.id)

        second = create_ticket(tool_name="same_tool", action_type="same_action")

        assert first.status == TicketStatus.SUPERSEDED
        assert first.superseded_by == second.id
        assert event.is_set()
        assert get_resume_decision(first.id) == "superseded"

        _all_tickets.clear()
        _pending_index.clear()
        reload_from_disk()
        assert _all_tickets[first.id].status == TicketStatus.SUPERSEDED
        assert first.id not in _pending_index
        assert second.id in _pending_index


class TestAppliedAndFailed:
    def test_mark_applied(self):
        t = create_ticket()
        approve_ticket(t.id)
        result = mark_applied(t.id, result='{"status":"ok"}')
        assert result is not None
        assert result.status == TicketStatus.APPLIED
        assert result.execution_result == '{"status":"ok"}'

    def test_mark_failed(self):
        t = create_ticket()
        approve_ticket(t.id)
        result = mark_failed(t.id, error="disk full")
        assert result is not None
        assert result.status == TicketStatus.FAILED

    def test_mark_applied_on_non_approved_returns_none(self):
        t = create_ticket()
        assert mark_applied(t.id) is None


class TestHistory:
    def test_history_excludes_pending(self):
        create_ticket()
        t2 = create_ticket()
        approve_ticket(t2.id)
        h = get_history()
        assert len(h) == 1
        assert h[0].id == t2.id

    def test_history_ordered_by_resolved_at(self):
        t1 = create_ticket()
        t2 = create_ticket()
        approve_ticket(t1.id)
        reject_ticket(t2.id)
        h = get_history()
        assert len(h) == 2
        assert h[0].resolved_at >= h[1].resolved_at


class TestPersistence:
    def test_reload_from_disk_restores_state(self):
        t = create_ticket(tool_name="ctrl_delete")
        tid = t.id
        _all_tickets.clear()
        _pending_index.clear()
        reload_from_disk()
        assert tid in _all_tickets
        assert tid in _pending_index

    def test_resolved_ticket_not_in_pending_after_reload(self):
        t = create_ticket()
        approve_ticket(t.id)
        _all_tickets.clear()
        _pending_index.clear()
        reload_from_disk()
        assert t.id in _all_tickets
        assert t.id not in _pending_index


class TestExpiry:
    def _expire(self, ticket_id: str) -> None:
        stale = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        _all_tickets[ticket_id].expires_at = stale
        _rewrite_tickets()

    def test_expired_ticket_not_returned_as_pending(self):
        t = create_ticket(tool_name="ctrl_delete")
        self._expire(t.id)
        assert get_pending_tickets() == []
        assert _all_tickets[t.id].status == TicketStatus.EXPIRED

    def test_expired_ticket_not_resurrected_on_reload(self):
        t = create_ticket(tool_name="mcp__filesystem__delete_file")
        self._expire(t.id)
        _all_tickets.clear()
        _pending_index.clear()
        reload_from_disk()
        assert t.id not in _pending_index
        assert get_pending_tickets() == []

    def test_expiry_survives_a_second_reload(self):
        t = create_ticket(tool_name="ctrl_delete")
        self._expire(t.id)
        reload_from_disk()
        reload_from_disk()
        assert _all_tickets[t.id].status == TicketStatus.EXPIRED
        assert get_pending_tickets() == []

    def test_unexpired_ticket_still_pending(self):
        t = create_ticket(tool_name="ctrl_delete")
        reload_from_disk()
        assert [p.id for p in get_pending_tickets()] == [t.id]

    def test_expired_ticket_cannot_be_approved(self):
        t = create_ticket(tool_name="ctrl_delete")
        self._expire(t.id)
        get_pending_tickets()
        assert approve_ticket(t.id) is None
