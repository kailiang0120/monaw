"""Tool execution boundary for agent harnesses."""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.agent.iteration_budget import IterationBudget
from app.agent.tool_cancellation import (
    cancel_call,
    reset_current_call_id,
    set_current_call_id,
    current_call_id,
    set_tool_scope,
    reset_tool_scope,
    finish_cancellation,
    current_tool_scope,
)
from app.agent.harness.tool_protocol import ToolCallResult, ToolStatus
from app.agent.harness.workers import mark_stopping, resource_keys, resource_lock, unfinished_workers
from app.agent.run_context import current_conversation_id

WORKER_STOP_GRACE_SECONDS = 0.25

_BROWSER_TOOL_TIMEOUTS: dict[str, float] = {
    "browser_session": 120.0,
    "browser_open": 120.0,
    # These inspections can be the first browser action and launch Chrome too.
    "browser_snapshot": 120.0,
    "browser_screenshot": 35.0,
    "browser_tabs": 120.0,
}


def _is_browser_tool(tool_name: str) -> bool:
    return tool_name.startswith("browser_")


def tool_timeout_seconds(tool_dict: dict) -> float | None:
    raw_timeout = tool_dict.get("timeout_seconds")
    if raw_timeout is not None:
        try:
            timeout = float(raw_timeout)
            return timeout if timeout > 0 else None
        except (TypeError, ValueError):
            return None
    tool_name = str(tool_dict.get("name") or "")
    if tool_name in _BROWSER_TOOL_TIMEOUTS:
        return _BROWSER_TOOL_TIMEOUTS[tool_name]
    return 30.0 if _is_browser_tool(tool_name) else None


def _status_from_output(output: str, fallback: ToolStatus = "ok") -> ToolStatus:
    try:
        payload = json.loads(output) if isinstance(output, str) else output
    except (json.JSONDecodeError, TypeError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    status = str(payload.get("status") or "").lower()
    if status in {"ok", "error", "blocked", "denied", "failed", "pending_approval", "pending_access_grant"}:
        return status  # type: ignore[return-value]
    return fallback


def _normalized_output(output: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(output) if isinstance(output, str) else output
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


class ToolExecutor:
    def __init__(self) -> None:
        self._stateless_executor = ThreadPoolExecutor(thread_name_prefix="agent-tools")
        self._affinity_executors: dict[str, ThreadPoolExecutor] = {}

    def shutdown(self) -> None:
        self._stateless_executor.shutdown(wait=False, cancel_futures=True)
        for executor in self._affinity_executors.values():
            executor.shutdown(wait=False, cancel_futures=True)
        self._affinity_executors.clear()

    def run_on_affinity(self, affinity_group: str, fn) -> bool:
        executor = self._affinity_executors.get(affinity_group)
        if executor is None:
            return False
        future = executor.submit(fn)
        future.result(timeout=5)
        return True

    def _affinity_executor(self, affinity_group: str) -> ThreadPoolExecutor:
        executor = self._affinity_executors.get(affinity_group)
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"agent-{affinity_group}",
            )
            self._affinity_executors[affinity_group] = executor
        return executor

    async def execute(
        self,
        *,
        tool_dict: dict,
        arguments: dict,
        budget: IterationBudget,
        call_id: str,
    ) -> ToolCallResult:
        fn = tool_dict["callable"]
        execution_mode = tool_dict.get("execution_mode", "sync_stateless")
        tool_name = str(tool_dict.get("name") or "unknown")
        timeout_seconds = tool_timeout_seconds(tool_dict)

        if unfinished_workers(current_conversation_id(), resource_keys(tool_dict)):
            return ToolCallResult.from_output(
                call_id=call_id, name=tool_name,
                output=json.dumps({"status": "blocked", "reason_code": "tool_still_running", "worker_state": "stopping", "error": "A previous tool is still stopping. Wait for it to finish before continuing."}),
            )

        # Standalone callers also need isolation when model call IDs are reused.
        scope_context = None
        if not current_tool_scope():
            scope_context = set_tool_scope(uuid.uuid4().hex)
        call_context = set_current_call_id(call_id)
        cancellation_id = current_call_id()
        try:
            if execution_mode == "async":
                output = await self._run_async(fn, arguments, timeout_seconds, tool_dict)
            else:
                output = await self._run_sync(fn, arguments, tool_dict, timeout_seconds)
            output = str(output)
            status = _status_from_output(output)
            normalized = _normalized_output(output)
            return ToolCallResult(
                call_id=call_id,
                name=tool_name,
                status=status,
                output=output,
                normalized_output=normalized,
                error=str(normalized.get("error") or "") if normalized and status != "ok" else None,
                retryable=status in {"error", "failed", "pending_approval", "pending_access_grant"},
                metadata={"execution_mode": execution_mode},
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            still_running = unfinished_workers(current_conversation_id(), resource_keys(tool_dict))
            output = json.dumps(
                {
                    "status": "error",
                    "code": "tool_timeout",
                    "reason_code": "tool_timeout",
                    "tool": tool_name,
                    "timeout_seconds": timeout_seconds,
                    "error": f"{tool_name} exceeded {timeout_seconds}s timeout.",
                    "worker_state": "stopping" if still_running else "stopped",
                }
            )
            return ToolCallResult(
                call_id=call_id,
                name=tool_name,
                status="error",
                output=output,
                normalized_output=_normalized_output(output),
                error=f"{tool_name} exceeded {timeout_seconds}s timeout.",
                retryable=not still_running,
                metadata={"execution_mode": execution_mode, "timeout_seconds": timeout_seconds},
            )
        except Exception as exc:
            output = json.dumps(
                {
                    "status": "error",
                    "reason_code": getattr(exc, "reason_code", "tool_exception"),
                    "tool": tool_name,
                    "error": str(exc),
                }
            )
            return ToolCallResult(
                call_id=call_id,
                name=tool_name,
                status="error",
                output=output,
                normalized_output=_normalized_output(output),
                error=str(exc),
                retryable=False,
                metadata={"execution_mode": execution_mode},
            )
        finally:
            reset_current_call_id(call_context)
            if scope_context is not None:
                reset_tool_scope(scope_context)

    async def _run_async(self, fn, arguments: dict, timeout_seconds: float | None, tool_dict: dict):
        cancellation_id = current_call_id()
        task = asyncio.create_task(fn(**arguments))
        task.add_done_callback(lambda _task: finish_cancellation(cancellation_id))
        try:
            if timeout_seconds is not None:
                return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
            return await asyncio.shield(task)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            # Signal native cancellation while the coroutine's hook is still
            # registered, then cancel and drain the coroutine itself.
            cancel_call(cancellation_id)
            task.cancel()
            mark_stopping(task, current_conversation_id(), resource_keys(tool_dict))
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=WORKER_STOP_GRACE_SECONDS)
            except (asyncio.CancelledError, Exception):
                pass
            def consume(done):
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(consume)
            if task.done():
                finish_cancellation(cancellation_id)
            raise

    async def _run_sync(self, fn, arguments: dict, tool_dict: dict, timeout_seconds: float | None):
        loop = asyncio.get_running_loop()
        if tool_dict.get("execution_mode") == "sync_thread_affine":
            affinity_group = tool_dict.get("affinity_group") or tool_dict.get("name", "tool")
            executor = self._affinity_executor(str(affinity_group))
        else:
            executor = self._stateless_executor
        cancelled = threading.Event()
        cancellation_id = current_call_id()
        conversation_id = current_conversation_id()
        resources = resource_keys(tool_dict)

        def call():
            acquired = []
            try:
                for resource in sorted(resources):
                    lock = resource_lock(resource)
                    while not lock.acquire(timeout=0.05):
                        if cancelled.is_set():
                            return json.dumps({"status": "cancelled", "reason_code": "user_stopped"})
                    acquired.append(lock)
                if cancelled.is_set():
                    return json.dumps({"status": "cancelled", "reason_code": "user_stopped"})
                return fn(**arguments)
            finally:
                for lock in reversed(acquired):
                    lock.release()

        ctx = contextvars.copy_context()
        worker = executor.submit(ctx.run, call)
        worker.add_done_callback(lambda _future: finish_cancellation(cancellation_id))
        future = asyncio.wrap_future(worker, loop=loop)
        try:
            if timeout_seconds is not None:
                return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_seconds)
            return await asyncio.shield(future)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            cancelled.set()
            cancel_call(cancellation_id)
            worker.cancel()  # Queued work must never start after Stop.
            mark_stopping(worker, conversation_id, resources)
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=WORKER_STOP_GRACE_SECONDS)
            except (asyncio.CancelledError, Exception):
                pass
            # Consume late exceptions even if the request loop has already closed.
            def consume(done):
                if not done.cancelled():
                    done.exception()
            future.add_done_callback(consume)
            if worker.done():
                finish_cancellation(cancellation_id)
            raise
