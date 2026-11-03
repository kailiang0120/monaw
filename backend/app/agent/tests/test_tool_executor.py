import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from app.agent.harness.tool_executor import ToolExecutor
from app.agent.iteration_budget import IterationBudget


def test_async_returned_result_survives_cancellation_before_wrapper_resumes():
    executor = ToolExecutor()

    async def run():
        invocation = None

        async def tool():
            # Cancel the awaiting wrapper in the same loop turn as the actual
            # return. No sleep or assumption about OS timer precision is needed.
            invocation.cancel()
            return '{"status":"ok","value":"captured"}'

        invocation = asyncio.create_task(executor.execute(
            tool_dict={"name": "fixture", "callable": tool, "execution_mode": "async"},
            arguments={}, budget=IterationBudget(), call_id="returned-before-stop",
        ))
        with pytest.raises(asyncio.CancelledError) as interrupted:
            await invocation
        assert json.loads(interrupted.value.tool_output) == {"status": "ok", "value": "captured"}

    try:
        asyncio.run(run())
    finally:
        executor.shutdown()


def test_partial_failure_cannot_be_marked_retryable():
    from app.agent.harness.tool_protocol import ToolCallResult
    output = json.dumps({"status": "error", "reason_code": "verification_failed", "completed_actions": 1, "retryable": False})
    parsed = ToolCallResult.from_output(call_id="call", name="desktop", output=output)
    assert not parsed.retryable
    executor = ToolExecutor()
    try:
        result = asyncio.run(executor.execute(tool_dict={"name": "desktop", "callable": lambda: output},
            arguments={}, budget=IterationBudget(), call_id="call"))
        assert not result.retryable
        assert result.normalized_output["completed_actions"] == 1
    finally:
        executor.shutdown()


def test_native_timeout_retains_partial_input_and_prevents_retry():
    from app.agent.tool_cancellation import cancellable_sleep, ToolCancelled
    def batch():
        try:
            while True:
                cancellable_sleep(0.01)
        except ToolCancelled:
            return '{"status":"cancelled","completed_actions":1,"retryable":false}'
    executor = ToolExecutor()
    try:
        result = asyncio.run(executor.execute(tool_dict={"name": "desktop", "callable": batch,
            "timeout_seconds": 0.02, "metadata": {"resource_locks": ["desktop"], "mutates_state": True}},
            arguments={}, budget=IterationBudget(), call_id="timed-batch"))
        assert result.normalized_output["reason_code"] == "tool_timeout"
        assert result.normalized_output["partial_result"]["completed_actions"] == 1
        assert not result.retryable
    finally:
        executor.shutdown()


def test_sync_timeout_blocks_conflicting_work_across_runtimes_until_worker_exits(monkeypatch):
    import app.agent.harness.tool_executor as executor_module
    from app.agent.harness.workers import unfinished_workers
    from app.agent.run_context import set_current_conversation_id, reset_current_conversation_id
    monkeypatch.setattr(executor_module, "WORKER_STOP_GRACE_SECONDS", 0.01)
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    actions = []

    def slow_tool():
        started.set()
        release.wait(timeout=2)
        actions.append("original")
        completed.set()
        return "ok"

    tool = {"name": "change", "callable": slow_tool, "timeout_seconds": 0.02, "metadata": {"resource_locks": ["audit-resource"]}}
    first, second = ToolExecutor(), ToolExecutor()
    async def run():
        token = set_current_conversation_id("worker-audit")
        try:
            result = await first.execute(tool_dict=tool, arguments={}, budget=IterationBudget(), call_id="same")
            assert started.is_set()
            assert result.status == "error" and result.retryable is False
            assert json.loads(result.output)["worker_state"] == "stopping"
            follow_up = {**tool, "callable": lambda: actions.append("follow-up") or "ok"}
            blocked = await second.execute(tool_dict=follow_up, arguments={}, budget=IterationBudget(), call_id="same")
            assert blocked.status == "blocked"
            assert actions == []
            # Another chat must also wait when it targets the same resource.
            other = set_current_conversation_id("worker-other-chat")
            try:
                blocked = await second.execute(tool_dict=follow_up, arguments={}, budget=IterationBudget(), call_id="same")
                assert blocked.status == "blocked"
            finally:
                reset_current_conversation_id(other)
            release.set()
            for _ in range(100):
                if not unfinished_workers("worker-audit"):
                    break
                await asyncio.sleep(0.005)
            assert completed.is_set()
            success = await second.execute(tool_dict=follow_up, arguments={}, budget=IterationBudget(), call_id="same")
            assert success.status == "ok"
            assert actions == ["original", "follow-up"]
        finally:
            release.set()
            reset_current_conversation_id(token)
    try:
        asyncio.run(run())
    finally:
        first.shutdown()
        second.shutdown()


def test_cancellation_with_reused_call_ids_only_reaches_its_own_worker():
    from app.agent.tool_cancellation import current_call_id, register_cancellation, unregister_cancellation
    callbacks = {}
    ready = asyncio.Event()
    executor = ToolExecutor()

    async def waiting_tool(label):
        callback = threading.Event()
        callbacks[label] = callback
        token = current_call_id()
        register_cancellation(token, callback.set)
        if len(callbacks) == 2:
            ready.set()
        try:
            await asyncio.sleep(100)
        finally:
            unregister_cancellation(token)

    async def run():
        def start(label):
            return asyncio.create_task(executor.execute(tool_dict={"name": "wait", "callable": waiting_tool, "execution_mode": "async"}, arguments={"label": label}, budget=IterationBudget(), call_id="same-model-id"))
        first, second = start("a"), start("b")
        try:
            await asyncio.wait_for(ready.wait(), timeout=1)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            assert callbacks["a"].is_set()
            assert not callbacks["b"].is_set()
            assert not second.done()
        finally:
            second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
    try:
        asyncio.run(run())
    finally:
        executor.shutdown()


def test_tool_executor_preserves_exception_reason_code():
    class ReasonedError(RuntimeError):
        reason_code = "system_attach_failed"

    async def failing_tool():
        raise ReasonedError("Chrome exited")

    executor = ToolExecutor()
    try:
        result = asyncio.run(
            executor.execute(
                tool_dict={"name": "mcp__chrome-devtools__new_page", "callable": failing_tool, "execution_mode": "async"},
                arguments={},
                budget=IterationBudget(),
                call_id="call-1",
            )
        )
    finally:
        executor.shutdown()

    payload = json.loads(result.output)
    assert result.status == "error"
    assert payload["reason_code"] == "system_attach_failed"
    assert payload["tool"] == "mcp__chrome-devtools__new_page"
    assert payload["error"] == "Chrome exited"


def test_durable_journal_failure_before_handler_entry_has_no_effect(monkeypatch):
    from app.agent.durable import recovery
    from app.agent.durable.operation_execution import prepare_tool_operation

    class FakeDB:
        def fetchone(self, *_args):
            return None

    class Journal:
        db = FakeDB()

        def __init__(self):
            self.events = []

        def prepare_operation(self, run_id, **kwargs):
            self.events.append(("prepared", run_id, kwargs["operation_id"]))

        def transition_operation(self, run_id, operation_id, *, to_status, **_kwargs):
            self.events.append((to_status, run_id, operation_id))
            if to_status == "executing":
                raise OSError("journal unavailable before effect")

    monkeypatch.setattr(recovery, "assert_run_dispatch_allowed", lambda _run: None)
    journal = Journal()
    durable_run = SimpleNamespace(
        journal=journal,
        run_id="run-test",
        owner_instance_id="owner-test",
        attempt=1,
        pause_requested=False,
        lease_lost=False,
    )
    lifecycle = SimpleNamespace(pause_requested=False, review_requested=False)
    operation = prepare_tool_operation(
        durable_run,
        tool_name="custom_effect",
        trusted_builtin=False,
        lifecycle=lifecycle,
    )
    called = []
    executor = ToolExecutor()
    try:
        result = asyncio.run(
            executor.execute(
                tool_dict={"name": "custom_effect", "callable": lambda: called.append(True) or "done"},
                arguments={},
                budget=IterationBudget(),
                call_id="call-journal-fail",
                operation_execution=operation,
            )
        )
    finally:
        executor.shutdown()

    assert called == []
    assert result.status == "error"
    assert [event[0] for event in journal.events] == ["prepared", "authorized", "executing"]


def test_approved_resume_reuses_the_prepared_operation_context(monkeypatch):
    from app.agent.durable import recovery
    from app.agent.durable.operation_execution import prepare_tool_operation

    class FakeDB:
        def fetchone(self, *_args):
            return None

    class Journal:
        db = FakeDB()

        def __init__(self):
            self.operation_id = ""
            self.transitions = []

        def prepare_operation(self, _run_id, *, operation_id, **_kwargs):
            self.operation_id = operation_id

        def transition_operation(self, _run_id, operation_id, *, to_status, **_kwargs):
            assert operation_id == self.operation_id
            self.transitions.append(to_status)

    monkeypatch.setattr(recovery, "assert_run_dispatch_allowed", lambda _run: None)
    journal = Journal()
    durable_run = SimpleNamespace(
        journal=journal,
        run_id="run-approval",
        owner_instance_id="owner-test",
        attempt=1,
        pause_requested=False,
        lease_lost=False,
    )
    lifecycle = SimpleNamespace(pause_requested=False, review_requested=False)
    operation = prepare_tool_operation(
        durable_run,
        tool_name="fs_write",
        trusted_builtin=True,
        lifecycle=lifecycle,
    )
    executor = ToolExecutor()
    try:
        pending = asyncio.run(
            executor.execute(
                tool_dict={"name": "fs_write", "callable": lambda: json.dumps({
                    "status": "pending_approval", "ticket_id": "ticket-1",
                })},
                arguments={},
                budget=IterationBudget(),
                call_id="call-approved-write",
                operation_execution=operation,
            )
        )
        assert operation.note_pending_preflight(pending.output)

        resumed = asyncio.run(
            executor.execute(
                tool_dict={"name": "fs_write", "callable": lambda: json.dumps({"status": "ok"})},
                arguments={},
                budget=IterationBudget(),
                call_id="call-approved-write",
                operation_execution=operation,
            )
        )
        operation.settle(resumed.output)
    finally:
        executor.shutdown()

    assert pending.status == "pending_approval"
    assert resumed.status == "ok"
    assert journal.transitions == ["authorized", "executing", "returned"]
    assert journal.operation_id.startswith("op-")


def test_post_effect_journal_failure_blocks_the_next_dispatch(monkeypatch):
    from app.agent.durable import recovery
    from app.agent.durable.operation_execution import (
        OperationDispatchBlocked,
        prepare_tool_operation,
    )

    class FakeDB:
        def fetchone(self, *_args):
            return None

    class Journal:
        db = FakeDB()

        def prepare_operation(self, _run_id, **_kwargs):
            return None

        def transition_operation(self, _run_id, _operation_id, *, to_status, **_kwargs):
            if to_status == "returned":
                raise OSError("could not persist returned operation")

    monkeypatch.setattr(recovery, "assert_run_dispatch_allowed", lambda _run: None)
    lifecycle = SimpleNamespace(
        pause_requested=False,
        review_requested=False,
        request_review=lambda _reason: setattr(lifecycle, "review_requested", True),
    )
    durable_run = SimpleNamespace(
        journal=Journal(),
        run_id="run-settle-failure",
        owner_instance_id="owner-test",
        attempt=1,
        pause_requested=False,
        lease_lost=False,
    )
    first_effects = []
    second_effects = []
    operation = prepare_tool_operation(
        durable_run,
        tool_name="custom_effect",
        lifecycle=lifecycle,
    )
    executor = ToolExecutor()
    try:
        result = asyncio.run(
            executor.execute(
                tool_dict={
                    "name": "custom_effect",
                    "callable": lambda: first_effects.append("ran") or json.dumps({"status": "ok"}),
                },
                arguments={},
                budget=IterationBudget(),
                call_id="call-first-effect",
                operation_execution=operation,
            )
        )
        assert result.status == "ok"
        with pytest.raises(OSError):
            operation.settle(result.output)
        try:
            prepare_tool_operation(
                durable_run,
                tool_name="second_effect",
                lifecycle=lifecycle,
            )
        except OperationDispatchBlocked:
            pass
        else:
            pytest.fail("a later operation was prepared after post-effect journal failure")
        assert lifecycle.review_requested
        assert second_effects == []
    finally:
        executor.shutdown()

    assert first_effects == ["ran"]


def test_read_only_classification_requires_bundled_tool_provenance():
    from app.agent.durable.operation_policy import classify_tool

    assert classify_tool("fs_read", trusted_builtin=True).effect_class == "read_only"
    assert classify_tool("fs_read", trusted_builtin=False).effect_class == "opaque_effect"
    assert classify_tool("user_defined_reader", trusted_builtin=True).effect_class == "opaque_effect"
    assert classify_tool("fs_read", trusted_builtin=True).operation_kind == "other"
