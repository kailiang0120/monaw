from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.access_grant_broker import (
    _all,
    _pending,
    _resume_decisions as _grant_resume_decisions,
    _resume_events as _grant_resume_events,
    create_grant_ticket,
    signal_resume as signal_grant_resume,
)
from app.agent.approval_broker import (
    TicketStatus,
    _all_tickets,
    _pending_index,
    _resume_decisions as _approval_resume_decisions,
    _resume_events as _approval_resume_events,
    approve_ticket,
    create_ticket,
    signal_resume as signal_approval_resume,
)
from app.agent.execution_gate import ExecutionGateService
from app.agent.execution_resume import _EXECUTORS, register_executor
from app.agent.iteration_budget import IterationBudget
from app.agent.tool_registry import ToolRegistry


@pytest.fixture(autouse=True)
def _clean_gate_state(tmp_path, monkeypatch):
    import app.agent.approval_broker as approval_mod

    monkeypatch.setattr(approval_mod, "_TICKETS_FILE", tmp_path / "tickets.jsonl")
    monkeypatch.setattr(approval_mod, "_APPROVAL_LOG", tmp_path / "approval_log.md")
    _all_tickets.clear()
    _pending_index.clear()
    _approval_resume_events.clear()
    _approval_resume_decisions.clear()
    _pending.clear()
    _all.clear()
    _grant_resume_events.clear()
    _grant_resume_decisions.clear()
    _EXECUTORS.clear()
    yield
    _all_tickets.clear()
    _pending_index.clear()
    _approval_resume_events.clear()
    _approval_resume_decisions.clear()
    _pending.clear()
    _all.clear()
    _grant_resume_events.clear()
    _grant_resume_decisions.clear()
    _EXECUTORS.clear()


async def _collect_events(generator):
    return [event async for event in generator]


async def _fake_execute_tool(_budget, tool_dict, arguments):
    return json.dumps(
        {
            "status": "ok",
            "tool": tool_dict.get("name"),
            "arguments": arguments,
        }
    )


def test_check_pending_status_parses_approval_and_access_grant():
    approval = ExecutionGateService.check_pending_status(
        json.dumps({"status": "pending_approval", "ticket_id": "t1", "action": "delete", "reason": "r"})
    )
    grant = ExecutionGateService.check_pending_status(
        {
            "status": "pending_access_grant",
            "ticket_id": "g1",
            "target_type": "path",
            "target_identifier": "C:/tmp",
            "display_name": "tmp",
            "action_context": "read",
        }
    )

    assert approval == {
        "event": "approval_required",
        "data": {"ticket_id": "t1", "action": "delete", "reason": "r"},
    }
    assert grant["event"] == "access_grant_required"
    assert grant["data"]["target_identifier"] == "C:/tmp"
    assert ExecutionGateService.check_pending_status("not json") is None


def test_handle_pending_tool_output_returns_pending_event_for_unknown_tool():
    service = ExecutionGateService()
    pending_output = json.dumps({"status": "pending_approval", "ticket_id": "missing"})

    resolved_output, events = asyncio.run(
        service.handle_pending_tool_output(
            tool_output=pending_output,
            tool_name="unknown_tool",
            arguments={},
            registry=ToolRegistry(),
            budget=IterationBudget(max_iterations=1),
            execute_tool=_fake_execute_tool,
        )
    )

    assert resolved_output == pending_output
    assert events == [{"event": "approval_required", "data": {"ticket_id": "missing", "action": "", "reason": ""}}]


def test_await_ticket_resolution_returns_denied_when_user_rejects():
    service = ExecutionGateService()
    signal_approval_resume("ticket-denied", "rejected")

    events = asyncio.run(
        _collect_events(
            service.await_ticket_resolution(
                budget=IterationBudget(max_iterations=1),
                ticket_id="ticket-denied",
                event_kind="approval_required",
                tool_dict={"name": "danger_tool"},
                arguments={},
                execute_tool=_fake_execute_tool,
            )
        )
    )

    result = json.loads(events[-1]["_result"])
    assert result["status"] == "denied"


def test_await_ticket_resolution_times_out_safely():
    service = ExecutionGateService(default_timeout_seconds=0)

    events = asyncio.run(
        _collect_events(
            service.await_ticket_resolution(
                budget=IterationBudget(max_iterations=1),
                ticket_id="never-signaled",
                event_kind="approval_required",
                tool_dict={"name": "danger_tool"},
                arguments={},
                execute_tool=_fake_execute_tool,
            )
        )
    )

    result = json.loads(events[-1]["_result"])
    assert result["status"] == "error"
    assert "timed out" in result["error"]


def test_await_ticket_resolution_replays_approved_ticket_payload():
    service = ExecutionGateService()
    register_executor("danger_tool", lambda input_str: json.dumps({"status": "ok", "input": json.loads(input_str)}))
    ticket = create_ticket(
        tool_name="danger_tool",
        payload={"input_str": json.dumps({"path": "C:/tmp/file.txt"})},
    )
    approve_ticket(ticket.id)
    signal_approval_resume(ticket.id, "approved")

    events = asyncio.run(
        _collect_events(
            service.await_ticket_resolution(
                budget=IterationBudget(max_iterations=1),
                ticket_id=ticket.id,
                event_kind="approval_required",
                tool_dict={"name": "danger_tool"},
                arguments={},
                execute_tool=_fake_execute_tool,
            )
        )
    )

    resumed = json.loads(events[-1]["_result"])
    assert ticket.status == TicketStatus.APPLIED
    assert resumed == {"status": "ok", "input": {"path": "C:/tmp/file.txt"}}
    assert events[0]["event"] == "tool_resumed"


def test_await_ticket_resolution_reruns_tool_after_access_grant():
    service = ExecutionGateService()
    ticket = create_grant_ticket(
        target_type="path",
        target_identifier="C:/tmp",
        action_context="read",
    )
    ticket.status = "granted"
    signal_grant_resume(ticket.id, "once")

    events = asyncio.run(
        _collect_events(
            service.await_ticket_resolution(
                budget=IterationBudget(max_iterations=1),
                ticket_id=ticket.id,
                event_kind="access_grant_required",
                tool_dict={"name": "read_tool"},
                arguments={"path": "C:/tmp/file.txt"},
                execute_tool=_fake_execute_tool,
            )
        )
    )

    resumed = json.loads(events[-1]["_result"])
    assert resumed == {
        "status": "ok",
        "tool": "read_tool",
        "arguments": {"path": "C:/tmp/file.txt"},
    }
    assert events[0]["event"] == "tool_resumed"
