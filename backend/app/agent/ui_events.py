"""Application-wide UI invalidation events."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator


UI_EVENT_HISTORY_LIMIT = 200


@dataclass(frozen=True)
class UiEvent:
    seq: int
    event: str
    data: dict[str, Any]


@dataclass(frozen=True)
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[UiEvent]


_lock = threading.RLock()
_next_seq = 0
_history: deque[UiEvent] = deque(maxlen=UI_EVENT_HISTORY_LIMIT)
_subscribers: list[_Subscriber] = []
_dropped_events = 0


def publish_ui_event(event: str, data: dict[str, Any] | None = None) -> UiEvent:
    """Publish a small invalidation event to connected renderers."""

    global _next_seq
    payload = UiEvent(seq=0, event=event, data=dict(data or {}))
    with _lock:
        _next_seq += 1
        payload = UiEvent(seq=_next_seq, event=event, data=dict(data or {}))
        _history.append(payload)
        subscribers = list(_subscribers)

    for subscriber in subscribers:
        def deliver(sub: _Subscriber = subscriber, item: UiEvent = payload) -> None:
            global _dropped_events
            try:
                sub.queue.put_nowait(item)
            except asyncio.QueueFull:
                with _lock:
                    _dropped_events += 1

        subscriber.loop.call_soon_threadsafe(deliver)
    return payload


async def subscribe_ui_events(last_event_id: int | None = None) -> AsyncIterator[UiEvent]:
    """Subscribe to UI events with bounded replay by event id."""

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[UiEvent] = asyncio.Queue(maxsize=100)
    subscriber = _Subscriber(loop=loop, queue=queue)
    with _lock:
        replay = [
            event
            for event in _history
            if last_event_id is None or event.seq > last_event_id
        ]
        _subscribers.append(subscriber)

    try:
        yield UiEvent(seq=0, event="backend.ready", data={"ready": True})
        for event in replay:
            yield event
        while True:
            try:
                yield await asyncio.wait_for(queue.get(), timeout=25)
            except asyncio.TimeoutError:
                yield UiEvent(seq=0, event="heartbeat", data={})
    finally:
        with _lock:
            if subscriber in _subscribers:
                _subscribers.remove(subscriber)


def reset_ui_events_for_tests() -> None:
    global _dropped_events, _next_seq
    with _lock:
        _next_seq = 0
        _dropped_events = 0
        _history.clear()
        _subscribers.clear()


def ui_event_metrics() -> dict[str, int]:
    with _lock:
        queue_depth = sum(subscriber.queue.qsize() for subscriber in _subscribers)
        return {
            "ui_event_dropped_events": _dropped_events,
            "ui_event_queue_depth": queue_depth,
            "ui_event_subscribers": len(_subscribers),
        }
