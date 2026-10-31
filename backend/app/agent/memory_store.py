from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
import secrets
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.agent.run_context import current_interactive

from app.agent.memory_models import (
    DUPLICATE_SIMILARITY,
    KINDS,
    MAX_CONTENT_CHARS,
    MAX_LEARNING_CONTEXT_ENTRIES,
    MAX_LEARNING_INPUT_CHARS,
    MIN_CONTENT_CHARS,
    MemoryEntry,
    MemoryWriteError,
    SCOPES,
    _clamp_importance,
    _clean_content,
    _now,
    _similarity,
    contains_secret,
    default_memory_root,
    grounded_in,
    has_durable_signal,
    logger,
    normalize_workspace,
    search_terms,
)

from app.agent.memory_repository import MarkdownRepository
from app.agent.memory_retrieval import MemoryRetrieval
from app.agent.memory_prompt import MemoryPromptPolicy

class MemoryStore(MarkdownRepository, MemoryRetrieval, MemoryPromptPolicy):
    """Global and per-workspace markdown memory with lexical retrieval."""

    def __init__(self, *, root: Path | None = None, settings: Any = None, llm_client: Any = None) -> None:
        self.root = Path(root or default_memory_root())
        self.settings = settings
        self.llm_client = llm_client
        self._lock = threading.RLock()
        self._generation = 0
        self._cache: dict[Path, tuple[tuple[int, int], list[MemoryEntry]]] = {}
        self._learning_tasks: set[asyncio.Task] = set()
        self._semantic_jobs: dict[tuple, concurrent.futures.Future] = {}
        self._semantic_failed_at: dict[tuple, float] = {}
        self._semantic_error = ""
        self._pending_sources: dict[str, list[tuple[str, str, str, int]]] = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self._retire_legacy_layout()

    # ------------------------------------------------------------------ settings

    def _setting(self, name: str, default: Any) -> Any:
        memory = getattr(self.settings, "memory", None)
        return getattr(memory if memory is not None else self.settings, name, default)

    @property
    def enabled(self) -> bool:
        return bool(self._setting("enabled", True))

    @property
    def auto_learn(self) -> bool:
        return bool(self._setting("auto_learn", True))

    @property
    def retrieval_limit(self) -> int:
        try:
            return max(1, min(20, int(self._setting("retrieval_limit", 6))))
        except (TypeError, ValueError):
            return 6

    @property
    def max_injected_chars(self) -> int:
        try:
            return max(500, min(10000, int(self._setting("max_injected_chars", 2500))))
        except (TypeError, ValueError):
            return 2500

    @property
    def min_relevance_score(self) -> float:
        try:
            return max(0.0, min(1.0, float(self._setting("min_relevance_score", 0.15))))
        except (TypeError, ValueError):
            return 0.15

    # -------------------------------------------------------------------- writes

    def _validate(self, content: str) -> str:
        text = _clean_content(content)
        if len(text) < MIN_CONTENT_CHARS:
            raise MemoryWriteError("Memory is too short to be useful.")
        if len(text) > MAX_CONTENT_CHARS:
            raise MemoryWriteError(f"Keep each memory under {MAX_CONTENT_CHARS} characters; save separate facts separately.")
        if contains_secret(text):
            raise MemoryWriteError("Memory looks like it contains a secret or sensitive identifier, so it was not saved.")
        return text

    def add(
        self,
        content: str,
        *,
        scope: str,
        kind: str = "fact",
        importance: int = 5,
        workspace: str = "",
        source: str = "user",
        source_refs: tuple[str, ...] = (),
    ) -> MemoryEntry:
        if scope not in SCOPES:
            raise MemoryWriteError("Scope must be 'global' or 'workspace'.")
        if kind not in KINDS:
            raise MemoryWriteError("Kind must be 'rule', 'preference', or 'fact'.")
        text = self._validate(content)
        with self._lock:
            scope_file = self._load(scope, workspace)
            now = _now()
            duplicate = max(
                scope_file.entries,
                key=lambda entry: _similarity(entry.content, text),
                default=None,
            )
            negations = re.compile(r"\b(?:not|never|no|don't|dont|do not)\b", re.I)
            if (duplicate is not None and _similarity(duplicate.content, text) >= DUPLICATE_SIMILARITY
                    and bool(negations.search(duplicate.content)) == bool(negations.search(text))):
                # Restating a memory refreshes it instead of adding a near-copy.
                updated = replace(
                    duplicate,
                    content=text,
                    kind=kind,
                    importance=max(duplicate.importance, _clamp_importance(importance)),
                    updated_at=now,
                    source_refs=source_refs if text != duplicate.content else (source_refs or duplicate.source_refs),
                    revision=duplicate.revision + (text != duplicate.content),
                    supersedes_revision=duplicate.revision if text != duplicate.content else duplicate.supersedes_revision,
                )
                scope_file.entries = [updated if entry.id == duplicate.id else entry for entry in scope_file.entries]
                self._save(scope_file)
                return updated
            entry = MemoryEntry(
                id=f"m-{secrets.token_hex(4)}",
                scope=scope,
                kind=kind,
                content=text,
                importance=_clamp_importance(importance),
                source=source,
                created_at=now,
                updated_at=now,
                workspace=scope_file.workspace,
                source_refs=source_refs,
            )
            scope_file.entries.append(entry)
            self._save(scope_file)
            return entry

    def update(
        self,
        memory_id: str,
        *,
        workspace: str = "",
        content: str | None = None,
        kind: str | None = None,
        importance: int | None = None,
        source_refs: tuple[str, ...] = (),
    ) -> MemoryEntry | None:
        if kind is not None and kind not in KINDS:
            raise MemoryWriteError("Kind must be 'rule', 'preference', or 'fact'.")
        text = self._validate(content) if content is not None else None
        with self._lock:
            for scope_file in self._scopes_for(workspace):
                for index, entry in enumerate(scope_file.entries):
                    if entry.id != str(memory_id):
                        continue
                    updated = replace(
                        entry,
                        content=text if text is not None else entry.content,
                        kind=kind or entry.kind,
                        importance=_clamp_importance(importance) if importance is not None else entry.importance,
                        updated_at=_now(),
                        source_refs=source_refs if text is not None and text != entry.content else (source_refs or entry.source_refs),
                        revision=entry.revision + (text is not None and text != entry.content),
                        supersedes_revision=entry.revision if text is not None and text != entry.content else entry.supersedes_revision,
                    )
                    scope_file.entries[index] = updated
                    self._save(scope_file)
                    return updated
        return None

    def note_pending_source(self, run_id: str, memory_id: str, workspace: str) -> None:
        if run_id:
            with self._lock:
                entry = self.find(memory_id, workspace)
                if entry is not None:
                    self._pending_sources.setdefault(run_id, []).append((memory_id, workspace, entry.content, entry.revision))

    def discard_pending_sources(self, run_id: str) -> None:
        with self._lock:
            self._pending_sources.pop(run_id, None)

    def attach_pending_sources(self, run_id: str, conversation_id: str, message_ids: list[int]) -> None:
        refs = tuple(f"{conversation_id}:{message_id}" for message_id in message_ids)
        with self._lock:
            pending = self._pending_sources.pop(run_id, [])
            if not refs:
                return
            for memory_id, workspace, content, revision in pending:
                for scope_file in self._scopes_for(workspace):
                    for index, entry in enumerate(scope_file.entries):
                        if entry.id == memory_id and entry.content == content and entry.revision == revision:
                            scope_file.entries[index] = replace(entry, source_refs=refs)
                            self._save(scope_file)
                            break

    def delete(self, memory_id: str, *, workspace: str = "") -> bool:
        with self._lock:
            for scope_file in self._scopes_for(workspace):
                kept = [entry for entry in scope_file.entries if entry.id != str(memory_id)]
                if len(kept) != len(scope_file.entries):
                    deleted = next(entry for entry in scope_file.entries if entry.id == str(memory_id))
                    scope_file.entries = kept
                    self._save(scope_file)
                    index = self._index()
                    try:
                        if index.path.exists():
                            index.remove(deleted)
                    except Exception as exc:
                        logger.warning("Could not remove derived memory: %s", exc)
                    self._semantic_jobs.clear()
                    self._semantic_failed_at.clear()
                    return True
        return False

    def clear(self) -> int:
        """Delete every memory file (used by the complete data delete)."""
        with self._lock:
            removed = 0
            for path in [self.global_path, *(self.root / "workspaces").glob("*.md")]:
                if path.is_file():
                    path.unlink()
                    removed += 1
            self._generation += 1
            self._cache.clear()
            self._semantic_jobs.clear()
            self._semantic_failed_at.clear()
            self._pending_sources.clear()
            try:
                index = self._index()
                if index.path.exists():
                    index.clear()
            except Exception as exc:
                logger.warning("Could not clear derived memory index: %s", exc)
            return removed

    # ------------------------------------------------------------------ learning

    def schedule_learning(self, *, user_message: str, workspace: str = "", source_refs: tuple[str, ...] = ()) -> asyncio.Task | None:
        """Start background learning for a finished turn; returns the task, if one started."""
        if not (self.enabled and self.auto_learn and self.llm_client is not None and has_durable_signal(user_message)):
            return None
        # Like the memory tools, learning only follows interactive turns. Scheduled
        # tasks and other unattended runs replay old prompts and must not rewrite memory.
        if not current_interactive():
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(self.learn_from_message(user_message, workspace=workspace, source_refs=source_refs))
        self._learning_tasks.add(task)
        task.add_done_callback(self._learning_done)
        return task

    def _learning_done(self, task: asyncio.Task) -> None:
        self._learning_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Memory learning failed: %s", task.exception())

    async def learn_from_message(self, user_message: str, *, workspace: str = "", source_refs: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """Ask the model what in the user's message is worth keeping, then apply it."""
        message = str(user_message or "").strip()[:MAX_LEARNING_INPUT_CHARS]
        if not message or self.llm_client is None:
            return []
        workspace = normalize_workspace(workspace)
        with self._lock:
            generation = self._generation
            snapshot = self.entries(workspace)
            known = snapshot[-MAX_LEARNING_CONTEXT_ENTRIES:]
        prompt = (
            "Decide whether the user's message below contains anything worth remembering in future conversations.\n"
            "Keep only durable information the user states themselves: preferences, rules for how the assistant should "
            "work, facts about the user, and conventions or decisions for the current project. Skip one-off requests, "
            "task details, anything the user is quoting or pasting from elsewhere, and secrets.\n"
            "Scope: 'global' for things about the user or that apply everywhere; 'workspace' for things specific to the "
            f"current folder ({Path(workspace).name or workspace}).\n"
            "Kind: 'rule' (how the assistant must behave), 'preference' (likes and dislikes), or 'fact'.\n"
            "Write each memory as one short standalone sentence in the user's own words.\n"
            "If the message changes or contradicts an existing memory, update it by id; if the user asks to forget one, "
            "delete it by id. Do not add something that is already remembered.\n"
            'Return strict JSON only: {"operations": [{"op": "add", "scope": "...", "kind": "...", "content": "...", '
            '"importance": 1-10} | {"op": "update", "id": "...", "content": "..."} | {"op": "delete", "id": "..."}]}. '
            'Return {"operations": []} when nothing should change.\n\n'
            f"Existing memories:\n{json.dumps([{'id': e.id, 'scope': e.scope, 'kind': e.kind, 'content': e.content} for e in known], ensure_ascii=False)}\n\n"
            f"User message:\n{message}"
        )
        try:
            raw = await self.llm_client.chat(
                [{"role": "user", "content": prompt}],
                system_prompt="You maintain the assistant's long-term memory. Return strict JSON only.",
            )
        except Exception as exc:
            logger.debug("Memory learning model call failed: %s", exc)
            return []
        with self._lock:
            if not (self.enabled and self.auto_learn) or generation != self._generation or snapshot != self.entries(workspace):
                return []
            return self.apply_operations(_parse_operations(raw), user_message=message, workspace=workspace, known_ids={e.id for e in known}, source_refs=source_refs)

    def apply_operations(
        self,
        operations: list[dict[str, Any]],
        *,
        user_message: str,
        workspace: str,
        known_ids: set[str],
        source_refs: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        for operation in operations:
            op = str(operation.get("op") or "").lower()
            content = str(operation.get("content") or "")
            try:
                if op == "add" and grounded_in(content, user_message):
                    entry = self.add(
                        content,
                        scope=str(operation.get("scope") or "global"),
                        kind=str(operation.get("kind") or "fact"),
                        importance=_clamp_importance(operation.get("importance", 5)),
                        workspace=workspace,
                        source="learned",
                        source_refs=source_refs,
                    )
                    applied.append({"op": "add", **entry.public()})
                elif op == "update" and str(operation.get("id")) in known_ids and grounded_in(content, user_message):
                    entry = self.update(str(operation["id"]), workspace=workspace, content=content, source_refs=source_refs)
                    if entry is not None:
                        applied.append({"op": "update", **entry.public()})
                elif op == "delete" and str(operation.get("id")) in known_ids:
                    if self.delete(str(operation["id"]), workspace=workspace):
                        applied.append({"op": "delete", "id": str(operation["id"])})
            except MemoryWriteError as exc:
                logger.debug("Learned memory skipped: %s", exc)
        return applied


def _parse_operations(raw: Any) -> list[dict[str, Any]]:
    text = str(raw or "")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match is None:
            return []
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    operations = payload.get("operations") if isinstance(payload, dict) else payload
    return [item for item in operations if isinstance(item, dict)] if isinstance(operations, list) else []


_default_store: MemoryStore | None = None
_default_lock = threading.Lock()


def get_memory_store(llm_client: Any = None, settings: Any = None) -> MemoryStore:
    global _default_store
    with _default_lock:
        if _default_store is None:
            if settings is None:
                from app.agent.settings_store import load_agent_settings
                settings = load_agent_settings()
            _default_store = MemoryStore(settings=settings, llm_client=llm_client)
        else:
            if llm_client is not None:
                _default_store.llm_client = llm_client
            if settings is not None:
                with _default_store._lock:
                    old_memory = getattr(_default_store.settings, "memory", _default_store.settings)
                    new_memory = getattr(settings, "memory", settings)
                    if old_memory != new_memory:
                        _default_store._generation += 1
                    _default_store.settings = settings
        return _default_store


def reset_memory_store() -> None:
    global _default_store
    with _default_lock:
        if _default_store is not None:
            _default_store._semantic_jobs.clear()
            _default_store._semantic_failed_at.clear()
        _default_store = None
