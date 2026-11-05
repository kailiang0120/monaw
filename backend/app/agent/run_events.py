"""Helpers for publishing run-stream events."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RunEvent:
    event: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"event": self.event, "data": dict(self.data)}


def event_dict(event: Mapping[str, Any] | RunEvent) -> dict[str, Any]:
    if isinstance(event, RunEvent):
        payload = event.to_dict()
    else:
        payload = dict(event)
    payload["event"] = str(payload.get("event") or "")
    data = payload.get("data")
    payload["data"] = dict(data) if isinstance(data, Mapping) else {}
    if payload["event"] in {"tool_start", "tool_end", "observation", "approval_required", "approval_resolved", "error"}:
        from app.agent.action_security.sensitive_data import public_tool_input, redact_sensitive
        if payload["event"] == "tool_start" and "input" in payload["data"]:
            payload["data"]["input"] = public_tool_input(str(payload["data"].get("tool") or ""), payload["data"]["input"])
        payload["data"] = redact_sensitive(payload["data"])
    return payload


class RunEventPublisher:
    def __init__(
        self,
        queue: asyncio.Queue,
        last_flush: list[float],
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.queue = queue
        self.last_flush = last_flush
        self.loop = loop

    def _loop(self) -> asyncio.AbstractEventLoop:
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        return self.loop

    def _touch(self) -> None:
        self.last_flush[0] = self._loop().time()

    def publish_nowait(self, event: Mapping[str, Any] | RunEvent) -> None:
        self.queue.put_nowait(event_dict(event))
        self._touch()

    async def publish(self, event: Mapping[str, Any] | RunEvent) -> None:
        await self.queue.put(event_dict(event))
        self._touch()

    def emit(self, event: str, data: Mapping[str, Any] | None = None) -> None:
        self.publish_nowait(RunEvent(event=event, data=dict(data or {})))

    async def emit_async(self, event: str, data: Mapping[str, Any] | None = None) -> None:
        await self.publish(RunEvent(event=event, data=dict(data or {})))

    def token(self, content: str) -> None:
        self.emit("token", {"content": content})

    async def heartbeat(self, *, phase: str = "idle") -> None:
        await self.emit_async(
            "heartbeat",
            {"t": int(time.time() * 1000), "phase": phase},
        )
