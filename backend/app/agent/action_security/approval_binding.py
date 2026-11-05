"""One-use approval capability, confined to the existing resume execution."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import threading


@dataclass
class _Replay:
    ticket: object
    consumed: bool = False


_CURRENT: ContextVar[_Replay | None] = ContextVar("action_security_replay", default=None)
_LOCK = threading.RLock()
_CLAIMED: set[str] = set()


def current_approved_ticket():
    state = _CURRENT.get()
    return state.ticket if state is not None else None


@contextmanager
def approved_action_replay(ticket):
    """Only the validated resume engine may establish this context."""
    state = _Replay(ticket)
    token = _CURRENT.set(state)
    try:
        yield state
    finally:
        _CURRENT.reset(token)


def claim_ticket(ticket) -> bool:
    with _LOCK:
        if ticket.id in _CLAIMED:
            return False
        _CLAIMED.add(ticket.id)
        return True


def replay_validation(action) -> str | None:
    """None: ordinary call; empty string: exact approved call; text: reject."""
    state = _CURRENT.get()
    if state is None or not getattr(state.ticket, "authorization_binding", ""):
        return None
    ticket = state.ticket
    if state.consumed:
        return "Approved action has already been dispatched."
    from app.agent.approval_broker import ticket_validation_error
    error = ticket_validation_error(ticket, require_approved=True)
    if error:
        return error
    if not action.attested or ticket.authorization_binding != action.authorization_binding:
        return "The action parameters, target, data scope, identity or runtime context changed."
    if getattr(ticket, "human_only", False) and ticket.resolved_by != "user":
        return "This consequential action requires direct user approval."
    state.consumed = True
    return ""
