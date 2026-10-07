from __future__ import annotations

import time
import uuid
import threading
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from app.agent.run_context import current_execution_principal, current_control_session_id
from app.agent.tool_cancellation import current_tool_scope
from app.skills.computer_use.observation import image_sample

# Must outlive the approval wait (ExecutionGateService default 600s): an
# approved batch replays its state_id. Live validation still rejects drift.
_STATE_TTL_SECONDS = 600
_MAX_STATES = 32


@dataclass
class ComputerWindowState:
    state_id: str
    created_at: float
    window: dict[str, Any]
    screenshot: dict[str, Any]
    uia: dict[str, Any]
    owner: tuple
    visual_sample: bytes | None = None


_STATES: dict[str, ComputerWindowState] = {}
_LOCK = threading.RLock()


def _owner() -> tuple:
    return (current_execution_principal(), current_control_session_id(), current_tool_scope())


def _prune() -> None:
    now = time.monotonic()
    expired = [
        state_id
        for state_id, state in _STATES.items()
        if now - state.created_at > _STATE_TTL_SECONDS
    ]
    for state_id in expired:
        _STATES.pop(state_id, None)
    if len(_STATES) <= _MAX_STATES:
        return
    oldest = sorted(_STATES.values(), key=lambda state: state.created_at)
    for state in oldest[: len(_STATES) - _MAX_STATES]:
        _STATES.pop(state.state_id, None)


def store_state(*, window: dict[str, Any], screenshot: dict[str, Any], uia: dict[str, Any]) -> str:
    with _LOCK:
        state_id = f"cfs_{uuid.uuid4().hex}"
        _STATES[state_id] = ComputerWindowState(
            state_id=state_id, created_at=time.monotonic(),
            window=deepcopy(window or {}), screenshot=deepcopy(screenshot or {}),
            uia=deepcopy(uia or {}), owner=_owner(),
            visual_sample=image_sample(screenshot or {}),
        )
        _prune()
        return state_id


def get_state(state_id: str) -> ComputerWindowState | None:
    with _LOCK:
        _prune()
        state = _STATES.get(str(state_id or ""))
        return deepcopy(state) if state is not None and state.owner == _owner() else None


def invalidate_window(hwnd: int) -> None:
    """Any interaction invalidates snapshots of that window in every chat."""
    with _LOCK:
        for state_id in [key for key, state in _STATES.items() if int(state.window.get("hwnd") or 0) == int(hwnd)]:
            _STATES.pop(state_id, None)


def consume_state(state_id: str) -> ComputerWindowState | None:
    """One batch may consume a snapshot, including when it only partly completes."""
    with _LOCK:
        state = get_state(state_id)
        if state is not None:
            invalidate_window(int(state.window.get("hwnd") or 0))
        return state


def clear_states() -> None:
    with _LOCK:
        _STATES.clear()
