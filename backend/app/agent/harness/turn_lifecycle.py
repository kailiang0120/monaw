"""Task ownership and finalization for one agent turn."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Literal

from app.agent.harness.run_outcome import done_payload, finish_observed_run
from app.agent.observability.ports import ObservabilityPort
from app.agent.observability.recorder import UsageStats
from app.agent.run_events import RunEventPublisher
from app.agent.run_state_machine import AgentRunStateMachine

logger = logging.getLogger(__name__)
HEARTBEAT_INTERVAL_S = 10.0


class TurnStopReason(str, Enum):
    CANCELLED = "cancelled"
    TIMEOUT = "turn_timeout"


@dataclass(frozen=True)
class TurnOutcome:
    summary: str
    status: Literal["complete", "paused", "error"] = "complete"
    reason_code: str = ""
    attachments: list[dict] = field(default_factory=list)


async def drain_task(task: asyncio.Task) -> None:
    """Keep ownership through repeated Stop requests until cleanup finishes."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    # Retrieve failures as well as cancellations without cancelling the task again.
    await asyncio.gather(task, return_exceptions=True)


class TurnLifecycle:
    def __init__(self, conversation_id: str, publisher: RunEventPublisher, observability: ObservabilityPort) -> None:
        self.conversation_id = conversation_id
        self.publisher = publisher
        self.observability = observability
        self.turn_id = uuid.uuid4().hex
        self.run_id = ""
        self.state = AgentRunStateMachine()
        self.started_at = time.perf_counter()
        self.stop_reason: TurnStopReason | None = None
        self.outcome: TurnOutcome | None = None
        self._duration_ms: int | None = None
        self._finalization_task: asyncio.Task | None = None

    def duration_ms(self) -> int:
        if self._duration_ms is not None:
            return self._duration_ms
        return max(0, round((time.perf_counter() - self.started_at) * 1000))

    def transition(self, method, *args):
        previous = self.state.state
        transition = method(*args)
        if self.state.state != previous:
            self.publisher.emit("run_state", {
                "from_state": transition.from_state.value,
                "state": transition.to_state.value,
                "reason": transition.reason,
            })
        return transition

    async def _heartbeat(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            timeout = max(0.01, self.publisher.last_flush[0] + HEARTBEAT_INTERVAL_S - loop.time())
            try:
                await asyncio.wait_for(stop.wait(), timeout=timeout)
                return
            except asyncio.TimeoutError:
                if not stop.is_set() and loop.time() - self.publisher.last_flush[0] >= HEARTBEAT_INTERVAL_S:
                    await self.publisher.heartbeat(phase="idle")

    async def drive(self, worker: Awaitable[None], *, timeout_seconds: float) -> None:
        """Set the stop reason before cancellation and drain all owned tasks."""
        task = asyncio.create_task(worker)
        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(stop_heartbeat))

        def expire() -> None:
            if not task.done() and self.stop_reason is None and self._finalization_task is None:
                self.stop_reason = TurnStopReason.TIMEOUT
                task.cancel()

        timer = asyncio.get_running_loop().call_later(max(0.0, timeout_seconds), expire)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                self.stop_reason = self.stop_reason or TurnStopReason.CANCELLED
                # A finalizer already in progress owns the selected outcome.
                if self._finalization_task is None:
                    task.cancel()
                await drain_task(task)
            if self.stop_reason != TurnStopReason.TIMEOUT:
                raise
        finally:
            timer.cancel()
            stop_heartbeat.set()
            await drain_task(heartbeat)
            self.publisher.queue.put_nowait(None)

    async def finalize(
        self,
        outcome: TurnOutcome,
        *,
        persist: Callable[[TurnOutcome], Awaitable[None]],
        usage: UsageStats,
        tool_calls: list[dict],
        metadata: dict | None = None,
        clear_progress: Callable[[], None] | None = None,
    ) -> None:
        """Select one outcome and finish it once, even if Stop arrives again."""
        if self._finalization_task is None:
            self.outcome = outcome
            self._duration_ms = self.duration_ms()
            self._finalization_task = asyncio.create_task(self._finish(
                outcome, persist=persist, usage=usage, tool_calls=tool_calls,
                metadata=metadata, clear_progress=clear_progress,
            ))
        await asyncio.shield(self._finalization_task)

    async def _finish(
        self,
        outcome: TurnOutcome,
        *,
        persist: Callable[[TurnOutcome], Awaitable[None]],
        usage: UsageStats,
        tool_calls: list[dict],
        metadata: dict | None,
        clear_progress: Callable[[], None] | None,
    ) -> None:
        self.transition(self.state.finalizing)
        try:
            await persist(outcome)
        except Exception:
            logger.exception("Unable to persist turn conversation_id=%s", self.conversation_id)
            outcome = TurnOutcome(
                summary=outcome.summary,
                status="error",
                reason_code="persistence_failed",
                attachments=outcome.attachments,
            )
            self.publisher.emit("error", {
                "code": "persistence_failed",
                "message": "The turn ended, but its history could not be saved.",
            })
        self.outcome = outcome
        if outcome.status == "complete" and clear_progress is not None:
            try:
                clear_progress()
            except Exception:
                logger.exception("Unable to clear task progress conversation_id=%s", self.conversation_id)
        duration_ms = self.duration_ms()
        try:
            finish_observed_run(
                self.observability, run_id=self.run_id, status=outcome.status,
                final_output=outcome.summary, failure_reason=outcome.reason_code,
                duration_ms=duration_ms, usage=usage, tool_calls=tool_calls, metadata=metadata,
            )
        except Exception:
            logger.exception("Unable to close observed turn conversation_id=%s", self.conversation_id)
        if outcome.status == "complete":
            self.transition(self.state.complete)
        elif outcome.reason_code == TurnStopReason.CANCELLED:
            self.transition(self.state.cancel, outcome.reason_code)
        else:
            self.transition(self.state.fail, outcome.reason_code)
        self.publisher.emit("done", done_payload(
            self.conversation_id, outcome.summary, outcome.status,
            attachments=outcome.attachments, duration_ms=duration_ms,
            incomplete=outcome.status != "complete", reason_code=outcome.reason_code,
        ))
