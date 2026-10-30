"""Own the tool calls of one turn from preflight until their results are persisted."""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.agent.harness.tool_protocol import ToolCallResult

PENDING_STATUSES = frozenset({"pending_approval", "pending_access_grant"})
OBSERVED_OK_STATUSES = frozenset({"ok", "complete"})


@dataclass
class ActiveToolCall:
    """A started call whose ordered result has not been persisted yet."""

    call_id: str
    tool_name: str
    input: str
    output: str
    status: str
    obs_started_at: float
    completed: bool = False
    duration_ms: int | None = None
    observability_finished: bool = False

    def persistable(self) -> dict:
        return {"tool_name": self.tool_name, "input": self.input, "output": self.output, "status": self.status}


InvokePrepared = Callable[[dict], Awaitable[ToolCallResult]]
ReportEscapedError = Callable[[dict, BaseException], None]
ObserveCancelled = Callable[[ActiveToolCall, str, str, str, "int | None"], None]


class ToolBatchLedger:
    """Track started calls and their invocation tasks for one turn.

    Calls are prepared in model order, but a safe contiguous batch may have
    several invocations in flight at once. Every started call stays here until
    its ordered result has been persisted, so cancellation accounts for the
    whole batch rather than only the last call, and completed siblings keep
    their real results.
    """

    def __init__(self) -> None:
        self._calls: dict[str, ActiveToolCall] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._sequence = itertools.count()

    def register(self, *, call_id: str, tool_name: str, arguments: dict, obs_started_at: float) -> str:
        """Record a preflighted call as cancelled until a result replaces that placeholder."""
        key = f"{call_id}:{next(self._sequence)}"
        self._calls[key] = ActiveToolCall(
            call_id=call_id,
            tool_name=tool_name,
            input=json.dumps(arguments, ensure_ascii=False),
            output=json.dumps(
                {
                    "status": "cancelled",
                    "reason_code": "user_stopped",
                    "error": "Stopped by user before the tool finished.",
                },
                ensure_ascii=False,
            ),
            status="cancelled",
            obs_started_at=obs_started_at,
        )
        return key

    def get(self, key: str) -> ActiveToolCall | None:
        return self._calls.get(key)

    def record_result(self, key: str, result: ToolCallResult, *, timed: bool = True) -> None:
        """Mirror a call's latest result so cancellation can keep it."""
        call = self._calls.get(key)
        if call is None:
            return
        call.output = result.output
        call.status = "complete" if result.status == "ok" else result.status
        call.completed = result.status not in PENDING_STATUSES
        if timed:
            completed_at = time.perf_counter()
            call.duration_ms = round((completed_at - (call.obs_started_at or completed_at)) * 1000)

    def record_output(self, key: str, output: str, status: str) -> None:
        """Replace a call's output with its final processed form."""
        call = self._calls.get(key)
        if call is not None:
            call.output = output
            call.status = "complete" if status == "ok" else status

    def finish(self, key: str) -> None:
        """Release a call once its result has been added to the persisted history."""
        self._calls.pop(key, None)

    def persistable_calls(self) -> list[dict]:
        return [call.persistable() for call in self._calls.values()]

    async def run(
        self,
        prepared_calls: list[dict],
        invoke: InvokePrepared,
        *,
        parallel: bool,
        report_escaped_error: ReportEscapedError,
    ) -> list[ToolCallResult]:
        """Run a prepared batch while retaining task ownership for cancellation."""
        tasks: list[asyncio.Task] = []
        for prepared in prepared_calls:
            task = asyncio.create_task(invoke(prepared), name=f"turn-tool-{prepared['call_id']}")
            self._tasks[prepared["active_key"]] = task
            tasks.append(task)

        if not parallel:
            # A serial barrier still uses a tracked task so a turn
            # cancellation can drain it through the same path.
            result = await tasks[0]
            self._tasks.pop(prepared_calls[0]["active_key"], None)
            return [result]

        values = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[ToolCallResult] = []
        for prepared, value in zip(prepared_calls, values):
            self._tasks.pop(prepared["active_key"], None)
            if isinstance(value, asyncio.CancelledError):
                raise value
            if isinstance(value, BaseException):
                # The invocation wrapper normally converts exceptions, but
                # isolate a failure from its sibling if a future error escapes
                # that boundary.
                report_escaped_error(prepared, value)
                value = ToolCallResult.from_output(
                    call_id=str(prepared["call_id"]),
                    name=str(prepared["tool_name"]),
                    output=json.dumps({"status": "error", "error": str(value)}),
                    fallback_status="error",
                )
                self.record_result(prepared["active_key"], value)
            results.append(value)
        return results

    async def cancel_tasks(self) -> None:
        """Cancel and drain invocation tasks created for this turn."""
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def mark_cancelled(self, *, worker_state: str, observe: ObserveCancelled) -> None:
        """Mark unfinished calls cancelled and report each unobserved call once.

        ``observe`` receives the call, its observed status, output, error code
        and measured duration (only for calls that completed).
        """
        cancelled_output = json.dumps(
            {
                "status": "cancelled",
                "reason_code": "user_stopped",
                "error": "Cancellation requested while the tool was running. Verify its last action before retrying.",
                "worker_state": worker_state,
            },
            ensure_ascii=False,
        )
        for call in self._calls.values():
            if not call.completed:
                call.output = cancelled_output
                call.status = "cancelled"
            if call.observability_finished:
                continue
            if call.completed:
                observed_status = "ok" if call.status == "complete" else "error"
            else:
                observed_status = "cancelled"
            observe(
                call,
                observed_status,
                call.output or cancelled_output,
                "" if observed_status in OBSERVED_OK_STATUSES else "cancelled",
                call.duration_ms if call.completed else None,
            )
            call.observability_finished = True
