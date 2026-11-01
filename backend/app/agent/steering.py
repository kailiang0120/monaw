"""Session-scoped inboxes for instructions sent to a running desktop turn."""

from __future__ import annotations

import asyncio
from collections import deque
import json
import time
from dataclasses import dataclass, field
from typing import Any
import uuid

# Text deltas are appended to the previous one of the same kind in the replay.
_MERGED_EVENTS = frozenset({"token", "thinking"})


def sse_chunk(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@dataclass
class _ReplayEntry:
    event: str
    data: Any
    size: int
    # Merged text deltas, joined when the entry is replayed.
    pieces: list[str] | None = None

    def chunk(self) -> bytes:
        if self.pieces is None:
            return sse_chunk(self.event, self.data)
        return sse_chunk(self.event, {**self.data, "content": "".join(self.pieces)})


class RunSubscription:
    """One viewer's pending chunks. None marks the end of the stream."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.pending_bytes = 0

    def put(self, chunk: bytes | None) -> None:
        self.pending_bytes += len(chunk or b"")
        self._queue.put_nowait(chunk)

    async def get(self) -> bytes | None:
        chunk = await self._queue.get()
        self.pending_bytes -= len(chunk or b"")
        return chunk

    def cut_off(self, chunk: bytes) -> None:
        """Drop what is waiting and end with chunk."""
        while not self._queue.empty():
            self._queue.get_nowait()
        self.pending_bytes = 0
        self.put(chunk)
        self.put(None)


class RunStream:
    """A turn's SSE events, so a reopened chat can replay them and follow along.

    The replay keeps the turn_started event and at most max_replay_bytes of the
    latest events; older ones are dropped and the replay says so. A viewer that
    falls more than max_backlog_bytes behind is disconnected rather than buffered.
    """

    def __init__(self, max_replay_bytes: int = 8 * 1024 * 1024, max_backlog_bytes: int | None = None) -> None:
        self.max_replay_bytes = max_replay_bytes
        # A new viewer starts with up to a full replay waiting.
        self.max_backlog_bytes = max_backlog_bytes if max_backlog_bytes is not None else 2 * max_replay_bytes
        self.closed = False
        self.dropped_events = 0
        self._started: _ReplayEntry | None = None
        self._entries: deque[_ReplayEntry] = deque()
        self._replay_bytes = 0
        self._subscribers: set[RunSubscription] = set()

    @property
    def replay_bytes(self) -> int:
        return self._replay_bytes

    def publish(self, event: str, data: Any) -> None:
        chunk = sse_chunk(event, data)
        self._remember(event, data, chunk)
        for subscription in list(self._subscribers):
            if subscription.pending_bytes + len(chunk) > self.max_backlog_bytes:
                self._subscribers.discard(subscription)
                subscription.cut_off(sse_chunk("error", {
                    "code": "stream_lagging",
                    "message": "This view fell behind the running task. Reopen the chat to follow it again.",
                }))
            else:
                subscription.put(chunk)

    def _remember(self, event: str, data: Any, chunk: bytes) -> None:
        if event == "turn_started" and self._started is None:
            self._started = _ReplayEntry(event, data, len(chunk))
            return
        last = self._entries[-1] if self._entries else None
        content = data.get("content") if isinstance(data, dict) else None
        if (
            event in _MERGED_EVENTS and isinstance(content, str) and last is not None and last.event == event
            and last.pieces is not None and data.keys() == last.data.keys()
        ):
            last.pieces.append(content)
            added = len(content.encode())
            last.size += added
            self._replay_bytes += added
        else:
            pieces = [content] if event in _MERGED_EVENTS and isinstance(content, str) else None
            self._entries.append(_ReplayEntry(event, data, len(chunk), pieces))
            self._replay_bytes += len(chunk)
        # Keep the newest entry even when it alone is over the limit.
        while self._replay_bytes > self.max_replay_bytes and len(self._entries) > 1:
            dropped = self._entries.popleft()
            self._replay_bytes -= dropped.size
            self.dropped_events += 1

    def close(self) -> None:
        self.closed = True
        for subscription in self._subscribers:
            subscription.put(None)
        self._subscribers.clear()

    def subscribe(self) -> RunSubscription:
        """A subscription holding the replay, then each new chunk; None marks the end."""
        subscription = RunSubscription()
        if self._started is not None:
            subscription.put(self._started.chunk())
        if self.dropped_events:
            subscription.put(sse_chunk("progress", {"content": "Earlier steps of this task are not shown here."}))
        for entry in self._entries:
            subscription.put(entry.chunk())
        if self.closed:
            subscription.put(None)
        else:
            self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: RunSubscription) -> None:
        self._subscribers.discard(subscription)


@dataclass
class SteeringMessage:
    id: str
    message: str
    attachments: list[dict] = field(default_factory=list)
    display_message: str = ""
    public_attachments: list[dict] = field(default_factory=list)
    applied: bool = False
    # How many of the turn's tool calls had run when the agent read it, so the
    # saved turn shows it at that point.
    tool_call_index: int = 0


@dataclass
class SteeringInbox:
    conversation_id: str
    control_session_id: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[SteeringMessage] = field(default_factory=list)
    closed: bool = False
    # The task running the turn, so an explicit Stop can cancel it.
    task: Any = field(default=None, repr=False, compare=False)
    # What the user sent, for a chat reopened while the turn runs.
    display_message: str = ""
    public_attachments: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    stream: RunStream = field(default_factory=RunStream, repr=False, compare=False)

    @property
    def pending(self) -> bool:
        return any(not item.applied for item in self.messages)

    def enqueue(self, message: SteeringMessage) -> SteeringMessage:
        # Retrying the same request must not inject the instruction twice.
        existing = next((item for item in self.messages if item.id == message.id), None)
        if existing is not None:
            if existing.message != message.message or existing.attachments != message.attachments:
                raise ValueError("This steering message ID was already used for a different message.")
            return existing
        if self.closed:
            raise ValueError("This turn has finished. Send the message as a new turn.")
        if len(self.messages) >= 32:
            raise OverflowError("This turn has reached its steering message limit.")
        self.messages.append(message)
        return message

    def drain(self) -> list[SteeringMessage]:
        pending = [item for item in self.messages if not item.applied]
        for item in pending:
            item.applied = True
        return pending


_inboxes: dict[str, SteeringInbox] = {}


def register_steering(inbox: SteeringInbox) -> None:
    if inbox.run_id in _inboxes:
        raise ValueError("A turn with this run ID is already running.")
    _inboxes[inbox.run_id] = inbox


def get_steering(run_id: str, control_session_id: str) -> SteeringInbox | None:
    inbox = _inboxes.get(run_id)
    return inbox if inbox is not None and inbox.control_session_id == control_session_id else None


def unregister_steering(inbox: SteeringInbox) -> None:
    inbox.closed = True
    _inboxes.pop(inbox.run_id, None)


def conversation_is_running(conversation_id: str) -> bool:
    return any(inbox.conversation_id == conversation_id for inbox in _inboxes.values())


def running_turns(control_session_id: str) -> list[SteeringInbox]:
    return [inbox for inbox in _inboxes.values() if inbox.control_session_id == control_session_id]
