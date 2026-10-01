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
    get_pending_grants,
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
from app.agent.execution_gate import ExecutionGateService, _format_timeout
from app.agent.execution_resume import _EXECUTORS, register_executor
from app.agent.iteration_budget import IterationBudget


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


def _resolve_pending(service: ExecutionGateService, tool_output: str, emitted: list[dict] | None = None) -> str:
    return asyncio.run(
        service.resolve_pending_output(
            tool_output=tool_output,
            tool_dict={"name": "looping_tool"},
            arguments={},
            budget=IterationBudget(max_iterations=1),
            execute_tool=_fake_execute_tool,
            emit=(emitted if emitted is not None else []).append,
        )
    )


def test_resolve_pending_output_emits_prompt_before_waiting():
    service = ExecutionGateService()
    emitted: list[dict] = []
    seen_at_wait: list[list[dict]] = []

    async def resolves_after_prompt(**_kwargs):
        seen_at_wait.append(list(emitted))
        yield {"event": "tool_resumed", "data": {}}
        yield {"_result": json.dumps({"status": "ok"})}

    service.await_ticket_resolution = resolves_after_prompt  # type: ignore[method-assign]

    output = _resolve_pending(service, json.dumps({"status": "pending_approval", "ticket_id": "t1"}), emitted)

    assert json.loads(output) == {"status": "ok"}
    assert seen_at_wait == [[{"event": "approval_required", "data": {"ticket_id": "t1", "action": "", "reason": ""}}]]
    assert [event["event"] for event in emitted] == ["approval_required", "tool_resumed"]


def test_resolve_pending_output_passes_through_non_pending_output():
    output = json.dumps({"status": "ok", "value": 1})

    assert _resolve_pending(ExecutionGateService(), output) == output


def test_resolve_pending_output_errors_when_gate_hops_are_exhausted():
    service = ExecutionGateService()

    async def always_pending(**_kwargs):
        yield {"_result": json.dumps({"status": "pending_approval", "ticket_id": "loop"})}

    service.await_ticket_resolution = always_pending  # type: ignore[method-assign]

    result = json.loads(_resolve_pending(service, json.dumps({"status": "pending_approval", "ticket_id": "loop"})))

    assert result["status"] == "error"
    assert result["reason_code"] == "gate_hop_limit_exceeded"


def test_gate_hop_exhaustion_rejects_trailing_approval_ticket():
    service = ExecutionGateService()
    ticket = create_ticket(tool_name="looping_tool")

    async def always_pending(**_kwargs):
        yield {"_result": json.dumps({"status": "pending_approval", "ticket_id": ticket.id})}

    service.await_ticket_resolution = always_pending  # type: ignore[method-assign]

    _resolve_pending(service, json.dumps({"status": "pending_approval", "ticket_id": ticket.id}))

    assert ticket.status == TicketStatus.REJECTED


def test_gate_hop_exhaustion_denies_trailing_access_grant():
    service = ExecutionGateService()
    ticket = create_grant_ticket(target_type="path", target_identifier="C:/private")

    async def always_pending(**_kwargs):
        yield {"_result": json.dumps({"status": "pending_access_grant", "ticket_id": ticket.id})}

    service.await_ticket_resolution = always_pending  # type: ignore[method-assign]

    _resolve_pending(service, json.dumps({"status": "pending_access_grant", "ticket_id": ticket.id}))

    assert ticket.status == "denied"


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


def test_noninteractive_approval_is_auto_denied_without_waiting():
    service = ExecutionGateService(default_timeout_seconds=600)
    ticket = create_ticket(tool_name="danger_tool", interactive=False, payload={"input_str": "{}"})

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

    assert json.loads(events[-1]["_result"])["status"] == "denied"
    assert ticket.status == TicketStatus.REJECTED


def test_noninteractive_access_grant_is_auto_denied_without_waiting():
    service = ExecutionGateService(default_timeout_seconds=600)
    ticket = create_grant_ticket(
        target_type="path",
        target_identifier="C:/private",
        interactive=False,
    )

    events = asyncio.run(
        _collect_events(
            service.await_ticket_resolution(
                budget=IterationBudget(max_iterations=1),
                ticket_id=ticket.id,
                event_kind="access_grant_required",
                tool_dict={"name": "read_tool"},
                arguments={"path": "C:/private/file.txt"},
                execute_tool=_fake_execute_tool,
            )
        )
    )

    assert json.loads(events[-1]["_result"])["status"] == "denied"
    assert ticket.status == "denied"


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


def test_timeout_text_uses_the_configured_duration():
    assert _format_timeout(90) == "90 seconds"
    assert _format_timeout(60) == "1 minute"


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


def test_timed_out_access_grant_wait_retires_the_ticket():
    service = ExecutionGateService(default_timeout_seconds=0)
    ticket = create_grant_ticket(target_type="path", target_identifier="C:/private", interactive=True)

    asyncio.run(
        _collect_events(
            service.await_ticket_resolution(
                budget=IterationBudget(max_iterations=1),
                ticket_id=ticket.id,
                event_kind="access_grant_required",
                tool_dict={"name": "read_tool"},
                arguments={},
                execute_tool=_fake_execute_tool,
            )
        )
    )

    assert ticket.status == "expired"
    assert get_pending_grants() == []


def test_pending_grants_only_lists_live_tickets_for_the_requesting_session():
    mine = create_grant_ticket(target_type="path", target_identifier="C:/mine", control_session_id="session-a")
    stale = create_grant_ticket(target_type="path", target_identifier="C:/stale", control_session_id="session-a")
    stale.expires_at = "2000-01-01T00:00:00+00:00"
    create_grant_ticket(target_type="path", target_identifier="C:/theirs", control_session_id="session-b")

    assert [ticket.id for ticket in get_pending_grants(control_session_id="session-a")] == [mine.id]
    assert stale.status == "expired"
