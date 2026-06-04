from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

_STATE_TTL_SECONDS = 600
_MAX_STATES = 32


@dataclass
class ComputerWindowState:
    state_id: str
    created_at: float
    window: dict[str, Any]
    screenshot: dict[str, Any]
    uia: dict[str, Any]


_STATES: dict[str, ComputerWindowState] = {}


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
    _prune()
    state_id = f"cfs_{uuid.uuid4().hex}"
    _STATES[state_id] = ComputerWindowState(
        state_id=state_id,
        created_at=time.monotonic(),
        window=dict(window or {}),
        screenshot=dict(screenshot or {}),
        uia=dict(uia or {}),
    )
    return state_id


def get_state(state_id: str) -> ComputerWindowState | None:
    _prune()
    state = _STATES.get(str(state_id or ""))
    if state is None:
        return None
    if time.monotonic() - state.created_at > _STATE_TTL_SECONDS:
        _STATES.pop(state.state_id, None)
        return None
    return state


def clear_states() -> None:
    _STATES.clear()
