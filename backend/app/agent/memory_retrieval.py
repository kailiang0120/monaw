from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from app.agent.memory_index import EmbeddingProvider, SemanticIndex, content_hash
from app.agent.runtime_paths import RUNTIME_DIR

_INDEX_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory-index")

from app.agent.memory_models import (
    MemoryEntry,
    RELEVANCE_QUERY_WEIGHT_CAP,
    logger,
    normalize_workspace,
    search_terms,
)

class MemoryRetrieval:
    def _index(self) -> SemanticIndex:
        provider = EmbeddingProvider(str(self._setting("embedding_mode", "keyword")), str(self._setting("embedding_model", "")), str(self._setting("embedding_endpoint", "")), float(self._setting("embedding_timeout", 5)))
        root_key = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:16]
        return SemanticIndex(RUNTIME_DIR / "memory-index" / f"{root_key}.sqlite3", provider)

    async def _await_semantic(self, query: str, candidates: list[MemoryEntry]) -> None:
        if not self.enabled or str(self._setting("embedding_mode", "keyword")) == "keyword" or not candidates:
            return
        index = self._index()
        signature = tuple((e.scope, e.workspace, e.id, content_hash(e.content)) for e in candidates)
        key = (index.provider.identity, query, signature)
        self._semantic_results(query, candidates)
        job = self._semantic_jobs.get(key)
        if job is not None:
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(job)), timeout=float(self._setting("embedding_timeout", 5)))
            except Exception:
                pass

    async def build_prompt_async(self, query: str, *, workspace: str = "") -> str:
        if self.enabled:
            await self._await_semantic(query, self.entries(workspace))
        return self.build_prompt(query, workspace=workspace)

    async def search_async(self, query: str, *, workspace: str = "", scope: str = "", limit: int = 10) -> list[dict[str, Any]]:
        await self._await_semantic(query, self.entries(workspace, scope=scope))
        return self.search(query, workspace=workspace, scope=scope, limit=limit)

    @contextmanager
    def _index_guard(self, entries):
        with self._lock:
            if not self.enabled:
                raise ValueError("Memory was disabled while building index")
            for entry in entries:
                current = self._load(entry.scope, entry.workspace).entries
                if not any(e.id == entry.id and content_hash(e.content) == content_hash(entry.content) for e in current):
                    raise ValueError("Memory changed while building index")
            yield

    def index_health(self) -> dict[str, Any]:
        mode = str(self._setting("embedding_mode", "keyword"))
        if not self.enabled or mode == "keyword":
            return {"mode": mode, "status": "disabled", "entries": 0}
        index = self._index()
        if not index.provider.model:
            return {"mode": mode, "status": "unconfigured", "entries": 0, "error": "Choose an embedding model."}
        if mode == "local":
            endpoint = urlparse(index.provider.endpoint)
            if endpoint.scheme != "http" or endpoint.hostname not in ("localhost", "127.0.0.1", "::1"):
                return {"mode": mode, "status": "unconfigured", "entries": 0, "error": "Enter a local service address."}
        elif mode == "provider":
            from app.config import settings
            if not settings.openai_api_key:
                return {"mode": mode, "status": "unconfigured", "entries": 0, "error": "Configure an OpenAI API key in Connections."}
        health = {"mode": mode, **index.health()}
        if self._semantic_error and health["status"] == "ready":
            health.update(status="degraded", error=self._semantic_error)
        return health

    def rebuild_index(self, workspace: str = "") -> dict[str, Any]:
        if not self.enabled or str(self._setting("embedding_mode", "keyword")) == "keyword":
            return {"status": "disabled", "entries": 0}
        try:
            index = self._index()
            entries = self.entries(workspace)
            scopes = [("global", ""), ("workspace", normalize_workspace(workspace))]
            try:
                count = index.rebuild(entries, self._index_guard, scopes)
            except sqlite3.DatabaseError:
                with self._lock:
                    index.path.unlink(missing_ok=True)
                count = index.rebuild(self.entries(workspace), self._index_guard, scopes)
            self._semantic_jobs.clear()
            self._semantic_failed_at.clear()
            self._semantic_error = ""
            return {"status": "ready", "entries": count}
        except Exception as exc:
            logger.warning("Memory index rebuild failed: %s", exc)
            self._semantic_error = "Embedding service unavailable; check your model and connection."
            return {"status": "error", "entries": 0, "error": self._semantic_error}

    def _semantic_results(self, query: str, candidates: list[MemoryEntry]) -> list[tuple[MemoryEntry, float]]:
        if not self.enabled or str(self._setting("embedding_mode", "keyword")) == "keyword" or not candidates:
            return []
        index = self._index()
        signature = tuple((e.scope, e.workspace, e.id, content_hash(e.content)) for e in candidates)
        key = (index.provider.identity, query, signature)
        job = self._semantic_jobs.get(key)
        if job is not None and job.done() and job.exception() is not None:
            failed_at = self._semantic_failed_at.setdefault(key, time.monotonic())
            if time.monotonic() - failed_at >= 10:
                self._semantic_jobs.pop(key, None)
                self._semantic_failed_at.pop(key, None)
                job = None
        if job is None:
            if len(self._semantic_jobs) >= 32:
                self._semantic_jobs = {k: v for k, v in self._semantic_jobs.items() if not v.done()}
                self._semantic_failed_at = {k: v for k, v in self._semantic_failed_at.items() if k in self._semantic_jobs}
                if len(self._semantic_jobs) >= 32:
                    return []
            self._semantic_jobs[key] = _INDEX_EXECUTOR.submit(index.search, query, candidates, self._index_guard)
            return []
        if not job.done():
            return []
        try:
            result = job.result()
            self._semantic_error = ""
            current = {(e.scope, e.workspace, e.id): e for e in candidates}
            return [(current[key], relevance) for entry, relevance in result
                    if (key := (entry.scope, entry.workspace, entry.id)) in current
                    and content_hash(current[key].content) == content_hash(entry.content)]
        except Exception as exc:
            self._semantic_error = "Embedding service unavailable; using keyword retrieval."
            logger.debug("Semantic memory unavailable; using keyword search: %s", exc)
            return []

    # ----------------------------------------------------------------- retrieval

    def search(self, query: str, *, workspace: str = "", scope: str = "", limit: int = 10) -> list[dict[str, Any]]:
        """Entries ranked by how much of the query's distinctive vocabulary they cover."""
        candidates = self.entries(workspace, scope=scope)
        query_terms = frozenset(search_terms(query))
        if not query_terms:
            return []
        entry_terms = {entry.id: frozenset([*search_terms(entry.content), entry.kind]) for entry in candidates}
        total = len(candidates)
        weights = {
            term: 1.0 - 0.5 * (sum(1 for terms in entry_terms.values() if term in terms) / total) if total else 1.0
            for term in query_terms
        }
        possible = min(sum(weights.values()), RELEVANCE_QUERY_WEIGHT_CAP)
        scored: list[dict[str, Any]] = []
        for entry in candidates:
            matched = sum(weights[term] for term in query_terms & entry_terms[entry.id])
            relevance = min(1.0, matched / possible) if possible else 0.0
            if relevance <= 0:
                continue
            # Importance and recency only order memories that already match.
            score = relevance * 0.8 + (entry.importance / 10) * 0.15 + self._recency(entry.updated_at) * 0.05
            scored.append({**entry.public(), "score": round(score, 6), "relevance": round(relevance, 6), "match_type": "keyword"})
        scored.sort(key=lambda item: (-item["score"], item["id"]))
        semantic = self._semantic_results(query, candidates)
        if semantic:
            merged = {(item["scope"], item["workspace"], item["id"]): item for item in scored}
            fusion = {key: 1 / (60 + rank) for rank, key in enumerate(merged, 1)}
            for rank, (entry, relevance) in enumerate(semantic, 1):
                key = (entry.scope, entry.workspace, entry.id)
                if key not in merged:
                    merged[key] = {**entry.public(), "score": round(relevance, 6), "relevance": round(relevance, 6), "match_type": "semantic"}
                else:
                    merged[key]["match_type"] = "both"
                    merged[key]["relevance"] = max(merged[key]["relevance"], round(relevance, 6))
                fusion[key] = fusion.get(key, 0) + 1 / (60 + rank)
            scored = sorted(merged.values(), key=lambda item: (-fusion[(item["scope"], item["workspace"], item["id"])], item["id"]))
        return scored[: max(1, min(100, int(limit)))]

    @staticmethod
    def _recency(value: str) -> float:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        return 1.0 / (1.0 + max(0.0, age.total_seconds()) / 86400 / 30)

