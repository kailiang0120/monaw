"""Thread-safe iteration budget to prevent runaway API costs."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class IterationBudget:
    """Thread-safe bounded iteration counter."""

    max_iterations: int = 40
    _remaining: int = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _consumed: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._remaining = self.max_iterations

    def consume(self, n: int = 1) -> bool:
        """Attempt to consume *n* iterations. Returns False if budget exhausted."""
        with self._lock:
            if self._remaining < n:
                return False
            self._remaining -= n
            self._consumed += n
            return True

    @property
    def remaining(self) -> int:
        with self._lock:
            return self._remaining

    @property
    def consumed(self) -> int:
        with self._lock:
            return self._consumed
