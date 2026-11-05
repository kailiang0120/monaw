"""Per-call dispatch evidence shared with the CDP boundary through contextvars."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ActionAttempt:
    name: str
    arguments: dict
    action_id: str
    session: Any = None
    before: dict = field(default_factory=dict)
    dispatched: bool = False
    authorized: bool = False
    guard: Any = None
    started: float = 0

    async def dispatch(self, method=None, params=None):
        self.boundary = (method, params or {})
        if not self.authorized:
            if self.guard:
                await self.guard(self)
            self.authorized = True
        elif self.guard:
            await self.guard(self, continuation=True)
        # Set before sending: a lost reply does not establish that input failed.
        self.dispatched = True


current_attempt: ContextVar[ActionAttempt | None] = ContextVar('browser_action_attempt', default=None)


async def before_dispatch(method=None, params=None):
    attempt = current_attempt.get()
    if attempt is not None:
        await attempt.dispatch(method, params)
