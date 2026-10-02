"""Cooperative cancellation hooks for synchronous tool workers."""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextvars import ContextVar

_CURRENT_CALL_ID: ContextVar[str] = ContextVar("current_tool_call_id", default="")
_TOOL_SCOPE: ContextVar[str] = ContextVar("tool_cancellation_scope", default="")
_LOCK = threading.RLock()
_CALLBACKS: dict[str, Callable[[], None]] = {}
_CANCELLED: set[str] = set()


def set_tool_scope(scope: str):
    return _TOOL_SCOPE.set(scope)


def current_tool_scope() -> str:
    return _TOOL_SCOPE.get()


def reset_tool_scope(token) -> None:
    _TOOL_SCOPE.reset(token)


def _scoped_id(call_id: str) -> str:
    value = str(call_id or "")
    scope = _TOOL_SCOPE.get()
    return f"{scope}:{value}" if value and scope and not value.startswith(f"{scope}:") else value


def set_current_call_id(call_id: str):
    return _CURRENT_CALL_ID.set(_scoped_id(call_id))


def reset_current_call_id(token) -> None:
    _CURRENT_CALL_ID.reset(token)


def current_call_id() -> str:
    return _CURRENT_CALL_ID.get()


def register_cancellation(call_id: str, callback: Callable[[], None]) -> None:
    normalized = _scoped_id(call_id)
    if not normalized:
        return
    with _LOCK:
        _CALLBACKS[normalized] = callback
        cancelled = normalized in _CANCELLED
    if cancelled:
        callback()


def unregister_cancellation(call_id: str) -> None:
    normalized = _scoped_id(call_id)
    if not normalized:
        return
    with _LOCK:
        _CALLBACKS.pop(normalized, None)
        _CANCELLED.discard(normalized)


def finish_cancellation(call_id: str) -> None:
    with _LOCK:
        _CALLBACKS.pop(call_id, None)
        _CANCELLED.discard(call_id)


def cancel_call(call_id: str) -> bool:
    normalized = _scoped_id(call_id)
    if not normalized:
        return False
    with _LOCK:
        _CANCELLED.add(normalized)
        callback = _CALLBACKS.get(normalized)
    if callback is None:
        return False
    try:
        callback()
    except Exception:
        return False
    return True

