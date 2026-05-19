"""Background job manager for detached long-running agent turns.

Maintains a process-wide registry of jobs, each with:
- An asyncio task running the react loop
- A ring buffer of up to MAX_LOG_EVENTS SSE event dicts (for Last-Event-ID replay)
- Status tracking and cancellation support
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator

MAX_LOG_EVENTS = 2000


@dataclass
class JobState:
    job_id: str
    conversation_id: str
    status: str = "running"  # "running" | "done" | "paused" | "cancelled" | "error"
    started_at: float = field(default_factory=time.time)
    workflow_engine: str = ""
    active_graph_node: str = ""
    checkpoint_status: str = ""
    last_resume_reason: str = ""
    # Ring buffer: list of (seq_id, event_dict)
    event_log: list[tuple[int, dict]] = field(default_factory=list)
    _seq: int = 0
    _task: asyncio.Task | None = None
    _subscribers: list[asyncio.Queue] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def append_event(self, event: dict) -> None:
        seq = self.next_seq()
        entry = (seq, event)
        data = event.get("data", {})
        if isinstance(data, dict):
            self.workflow_engine = str(data.get("workflow_engine", self.workflow_engine) or self.workflow_engine)
            self.active_graph_node = str(data.get("active_graph_node", self.active_graph_node) or self.active_graph_node)
            self.checkpoint_status = str(data.get("checkpoint_status", self.checkpoint_status) or self.checkpoint_status)
            self.last_resume_reason = str(data.get("last_resume_reason", self.last_resume_reason) or self.last_resume_reason)
        async with self._lock:
            self.event_log.append(entry)
            if len(self.event_log) > MAX_LOG_EVENTS:
                self.event_log = self.event_log[-MAX_LOG_EVENTS:]
            for q in list(self._subscribers):
                try:
                    q.put_nowait(entry)
                except asyncio.QueueFull:
                    pass
        if event.get("event") in ("done", "error"):
            if event.get("event") == "done":
                event_data = event.get("data", {})
                self.status = "paused" if isinstance(event_data, dict) and event_data.get("incomplete") else "done"
            else:
                self.status = "error"

    async def subscribe(self, last_event_id: int | None = None) -> AsyncIterator[tuple[int, dict]]:
        """Yield (seq_id, event) starting from last_event_id+1, then live."""
        q: asyncio.Queue = asyncio.Queue(maxsize=512)

        async with self._lock:
            # Replay buffered events after last_event_id
            replay_start = 0
            if last_event_id is not None:
                for i, (seq, _) in enumerate(self.event_log):
                    if seq > last_event_id:
                        replay_start = i
                        break
                else:
                    replay_start = len(self.event_log)
            replay = list(self.event_log[replay_start:])
            self._subscribers.append(q)

        try:
            for entry in replay:
                yield entry

            if self.status != "running":
                return

            while True:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield entry
                    _, event = entry
                    if event.get("event") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    # Emit a heartbeat to keep the subscriber's connection alive
                    yield (0, {"event": "heartbeat", "data": {"t": int(time.time() * 1000), "phase": "idle"}})
        finally:
            async with self._lock:
                try:
                    self._subscribers.remove(q)
                except ValueError:
                    pass


# Process-wide registry
_jobs: dict[str, JobState] = {}


def create_job(conversation_id: str) -> JobState:
    job_id = str(uuid.uuid4())
    job = JobState(job_id=job_id, conversation_id=conversation_id)
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> JobState | None:
    return _jobs.get(job_id)


def list_jobs() -> list[JobState]:
    return list(_jobs.values())


async def run_job(
    job: JobState,
    message: str,
    settings,
) -> None:
    """Run the agent loop for a job, feeding all events into the job's ring buffer."""
    from app.agent.runtime import run_agent_stream

    async def _drive():
        try:
            async for event in run_agent_stream(
                message=message,
                conversation_id=job.conversation_id,
                settings=settings,
            ):
                await job.append_event(event)
                if event.get("event") == "done":
                    break
        except asyncio.CancelledError:
            job.status = "cancelled"
            await job.append_event({
                "event": "error",
                "data": {"code": "cancelled", "message": "Job was cancelled."},
            })
            await job.append_event({
                "event": "done",
                "data": {"conversation_id": job.conversation_id, "summary": "Cancelled."},
            })
        except Exception as exc:
            job.status = "error"
            await job.append_event({
                "event": "error",
                "data": {"code": "internal_error", "message": str(exc)},
            })
            await job.append_event({
                "event": "done",
                "data": {"conversation_id": job.conversation_id, "summary": "Error."},
            })

    task = asyncio.create_task(_drive())
    job._task = task
    await task


async def cancel_job(job_id: str) -> bool:
    job = _jobs.get(job_id)
    if job is None:
        return False
    if job._task and not job._task.done():
        job._task.cancel()
        return True
    return False
