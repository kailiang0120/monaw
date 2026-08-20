"""Cooperative cancellation hooks for synchronous tool workers."""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextvars import ContextVar

_CURRENT_CALL_ID: ContextVar[str] = ContextVar("current_tool_call_id", default="")
_LOCK = threading.RLock()
_CALLBACKS: dict[str, Callable[[], None]] = {}


def set_current_call_id(call_id: str):
    return _CURRENT_CALL_ID.set(str(call_id or ""))


def reset_current_call_id(token) -> None:
    _CURRENT_CALL_ID.reset(token)


def current_call_id() -> str:
    return _CURRENT_CALL_ID.get()


def register_cancellation(call_id: str, callback: Callable[[], None]) -> None:
    normalized = str(call_id or "")
    if not normalized:
        return
    with _LOCK:
        _CALLBACKS[normalized] = callback


def unregister_cancellation(call_id: str) -> None:
    normalized = str(call_id or "")
    if not normalized:
        return
    with _LOCK:
        _CALLBACKS.pop(normalized, None)


def cancel_call(call_id: str) -> bool:
    normalized = str(call_id or "")
    if not normalized:
        return False
    with _LOCK:
        callback = _CALLBACKS.get(normalized)
    if callback is None:
        return False
    try:
        callback()
    except Exception:
        return False
    return True

