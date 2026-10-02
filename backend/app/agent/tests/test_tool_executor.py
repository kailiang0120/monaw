import asyncio
import json
import threading

from app.agent.harness.tool_executor import ToolExecutor
from app.agent.iteration_budget import IterationBudget


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
        reason_code = "managed_chrome_exited"

    async def failing_tool():
        raise ReasonedError("Chrome exited")

    executor = ToolExecutor()
    try:
        result = asyncio.run(
            executor.execute(
                tool_dict={"name": "browser_open", "callable": failing_tool, "execution_mode": "async"},
                arguments={},
                budget=IterationBudget(),
                call_id="call-1",
            )
        )
    finally:
        executor.shutdown()

    payload = json.loads(result.output)
    assert result.status == "error"
    assert payload["reason_code"] == "managed_chrome_exited"
    assert payload["tool"] == "browser_open"
    assert payload["error"] == "Chrome exited"
