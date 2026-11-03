"""Contract tests for the per-turn tool batch ledger."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent.harness.tool_batch import ToolBatchLedger, TrackedToolCall
from app.agent.harness.tool_protocol import ToolCallResult


def _prepared(ledger: ToolBatchLedger, call_id: str, tool_name: str = "tool") -> TrackedToolCall:
    key = ledger.register(call_id=call_id, tool_name=tool_name, arguments={"n": call_id}, obs_started_at=1.0)
    return TrackedToolCall(key, call_id, tool_name)


def _ok(prepared: TrackedToolCall, output: str = "done") -> ToolCallResult:
    return ToolCallResult.from_output(call_id=prepared.call_id, name=prepared.tool_name, output=output)


def _no_escapes(prepared: TrackedToolCall, exc: BaseException) -> None:
    raise AssertionError(f"unexpected escaped error for {prepared.call_id}: {exc!r}")


def test_registered_call_persists_as_stopped_until_a_result_arrives():
    ledger = ToolBatchLedger()
    _prepared(ledger, "a", "read_file")

    [call] = ledger.persistable_calls()

    assert call["tool_name"] == "read_file"
    assert json.loads(call["input"]) == {"n": "a"}
    assert call["status"] == "cancelled"
    assert json.loads(call["output"])["reason_code"] == "user_stopped"


def test_repeated_call_ids_get_distinct_keys_after_earlier_calls_finish():
    ledger = ToolBatchLedger()
    first = ledger.register(call_id="same", tool_name="t", arguments={}, obs_started_at=1.0)
    ledger.finish(first)
    second = ledger.register(call_id="same", tool_name="t", arguments={}, obs_started_at=1.0)
    third = ledger.register(call_id="same", tool_name="t", arguments={}, obs_started_at=1.0)

    assert len({first, second, third}) == 3
    assert len(ledger.persistable_calls()) == 2


def test_pending_gate_result_is_not_treated_as_completed():
    ledger = ToolBatchLedger()
    prepared = _prepared(ledger, "gated")
    pending = ToolCallResult.from_output(
        call_id="gated", name="tool", output=json.dumps({"status": "pending_approval"})
    )

    ledger.record_result(prepared.active_key, pending)
    ledger.mark_cancelled(worker_state="stopped", observe=lambda *args: None)

    [call] = ledger.persistable_calls()
    assert call["status"] == "cancelled"
    assert json.loads(call["output"])["worker_state"] == "stopped"


def test_parallel_batch_returns_results_in_model_order():
    ledger = ToolBatchLedger()
    calls = [_prepared(ledger, "slow"), _prepared(ledger, "fast")]

    async def invoke(prepared: TrackedToolCall) -> ToolCallResult:
        await asyncio.sleep(0.02 if prepared.call_id == "slow" else 0)
        result = _ok(prepared, prepared.call_id)
        ledger.record_result(prepared.active_key, result)
        return result

    results = asyncio.run(ledger.run(calls, invoke, parallel=True, report_escaped_error=_no_escapes))

    assert [result.output for result in results] == ["slow", "fast"]
    assert [call["status"] for call in ledger.persistable_calls()] == ["complete", "complete"]


def test_escaped_error_is_isolated_from_its_sibling():
    ledger = ToolBatchLedger()
    calls = [_prepared(ledger, "boom"), _prepared(ledger, "fine")]
    reported: list[tuple[str, str]] = []

    async def invoke(prepared: TrackedToolCall) -> ToolCallResult:
        if prepared.call_id == "boom":
            raise RuntimeError("escaped")
        return _ok(prepared)

    results = asyncio.run(
        ledger.run(
            calls,
            invoke,
            parallel=True,
            report_escaped_error=lambda prepared, exc: reported.append((prepared.call_id, str(exc))),
        )
    )

    assert reported == [("boom", "escaped")]
    assert results[0].status == "error"
    assert json.loads(results[0].output) == {"status": "error", "error": "escaped"}
    assert results[1].output == "done"
    assert ledger.persistable_calls()[0]["status"] == "error"


def test_cancellation_keeps_a_completed_sibling_and_observes_each_call_once():
    ledger = ToolBatchLedger()
    finished, blocked = _prepared(ledger, "finished"), _prepared(ledger, "blocked")
    observed: list[tuple[str, str, str, int | None]] = []

    async def invoke(prepared: TrackedToolCall) -> ToolCallResult:
        if prepared.call_id == "blocked":
            await asyncio.Event().wait()
        result = _ok(prepared, "kept")
        ledger.record_result(prepared.active_key, result)
        return result

    async def scenario() -> None:
        batch = asyncio.create_task(
            ledger.run([finished, blocked], invoke, parallel=True, report_escaped_error=_no_escapes)
        )
        await asyncio.sleep(0.01)
        batch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await batch
        await ledger.cancel_tasks()

    def observe(call, status, output, error_code, duration_ms):
        observed.append((call.call_id, status, error_code, duration_ms))

    asyncio.run(scenario())
    ledger.mark_cancelled(worker_state="stopping", observe=observe)
    ledger.mark_cancelled(worker_state="stopping", observe=observe)

    persisted = {json.loads(call["input"])["n"]: call for call in ledger.persistable_calls()}
    assert persisted["finished"]["output"] == "kept"
    assert persisted["finished"]["status"] == "complete"
    assert persisted["blocked"]["status"] == "cancelled"
    assert json.loads(persisted["blocked"]["output"])["worker_state"] == "stopping"
    assert [(call_id, status, code) for call_id, status, code, _ in observed] == [
        ("finished", "ok", ""),
        ("blocked", "cancelled", "cancelled"),
    ]
    assert observed[0][3] is not None and observed[1][3] is None


def test_cancel_tasks_drains_a_serial_barrier():
    ledger = ToolBatchLedger()
    prepared = _prepared(ledger, "serial")
    cancelled: list[bool] = []

    async def invoke(_prepared: TrackedToolCall) -> ToolCallResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        raise AssertionError("unreachable")

    async def scenario() -> None:
        batch = asyncio.create_task(ledger.run([prepared], invoke, parallel=False, report_escaped_error=_no_escapes))
        await asyncio.sleep(0.01)
        await ledger.cancel_tasks()
        with pytest.raises(asyncio.CancelledError):
            await batch

    asyncio.run(scenario())

    assert cancelled == [True]


def test_parallel_cancel_keeps_completed_sibling_and_marks_cancelled_effect_uncertain(monkeypatch):
    from app.agent.durable import recovery
    from app.agent.durable.operation_execution import prepare_tool_operation
    from app.agent.harness.tool_executor import ToolExecutor
    from app.agent.iteration_budget import IterationBudget

    class FakeDB:
        def __init__(self, journal):
            self.journal = journal

        def fetchone(self, _sql, _params=()):
            if any(status in {"uncertain", "needs_review"} for status in self.journal.statuses.values()):
                return {"1": 1}
            return None

    class Journal:
        def __init__(self):
            self.statuses = {}
            self.operation_ids = {}
            self.db = FakeDB(self)

        def prepare_operation(self, _run_id, *, operation_id, **_kwargs):
            self.statuses[operation_id] = "prepared"
            self.operation_ids[operation_id] = operation_id

        def transition_operation(self, _run_id, operation_id, *, to_status, **_kwargs):
            self.statuses[operation_id] = to_status

    monkeypatch.setattr(recovery, "assert_run_dispatch_allowed", lambda _run: None)
    journal = Journal()
    durable_run = SimpleNamespace(
        journal=journal,
        run_id="run-parallel-cancel",
        owner_instance_id="owner-test",
        attempt=1,
        pause_requested=False,
        lease_lost=False,
    )
    lifecycle = SimpleNamespace(
        pause_requested=False,
        review_requested=False,
        request_review=lambda _reason: setattr(lifecycle, "review_requested", True),
    )
    ledger = ToolBatchLedger()
    completed = _prepared(ledger, "read-call", "calculator")
    interrupted = _prepared(ledger, "write-call", "custom_effect")
    executor = ToolExecutor()
    read_done = asyncio.Event()
    write_started = asyncio.Event()

    async def read_tool():
        read_done.set()
        return "read result"

    async def write_tool():
        write_started.set()
        await asyncio.Event().wait()

    async def invoke(prepared: TrackedToolCall) -> ToolCallResult:
        tool_name = prepared.tool_name
        operation = prepare_tool_operation(
            durable_run,
            tool_name=tool_name,
            trusted_builtin=(tool_name == "calculator"),
            lifecycle=lifecycle,
        )
        tool = {
            "name": tool_name,
            "callable": read_tool if tool_name == "calculator" else write_tool,
            "execution_mode": "async",
        }
        try:
            result = await executor.execute(
                tool_dict=tool,
                arguments={},
                budget=IterationBudget(),
                call_id=prepared.call_id,
                operation_execution=operation,
            )
        except asyncio.CancelledError:
            operation.settle(cancelled=True)
            raise
        operation.settle(result.output)
        ledger.record_result(prepared.active_key, result)
        return result

    async def scenario():
        batch = asyncio.create_task(
            ledger.run([completed, interrupted], invoke, parallel=True, report_escaped_error=_no_escapes)
        )
        await asyncio.wait_for(asyncio.gather(read_done.wait(), write_started.wait()), timeout=1)
        await ledger.cancel_tasks()
        with pytest.raises(asyncio.CancelledError):
            await batch

    try:
        asyncio.run(scenario())
    finally:
        executor.shutdown()

    statuses = list(journal.statuses.values())
    assert "verified" in statuses
    assert "uncertain" in statuses
    assert lifecycle.review_requested
    assert len(set(journal.operation_ids)) == 2
