"""Session-scoped inboxes for instructions sent to a running desktop turn."""

from __future__ import annotations

from dataclasses import dataclass, field
import uuid


@dataclass
class SteeringMessage:
    id: str
    message: str
    attachments: list[dict] = field(default_factory=list)
    display_message: str = ""
    public_attachments: list[dict] = field(default_factory=list)
    applied: bool = False


@dataclass
class SteeringInbox:
    conversation_id: str
    control_session_id: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    messages: list[SteeringMessage] = field(default_factory=list)
    closed: bool = False

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
    _inboxes[inbox.run_id] = inbox


def get_steering(run_id: str, control_session_id: str) -> SteeringInbox | None:
    inbox = _inboxes.get(run_id)
    return inbox if inbox is not None and inbox.control_session_id == control_session_id else None


def unregister_steering(inbox: SteeringInbox) -> None:
    inbox.closed = True
    _inboxes.pop(inbox.run_id, None)
