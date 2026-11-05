"""Session-scoped inboxes for instructions sent to a running desktop turn."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
import uuid

from app.agent.run_stream import RunStream, sse_chunk

__all__ = ["RunStream", "sse_chunk", "SteeringInbox", "SteeringMessage", "register_steering",
           "get_steering", "unregister_steering", "conversation_is_running", "running_turns"]

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
    # A resumed Run continues the paused reply; it has no new user message to show.
    continuation: bool = False
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
