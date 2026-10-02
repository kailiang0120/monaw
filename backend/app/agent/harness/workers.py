"""Track synchronous workers that outlive cancellation across runtime instances."""

from __future__ import annotations

import threading
import asyncio
from concurrent.futures import Future

_LOCK = threading.Lock()
_STOPPING: dict[Future | asyncio.Future, tuple[str, frozenset[str]]] = {}
_RESOURCE_LOCKS: dict[str, threading.Lock] = {}


def resource_keys(tool: dict) -> frozenset[str]:
    metadata = tool.get("metadata") or {}
    resources = {str(value) for value in metadata.get("resource_locks", [])}
    affinity = tool.get("affinity_group")
    if affinity:
        resources.add(f"affinity:{affinity}")
    if not resources and not metadata.get("parallel_safe"):
        resources.add(str(tool.get("domain") or tool.get("name") or "tool"))
    return frozenset(resources)


def resource_lock(resource: str) -> threading.Lock:
    with _LOCK:
        return _RESOURCE_LOCKS.setdefault(resource, threading.Lock())


def mark_stopping(future: Future | asyncio.Future, conversation_id: str, resources: frozenset[str]) -> None:
    with _LOCK:
        if future.done():
            return
        _STOPPING[future] = (conversation_id, resources)

    def finished(_future):
        with _LOCK:
            _STOPPING.pop(_future, None)

    future.add_done_callback(finished)


def unfinished_workers(conversation_id: str, resources: frozenset[str] = frozenset()) -> bool:
    with _LOCK:
        return any(
            not future.done() and ((conversation_id and owner == conversation_id) or bool(locked & resources))
            for future, (owner, locked) in _STOPPING.items()
        )
