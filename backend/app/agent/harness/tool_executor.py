"""Tool execution boundary for agent harnesses."""

from __future__ import annotations

import asyncio
import contextvars
import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from app.agent.iteration_budget import IterationBudget
from app.agent.harness.tool_protocol import ToolCallResult, ToolStatus

_BROWSER_TOOL_TIMEOUTS: dict[str, float] = {
    "browser_session": 45.0,
    "browser_open": 60.0,
    "browser_snapshot": 35.0,
    "browser_screenshot": 35.0,
    "browser_tabs": 20.0,
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

        budget.set_current_tool(tool_name)
        try:
            if execution_mode == "async":
                output = await self._run_async(fn, arguments, timeout_seconds)
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
        except asyncio.TimeoutError:
            output = json.dumps(
                {
                    "status": "error",
                    "code": "tool_timeout",
                    "reason_code": "tool_timeout",
                    "tool": tool_name,
                    "timeout_seconds": timeout_seconds,
                    "error": f"{tool_name} exceeded {timeout_seconds}s timeout.",
                }
            )
            return ToolCallResult(
                call_id=call_id,
                name=tool_name,
                status="error",
                output=output,
                normalized_output=_normalized_output(output),
                error=f"{tool_name} exceeded {timeout_seconds}s timeout.",
                retryable=True,
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
            budget.set_current_tool(None)

    async def _run_async(self, fn, arguments: dict, timeout_seconds: float | None):
        result = fn(**arguments)
        if timeout_seconds is not None:
            return await asyncio.wait_for(result, timeout=timeout_seconds)
        return await result

    async def _run_sync(self, fn, arguments: dict, tool_dict: dict, timeout_seconds: float | None):
        loop = asyncio.get_running_loop()
        if tool_dict.get("execution_mode") == "sync_thread_affine":
            affinity_group = tool_dict.get("affinity_group") or tool_dict.get("name", "tool")
            executor = self._affinity_executor(str(affinity_group))
        else:
            executor = self._stateless_executor
        call = partial(fn, **arguments)
        ctx = contextvars.copy_context()
        future = loop.run_in_executor(executor, ctx.run, call)
        if timeout_seconds is not None:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        return await future
