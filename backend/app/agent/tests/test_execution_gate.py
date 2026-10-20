from __future__ import annotations

import asyncio
import json
import threading

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


def test_approved_replay_stop_keeps_partial_result_and_call_scope():
    from app.agent.harness.tool_executor import ToolExecutor
    from app.agent.tool_cancellation import current_call_id, set_tool_scope, reset_tool_scope, check_cancelled, cancellable_sleep, ToolCancelled
    started = threading.Event()
    finished = threading.Event()
    calls = []
    identities = []
    def replay(payload):
        identities.append(current_call_id())
        calls.append(json.loads(payload)["first"])
        started.set()
        try:
            while True:
                cancellable_sleep(0.01)
                check_cancelled()
        except ToolCancelled:
            finished.set()
            return json.dumps({"status": "cancelled", "completed_actions": 1, "results": [{"status": "ok"}], "retryable": False})
    register_executor("computer_functions_test", replay)
    ticket = create_ticket(tool_name="computer_functions_test", payload={"input_str": '{"first":"applied"}'})
    approve_ticket(ticket.id)
    executor = ToolExecutor()
    service = ExecutionGateService(executor=executor)
    async def run():
        scope = set_tool_scope("approval-turn")
        try:
            task = asyncio.create_task(service._resume_approved_ticket(ticket.id,
                tool_dict={"name": "computer_functions_test", "metadata": {"resource_locks": ["desktop"]}},
                call_id="original-call"))
            for _ in range(200):
                if started.is_set(): break
                await asyncio.sleep(0.005)
            assert started.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as stopped:
                await task
            assert finished.is_set()
            assert json.loads(stopped.value.tool_output)["completed_actions"] == 1
        finally:
            reset_tool_scope(scope)
    try:
        asyncio.run(run())
    finally:
        executor.shutdown()
    assert calls == ["applied"]
    assert identities == ["approval-turn:original-call"]
    assert ticket.status == TicketStatus.FAILED
    assert json.loads(ticket.execution_result)["completed_actions"] == 1


def test_approved_replay_respects_desktop_lock():
    from app.agent.harness.tool_executor import ToolExecutor
    from app.agent.harness.workers import resource_lock
    started = threading.Event()
    register_executor("computer_functions_test", lambda _: started.set() or '{"status":"ok"}')
    ticket = create_ticket(tool_name="computer_functions_test", payload={"input_str": "{}"})
    approve_ticket(ticket.id)
    executor = ToolExecutor()
    lock = resource_lock("desktop")
    async def run():
        lock.acquire()
        try:
            task = asyncio.create_task(ExecutionGateService(executor=executor)._resume_approved_ticket(ticket.id,
                tool_dict={"name": "computer_functions_test", "metadata": {"resource_locks": ["desktop"]}}, call_id="locked-call"))
            await asyncio.sleep(0.05)
            assert not started.is_set()
        finally:
            lock.release()
        output = await asyncio.wait_for(task, timeout=2)
        assert json.loads(output)["status"] == "ok"
    try:
        asyncio.run(run())
    finally:
        executor.shutdown()
    assert started.is_set()


def test_failed_approved_replay_preserves_structured_verification_failure():
    result = {"status": "error", "reason_code": "verification_failed", "completed_actions": 1,
              "results": [{"status": "ok"}], "retryable": False}
    register_executor("computer_functions_test", lambda _: json.dumps(result))
    ticket = create_ticket(tool_name="computer_functions_test", payload={"input_str": "{}"})
    approve_ticket(ticket.id)
    output = asyncio.run(ExecutionGateService()._resume_approved_ticket(ticket.id))
    assert json.loads(output) == result
    assert ticket.status == TicketStatus.FAILED


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


@pytest.mark.parametrize("response,should_run", [
    ('{"decision":"approve","reason":"Creates the requested report."}', True),
    ('{"decision":"ask","reason":"The destination is unclear."}', False),
    ('not json', False),
    ('{"decision":"approve"}', False),
])
def test_auto_review_only_runs_exact_validated_approved_actions(monkeypatch, response, should_run):
    import app.agent.execution_gate as gate
    from app.agent.settings_store import AgentSettings
    settings = AgentSettings()
    settings.permissions.mode = "auto_review"
    monkeypatch.setattr(gate, "load_agent_settings", lambda: settings)
    calls = []
    register_executor("write_report", lambda payload: calls.append(json.loads(payload)) or '{"status":"ok"}')
    ticket = create_ticket(tool_name="write_report", interactive=True,
                           payload={"input_str": '{"path":"report.txt"}'})
    class Reviewer:
        async def chat(self, **kwargs):
            assert "report.txt" in kwargs["messages"][0]["content"]
            return response
    service = ExecutionGateService(default_timeout_seconds=0.01)
    events = []
    result = asyncio.run(service.resolve_pending_output(
        tool_output=json.dumps({"status": "pending_approval", "ticket_id": ticket.id}),
        tool_dict={"name": "write_report"}, arguments={}, budget=IterationBudget(max_iterations=1),
        execute_tool=_fake_execute_tool, emit=events.append, review_client=Reviewer(),
        user_request="Create report.txt with my report."))
    assert bool(calls) is should_run
    if should_run:
        assert calls == [{"path": "report.txt"}]
        assert ticket.resolved_by == "auto_review"
        assert ticket.status == TicketStatus.APPLIED
        assert json.loads(result)["status"] == "ok"
    else:
        assert ticket.status == TicketStatus.PENDING
    assert events[0]["data"]["review_reason"]
    assert events[0]["event"] == ("approval_resolved" if should_run else "approval_required")


def test_auto_review_cannot_approve_a_changed_payload(monkeypatch):
    import app.agent.execution_gate as gate
    from app.agent.settings_store import AgentSettings
    settings = AgentSettings()
    settings.permissions.mode = "auto_review"
    monkeypatch.setattr(gate, "load_agent_settings", lambda: settings)
    ticket = create_ticket(tool_name="write_report", interactive=True, payload={"input_str": "{}"})
    class Reviewer:
        async def chat(self, **_kwargs):
            ticket.payload["input_str"] = '{"path":"different.txt"}'
            ticket.payload_hash = ticket.compute_hash()
            return '{"decision":"approve","reason":"Requested."}'
    result = asyncio.run(ExecutionGateService(default_timeout_seconds=0).resolve_pending_output(
        tool_output=json.dumps({"status":"pending_approval", "ticket_id":ticket.id}),
        tool_dict={"name":"write_report"}, arguments={}, budget=IterationBudget(max_iterations=1),
        execute_tool=_fake_execute_tool, emit=lambda _event: None, review_client=Reviewer(), user_request="Create a report."))
    assert ticket.status == TicketStatus.PENDING
    assert json.loads(result)["status"] == "error"


def test_auto_review_denial_retires_ticket_without_execution_and_persists_reason(monkeypatch):
    import app.agent.approval_broker as broker
    import app.agent.execution_gate as gate
    from app.agent.approval_review import AutoReviewState
    from app.agent.settings_store import AgentSettings
    settings = AgentSettings()
    settings.permissions.mode = "auto_review"
    monkeypatch.setattr(gate, "load_agent_settings", lambda: settings)
    calls = []
    register_executor("send", lambda payload: calls.append(payload) or '{"status":"ok"}')
    class Reviewer:
        async def chat(self, **kwargs):
            return '{"decision":"deny","reason":"The user prohibited sending this report."}'
    state = AutoReviewState()
    events = []
    ticket = create_ticket(tool_name="send", payload={"input_str": "{}"})
    def resolve(target):
        return asyncio.run(ExecutionGateService(default_timeout_seconds=0).resolve_pending_output(
            tool_output=json.dumps({"status": "pending_approval", "ticket_id": target.id}),
            tool_dict={"name": "send"}, arguments={"recipient": "someone"}, budget=IterationBudget(max_iterations=1),
            execute_tool=_fake_execute_tool, emit=events.append, review_client=Reviewer(),
            user_request="Draft this report; do not send it.", review_state=state))
    assert json.loads(resolve(ticket))["reason_code"] == "auto_review_denied"
    assert ticket.status == TicketStatus.REJECTED
    assert broker.get_pending_tickets() == []
    assert events[0]["event"] == "approval_resolved"
    assert events[0]["data"]["decision"] == "deny"
    assert not calls
    broker._load_tickets_from_disk()
    assert broker.get_ticket(ticket.id).review_decision == "deny"
    assert "prohibited" in broker.get_ticket(ticket.id).review_reason
    retry = create_ticket(tool_name="send", payload={"input_str": "{}"})
    assert json.loads(resolve(retry))["reason_code"] == "auto_review_retry_blocked"
    assert retry.status == TicketStatus.REJECTED
    assert len(state.recent) == 1  # No second model review of an identical denied action.
    assert not calls


@pytest.mark.parametrize("change", ["target", "identity", "mode", "human_rejected", "expiry", "steering"])
def test_auto_review_revalidates_all_action_context_before_execution(monkeypatch, change):
    import app.agent.execution_gate as gate
    from app.agent.approval_broker import reject_ticket
    from app.agent.settings_store import AgentSettings
    settings = AgentSettings()
    settings.permissions.mode = "auto_review"
    monkeypatch.setattr(gate, "load_agent_settings", lambda: settings)
    ticket = create_ticket(tool_name="write_report", payload={"input_str": "{}"})
    calls = []
    register_executor("write_report", lambda payload: calls.append(payload) or '{"status":"ok"}')
    class Reviewer:
        async def chat(self, **kwargs):
            if change == "target":
                ticket.target_path = "different.txt"
            elif change == "identity":
                ticket.conversation_id = "other"
            elif change == "mode":
                settings.permissions.mode = "default"
            elif change == "human_rejected":
                reject_ticket(ticket.id)
                signal_approval_resume(ticket.id, "rejected")
            elif change == "expiry":
                ticket.expires_at = "2000-01-01T00:00:00+00:00"
            return '{"decision":"approve","reason":"Requested."}'
    output = asyncio.run(ExecutionGateService(default_timeout_seconds=0.01).resolve_pending_output(
        tool_output=json.dumps({"status": "pending_approval", "ticket_id": ticket.id}),
        tool_dict={"name": "write_report"}, arguments={}, budget=IterationBudget(max_iterations=1),
        execute_tool=_fake_execute_tool, emit=lambda event: None, review_client=Reviewer(), user_request="Create the report",
        review_is_current=lambda: change != "steering"))
    assert not calls
    assert json.loads(output)["status"] in {"error", "denied", "cancelled"}
    assert ticket.status != TicketStatus.APPLIED
    assert ticket.review_decision != "approve"


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


@pytest.mark.parametrize("timeout", [0, 0.01, 600])
def test_await_ticket_resolution_replays_approved_ticket_payload(timeout):
    service = ExecutionGateService(default_timeout_seconds=timeout)
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
