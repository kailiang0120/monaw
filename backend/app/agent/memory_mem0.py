"""Experimental Mem0 evaluation adapter; it never writes canonical memory.

Caller supplies an independently configured OSS Memory object. Use a disposable
local store and local embeddings; this module never creates a cloud client.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any


class Mem0EvaluationAdapter:
    def __init__(self, canonical: Any, client: Any):
        self.canonical = canonical
        self.client = client
        self._namespace = 'monaw-eval-' + secrets.token_hex(12)
        self._users: set[str] = set()
        self._signature = None
        self.last_ingest_ms = 0.0

    def _user(self, scope: str, workspace: str) -> str:
        return self._namespace + '-' + hashlib.sha256(f'{scope}:{workspace}'.encode()).hexdigest()[:16]

    def prepare(self, workspace: str = '', scope: str = ''):
        entries = self.canonical.entries(workspace, scope=scope)
        signature = tuple((e.scope, e.workspace, e.id, hashlib.sha256(e.content.encode()).hexdigest()) for e in entries)
        if signature == self._signature:
            self.last_ingest_ms = 0.0
            return
        started = time.perf_counter()
        self.close()
        try:
            for entry in entries:
                user = self._user(entry.scope, entry.workspace)
                self._users.add(user)
                self.client.add(entry.content, user_id=user, infer=False, metadata={'canonical_id': entry.id, 'hash': hashlib.sha256(entry.content.encode()).hexdigest()})
            self._signature = signature
        except Exception:
            self.close()
            raise
        finally:
            self.last_ingest_ms = (time.perf_counter() - started) * 1000

    def search(self, query: str, *, workspace: str = '', scope: str = '', limit: int = 10):
        self.prepare(workspace, scope)
        entries = self.canonical.entries(workspace, scope=scope)
        grouped: dict[tuple[str, str], list] = {}
        for entry in entries:
            grouped.setdefault((entry.scope, entry.workspace), []).append(entry)
        found = []
        for (entry_scope, folder), group in grouped.items():
            user = self._user(entry_scope, folder)
            result = self.client.search(query, user_id=user, limit=limit)
            rows = result.get('results', []) if isinstance(result, dict) else result
            canonical = {e.id: e for e in self.canonical.entries(workspace, scope=entry_scope) if e.workspace == folder}
            for row in rows:
                meta = row.get('metadata') or {}
                entry = canonical.get(meta.get('canonical_id'))
                if entry is None or meta.get('hash') != hashlib.sha256(entry.content.encode()).hexdigest():
                    continue
                relevance = float(row.get('score', 0))
                found.append({**entry.public(), 'score': relevance, 'relevance': relevance})
        return sorted(found, key=lambda item: (-item['score'], item['id']))[:limit]

    def close(self):
        for user in self._users:
            self.client.delete_all(user_id=user)
        self._users.clear()
        self._signature = None
