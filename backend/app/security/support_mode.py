from __future__ import annotations

import threading
import time
from dataclasses import dataclass


MAX_SUPPORT_MODE_SECONDS = 15 * 60


@dataclass(frozen=True)
class SupportModeStatus:
    enabled: bool
    expires_at_epoch: int
    remaining_seconds: int


_lock = threading.RLock()
_expiry_by_session: dict[str, float] = {}


def enable_support_mode(session_id: str, duration_seconds: int = 300) -> SupportModeStatus:
    duration = max(30, min(int(duration_seconds or 300), MAX_SUPPORT_MODE_SECONDS))
    expires_at = time.time() + duration
    with _lock:
        _expiry_by_session[session_id] = expires_at
    return support_mode_status(session_id)


def disable_support_mode(session_id: str) -> SupportModeStatus:
    with _lock:
        _expiry_by_session.pop(session_id, None)
    return support_mode_status(session_id)


def support_mode_status(session_id: str) -> SupportModeStatus:
    now = time.time()
    with _lock:
        expires_at = _expiry_by_session.get(session_id, 0.0)
        if expires_at <= now:
            _expiry_by_session.pop(session_id, None)
            expires_at = 0.0
    remaining = max(0, int(expires_at - now))
    return SupportModeStatus(
        enabled=remaining > 0,
        expires_at_epoch=int(expires_at),
        remaining_seconds=remaining,
    )
