"""Thread-safe iteration budget to prevent runaway API costs.

Shared across parent + child agents. Each consume() call deducts from
the remaining budget; refund() returns unused iterations.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class IterationBudget:
    """Thread-safe bounded iteration counter."""

    max_iterations: int = 90
    _remaining: int = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _consumed: int = field(init=False, default=0)
    _last_activity: float = field(init=False, default_factory=time.monotonic)
    _current_tool: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._remaining = self.max_iterations

    def consume(self, n: int = 1) -> bool:
        """Attempt to consume *n* iterations. Returns False if budget exhausted."""
        with self._lock:
            if self._remaining < n:
                return False
            self._remaining -= n
            self._consumed += n
            self._last_activity = time.monotonic()
            return True

    def refund(self, n: int = 1) -> None:
        """Return *n* unused iterations back to the budget."""
        with self._lock:
            self._remaining = min(self._remaining + n, self.max_iterations)
            self._consumed = max(0, self._consumed - n)

    def set_current_tool(self, tool_name: str | None) -> None:
        """Track which tool is currently executing (for timeout monitoring)."""
        with self._lock:
            self._current_tool = tool_name
            self._last_activity = time.monotonic()

    @property
    def remaining(self) -> int:
        with self._lock:
            return self._remaining

    @property
    def consumed(self) -> int:
        with self._lock:
            return self._consumed

    @property
    def current_tool(self) -> str | None:
        with self._lock:
            return self._current_tool

    @property
    def idle_seconds(self) -> float:
        """Seconds since the last consume() or set_current_tool() call."""
        with self._lock:
            return time.monotonic() - self._last_activity

    def is_stalled(self, timeout: float = 300.0) -> bool:
        """True if no activity for longer than *timeout* seconds."""
        return self.idle_seconds > timeout

    def fork(self, child_budget: int = 10) -> "IterationBudget":
        """Create a child budget that shares the parent's remaining pool."""
        with self._lock:
            grant = min(child_budget, self._remaining)
            self._remaining -= grant
        child = IterationBudget(max_iterations=grant)
        child._remaining = grant
        return child

    def merge_child(self, child: "IterationBudget") -> None:
        """Return unused child iterations back to parent."""
        leftover = child.remaining
        if leftover > 0:
            self.refund(leftover)
