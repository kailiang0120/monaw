"""Markdown-backed durable memory for the agent runtime.

Conversation history stays in SQLite. Durable memories, session summaries,
personality notes, memory audit events, and migrated memory records live as
human-readable Markdown under the memory root.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.agent.database import Database, get_db
from app.agent.identity import DEFAULT_AGENT_NAME
from app.agent.memory_documents import (
    SCHEMA_VERSION,
    SECTIONED_SCHEMA,
    SearchDocument,
    Section,
    content_hash as _content_hash,
    default_memory_root,
    read_markdown as _read_markdown,
    safe_id as _safe_id,
    slug as _slug,
    split_markdown as _split_markdown,
    tokens as _tokens,
    write_markdown as _write_markdown,
)
from app.agent.memory_repository import MemorySectionRepository
from app.agent.runtime_paths import MONAW_HOME_DIR, RUNTIME_DIR

logger = logging.getLogger(__name__)

VALID_CATEGORIES = ("preference", "behavior", "fact", "workflow", "project", "reflection")
LEGACY_CATEGORIES = (*VALID_CATEGORIES, "style")
VALID_STATUSES = ("active", "archived")
VALID_REVIEW_STATES = ("new", "reviewed")
VALID_KINDS = ("fact", "reflection")
HEURISTIC_MERGE_MIN_SIMILARITY = 0.8

MAX_TRANSCRIPT_CHARS = 14000
MEMORY_RETENTION_DAYS = 90
MAX_AUDIT_COUNTER_ACTIONS = frozenset({"INJECT", "CANDIDATE", "REJECT_CANDIDATE"})
MAX_TOKENIZED_RECORD_CACHE = 5000

_SENSITIVE_PATTERNS = [
    re.compile(r"\b(api[_ -]?key|secret|password|passwd|token|bearer)\b", re.I),
    re.compile(r"sk-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
    re.compile(r"\b(cvv|bank account|medical record|diagnosis)\b", re.I),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _table_exists(db: Database, table: str) -> bool:
    row = db.fetchone(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    )
    return row is not None


class LongTermMemory:
    """Markdown-backed durable memory with lexical retrieval."""

    def __init__(
        self,
        db: Database | None = None,
        llm_client=None,
        settings=None,
        memory_root: Path | None = None,
    ) -> None:
        self._db = db or get_db()
        self.llm_client = llm_client
        self.settings = settings
        self.root = Path(memory_root or default_memory_root())
        self._lock = threading.RLock()
        self._records_cache: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        self._search_documents_cache: dict[tuple[Any, ...], list[SearchDocument]] = {}
        self._tokenized_record_cache: dict[tuple[str, str, str, str], tuple[str, frozenset[str]]] = {}
        self._section_repository = MemorySectionRepository(
            self.root,
            VALID_CATEGORIES,
            now=_now,
        )
        self._ensure_layout()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._memory_settings(), "enabled", True))

    @property
    def auto_learn_enabled(self) -> bool:
        return bool(getattr(self._memory_settings(), "auto_learn", True))

    @property
    def curate_on_session_close(self) -> bool:
        return bool(getattr(self._memory_settings(), "curate_on_session_close", True))

    @property
    def write_policy(self) -> str:
        value = str(getattr(self._memory_settings(), "write_policy", "auto_with_review") or "auto_with_review")
        return value if value in {"off", "manual", "auto_with_review", "auto_reviewed"} else "auto_with_review"

    @property
    def retrieval_limit(self) -> int:
        return max(1, min(20, _coerce_int(getattr(self._memory_settings(), "retrieval_limit", 6), 6)))

    @property
    def max_injected_chars(self) -> int:
        return max(500, min(10000, _coerce_int(getattr(self._memory_settings(), "max_injected_chars", 2500), 2500)))

    @property
    def min_confidence(self) -> float:
        return max(0.0, min(1.0, _coerce_float(getattr(self._memory_settings(), "min_confidence", 0.75), 0.75)))

    @property
    def min_relevance_score(self) -> float:
        return max(0.0, min(1.0, _coerce_float(getattr(self._memory_settings(), "min_relevance_score", 0.15), 0.15)))

    @property
    def maintenance_cooldown_hours(self) -> int:
        return max(0, min(168, _coerce_int(getattr(self._memory_settings(), "maintenance_cooldown_hours", 24), 24)))

    def _memory_settings(self):
        memory = getattr(self.settings, "memory", None)
        return memory if memory is not None else self.settings

    def _ensure_layout(self) -> None:
        self._remove_legacy_audit_folder()
        for path in [
            self.root / "personalities",
            self.root / "long-term",
            self.root / "short-term",
            self.root / "archive" / "short-term",
            self.root / "archive",
            self.root / ".system",
            self.root / ".system" / "curated",
        ]:
            path.mkdir(parents=True, exist_ok=True)
        for category in VALID_CATEGORIES:
            (self.root / "archive" / "memories" / category).mkdir(parents=True, exist_ok=True)

        schema_path = self.root / ".system" / "schema.json"
        if not schema_path.exists():
            schema_path.write_text(
                json.dumps({"version": SCHEMA_VERSION, "created_at": _now()}, indent=2),
                encoding="utf-8",
            )

        self._ensure_personality_file("monaw", {"display_name": DEFAULT_AGENT_NAME})
        self._ensure_personality_file("user", {"display_name": "User"})
        self._migrate_sectioned_storage_once()

    def _remove_legacy_audit_folder(self) -> None:
        """Remove the pre-SQLite audit files only from an approved memory root."""
        audit_root = self.root / "audit"
        try:
            approved = self.root.resolve(strict=False).is_relative_to(RUNTIME_DIR.resolve(strict=False))
            approved = approved or self.root.resolve(strict=False).is_relative_to(MONAW_HOME_DIR.resolve(strict=False))
        except (OSError, RuntimeError):
            approved = False
        if not approved or audit_root.is_symlink() or not audit_root.exists():
            return
        try:
            shutil.rmtree(audit_root)
        except OSError:
            logger.warning("Unable to remove legacy memory audit folder %s", audit_root)

    def _ensure_personality_file(self, name: str, extra: dict[str, Any]) -> None:
        path = self.root / "personalities" / f"{_safe_id(name)}.md"
        if path.exists():
            return
        _write_markdown(
            path,
            {
                "id": _safe_id(name),
                "type": "personality",
                "status": "active",
                "created_at": _now(),
                "updated_at": _now(),
                **extra,
            },
            "",
        )

    def _migrate_sectioned_storage_once(self) -> None:
        marker = self.root / ".system" / "sectioned-v1.migrated"
        legacy_active = [
            path
            for category in LEGACY_CATEGORIES
            for path in (self.root / "long-term" / category).glob("*.md")
        ]
        legacy_archived = [
            path
            for category in LEGACY_CATEGORIES
            for path in (self.root / "archive" / "memories" / category).glob("*.md")
        ]
        if marker.exists() and not legacy_active and not legacy_archived:
            return

        with self._lock:
            grouped: dict[tuple[str, bool], list[dict[str, Any]]] = {}
            for path in legacy_active:
                if record := self._load_record(path):
                    record["status"] = "active"
                    record["collection"] = "long-term"
                    grouped.setdefault((record["category"], False), []).append(record)
            for path in legacy_archived:
                if record := self._load_record(path):
                    record["status"] = "archived"
                    record["collection"] = "long-term"
                    grouped.setdefault((record["category"], True), []).append(record)

            for (category, archived), records in grouped.items():
                sections = self._load_sections(category, archived=archived)
                for record in records:
                    self._merge_legacy_record_into_sections(sections, record)
                self._save_sections(category, sections, archived=archived)

            archive_root = self.root / "archive" / "legacy-memories"
            for category in LEGACY_CATEGORIES:
                self._move_legacy_dir(self.root / "long-term" / category, archive_root / "long-term" / category)
                self._move_legacy_dir(self.root / "archive" / "memories" / category, archive_root / "archive" / category)

            marker.write_text(json.dumps({"migrated_at": _now(), "schema": SECTIONED_SCHEMA}, indent=2), encoding="utf-8")

    def _move_legacy_dir(self, source: Path, target: Path) -> None:
        if not source.exists() or not source.is_dir():
            return
        files = list(source.glob("*.md"))
        if not files:
            try:
                source.rmdir()
            except OSError:
                pass
            return
        target.mkdir(parents=True, exist_ok=True)
        for file_path in files:
            destination = target / file_path.name
            if destination.exists():
                destination = target / f"{file_path.stem}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}.md"
            shutil.move(str(file_path), str(destination))
        try:
            source.rmdir()
        except OSError:
            pass

    def _merge_legacy_record_into_sections(self, sections: list[Section], record: dict[str, Any]) -> None:
        query = str(record.get("content") or "")
        query_tokens = set(_tokens(query))
        best_index = -1
        best_score = 0.0
        for index, section in enumerate(sections):
            existing = self._section_to_record(section, str(record.get("category") or "fact"))
            score, _breakdown = self._score_record(query, query_tokens, existing)
            if score > best_score:
                best_score = score
                best_index = index
        if best_index >= 0 and best_score >= self.min_relevance_score:
            section = sections[best_index]
            section.body = self._dedupe_bullets(section.body, query)
            section.meta["updated_at"] = max(str(section.meta.get("updated_at") or ""), str(record.get("updated_at") or _now()))
            section.meta["importance"] = max(_coerce_int(section.meta.get("importance"), 5), _coerce_int(record.get("importance"), 5))
            return
        sections.append(self._record_to_section(record))

    def _target_path(self, meta: dict[str, Any]) -> Path:
        memory_id = _safe_id(str(meta.get("id") or "memory"))
        category = self._normalize_category(str(meta.get("category") or "fact"))
        status = str(meta.get("status") or "active")
        if status == "archived":
            if str(meta.get("collection") or "") == "short-term":
                return self.root / "archive" / "short-term" / f"{memory_id}.md"
            return self._archive_category_file(category)
        if str(meta.get("collection") or "") == "short-term":
            return self.root / "short-term" / f"{memory_id}.md"
        return self._category_file(category)

    def _normalize_category(self, category: str) -> str:
        return self._section_repository.normalize_category(category)

    def _category_file(self, category: str) -> Path:
        return self._section_repository.category_file(category)

    def _archive_category_file(self, category: str) -> Path:
        return self._section_repository.archive_category_file(category)

    def _section_default_meta(self, section_id: str, category: str) -> dict[str, Any]:
        return self._section_repository.section_default_meta(section_id, category)

    def _parse_sectioned(self, text: str, *, category: str = "fact") -> list[Section]:
        return self._section_repository.parse_sectioned(text, category=category)

    def _dump_sectioned(self, category: str, sections: list[Section]) -> str:
        return self._section_repository.dump_sectioned(category, sections)

    def _load_sections(self, category: str, *, archived: bool = False) -> list[Section]:
        return self._section_repository.load_sections(category, archived=archived)

    def _save_sections(self, category: str, sections: list[Section], *, archived: bool = False) -> None:
        self._section_repository.save_sections(category, sections, archived=archived)
        self._invalidate_records_cache()

    def _section_to_record(
        self,
        section: Section,
        category: str,
        *,
        status: str = "active",
        collection: str = "long-term",
        path: Path | None = None,
    ) -> dict[str, Any]:
        meta = dict(section.meta)
        meta["id"] = section.id
        meta["category"] = self._normalize_category(category)
        meta["status"] = status
        meta["collection"] = collection
        record = self._normalize_record(path or self._category_file(category), meta, section.body)
        record["_title"] = section.title
        return record

    def _record_to_section(self, record: dict[str, Any]) -> Section:
        memory_id = _safe_id(str(record.get("id") or self._new_memory_id(str(record.get("content") or ""), str(record.get("category") or "fact"))))
        title = str(record.get("_title") or self._title_from_content(str(record.get("content") or "")) or memory_id).strip()
        meta = self._record_meta({**record, "id": memory_id})
        return Section(id=memory_id, title=title, meta=meta, body=str(record.get("content") or "").strip())

    def _title_from_content(self, content: str) -> str:
        first = next((line.strip("-# ").strip() for line in str(content or "").splitlines() if line.strip()), "")
        return first[:80] or "Memory"

    def _remove_section_by_id(self, memory_id: str, *, include_archived: bool = True) -> bool:
        removed = self._section_repository.remove_section_by_id(
            memory_id,
            include_archived=include_archived,
        )
        if removed:
            self._invalidate_records_cache()
        return removed

    def _normalize_record(self, path: Path, meta: dict[str, Any], content: str) -> dict[str, Any]:
        now = _now()
        category = self._normalize_category(str(meta.get("category") or "fact"))
        status = str(meta.get("status") or "active").lower()
        if status not in VALID_STATUSES:
            status = "active"
        review_state = str(meta.get("review_state") or "new").lower()
        if review_state not in VALID_REVIEW_STATES:
            review_state = "new"
        kind = str(meta.get("kind") or "fact").lower()
        if kind not in VALID_KINDS:
            kind = "reflection" if category == "reflection" else "fact"
        memory_id = _safe_id(str(meta.get("id") or path.stem))
        return {
            "id": memory_id,
            "content": content.strip(),
            "category": category,
            "status": status,
            "review_state": review_state,
            "confidence": max(0.0, min(1.0, _coerce_float(meta.get("confidence"), 1.0))),
            "importance": max(1, min(10, _coerce_int(meta.get("importance"), 5))),
            "kind": kind,
            "source": str(meta.get("source") or ""),
            "source_conversation_id": str(meta.get("source_conversation_id") or ""),
            "source_message_id": meta.get("source_message_id"),
            "source_principal_id": str(meta.get("source_principal_id") or ""),
            "source_permission_profile_id": str(meta.get("source_permission_profile_id") or ""),
            "source_execution_source": str(meta.get("source_execution_source") or ""),
            "sensitivity": str(meta.get("sensitivity") or "normal"),
            "created_at": str(meta.get("created_at") or now),
            "updated_at": str(meta.get("updated_at") or now),
            "last_used_at": str(meta.get("last_used_at") or ""),
            "use_count": max(0, _coerce_int(meta.get("use_count"), 0)),
            "collection": str(meta.get("collection") or "long-term"),
            "_path": path,
        }

    def _record_meta(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record["id"],
            "category": record["category"],
            "kind": record["kind"],
            "status": record["status"],
            "review_state": record["review_state"],
            "confidence": record["confidence"],
            "importance": record["importance"],
            "source": record.get("source", ""),
            "source_conversation_id": record.get("source_conversation_id", ""),
            "source_message_id": record.get("source_message_id"),
            "source_principal_id": record.get("source_principal_id", ""),
            "source_permission_profile_id": record.get("source_permission_profile_id", ""),
            "source_execution_source": record.get("source_execution_source", ""),
            "sensitivity": record.get("sensitivity", "normal"),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "last_used_at": record.get("last_used_at", ""),
            "use_count": record.get("use_count", 0),
            "collection": record.get("collection", "long-term"),
        }

    def _public(self, record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if not key.startswith("_") and key != "collection"}

    def _memory_files(self, *, include_archived: bool = True, include_short_term: bool = True) -> list[Path]:
        roots: list[Path] = []
        if include_short_term:
            roots.append(self.root / "short-term")
            if include_archived:
                roots.append(self.root / "archive" / "short-term")
        paths: list[Path] = []
        for root in roots:
            if root.exists():
                paths.extend(sorted(root.rglob("*.md")))
        return paths

    def _invalidate_records_cache(self) -> None:
        self._records_cache.clear()
        self._search_documents_cache.clear()

    def _file_signature(self, path: Path) -> tuple[str, int, int]:
        try:
            stat = path.stat()
        except OSError:
            return (str(path), 0, 0)
        return (str(path), int(stat.st_mtime_ns), int(stat.st_size))

    def _records_cache_signature(self, *, include_archived: bool, include_short_term: bool) -> tuple[Any, ...]:
        files: list[tuple[str, int, int]] = []
        for category in VALID_CATEGORIES:
            files.append(self._file_signature(self._category_file(category)))
            if include_archived:
                files.append(self._file_signature(self._archive_category_file(category)))
        if include_short_term:
            files.extend(self._file_signature(path) for path in self._memory_files(include_short_term=True))
        return (include_archived, include_short_term, tuple(files))

    def _load_record(self, path: Path) -> dict[str, Any] | None:
        try:
            meta, content = _read_markdown(path)
            record = self._normalize_record(path, meta, content)
        except Exception as exc:
            logger.warning("Unable to read memory file %s: %s", path, exc)
            return None
        if not record["content"]:
            return None
        return record

    def _json_list(self, value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        try:
            loaded = json.loads(str(value or "[]"))
        except json.JSONDecodeError:
            return []
        return loaded if isinstance(loaded, list) else []

    def _db_rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            return [dict(row) for row in self._db.fetchall(sql, params)]
        except sqlite3.Error:
            return []

    def _db_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        try:
            row = self._db.fetchone(sql, params)
        except sqlite3.Error:
            return None
        return dict(row) if row is not None else None

    def _all_records(self, *, include_archived: bool = True, include_short_term: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            cache_key = self._records_cache_signature(
                include_archived=include_archived,
                include_short_term=include_short_term,
            )
            cached = self._records_cache.get(cache_key)
            if cached is not None:
                return [dict(record) for record in cached]

            records: list[dict[str, Any]] = []
            for category in VALID_CATEGORIES:
                active_path = self._category_file(category)
                records.extend(
                    self._section_to_record(section, category, status="active", path=active_path)
                    for section in self._load_sections(category)
                )
                if include_archived:
                    archive_path = self._archive_category_file(category)
                    records.extend(
                        self._section_to_record(section, category, status="archived", path=archive_path)
                        for section in self._load_sections(category, archived=True)
                    )
            if include_short_term:
                records.extend(
                    record
                    for path in self._memory_files(include_archived=True, include_short_term=True)
                    if (record := self._load_record(path)) is not None
                )
            records = sorted(records, key=lambda item: (item["updated_at"], item["id"]), reverse=True)
            self._records_cache[cache_key] = [dict(record) for record in records]
            return [dict(record) for record in records]

    def _search_documents(self, *, include_archived: bool, include_short_term: bool) -> list[SearchDocument]:
        with self._lock:
            cache_key = self._records_cache_signature(
                include_archived=include_archived,
                include_short_term=include_short_term,
            )
            cached = self._search_documents_cache.get(cache_key)
            if cached is not None:
                return cached

            documents: list[SearchDocument] = []
            for record in self._all_records(
                include_archived=include_archived,
                include_short_term=include_short_term,
            ):
                documents.append(self._search_document(record))
            self._search_documents_cache[cache_key] = documents
            return documents

    def _write_record(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            record = dict(record)
            record["category"] = self._normalize_category(str(record.get("category") or "fact"))
            record["status"] = str(record.get("status") or "active")
            record["review_state"] = str(record.get("review_state") or "new")
            record["kind"] = str(record.get("kind") or ("reflection" if record["category"] == "reflection" else "fact"))
            record["id"] = _safe_id(str(record.get("id") or self._new_memory_id(record["content"], record["category"])))
            record["created_at"] = str(record.get("created_at") or _now())
            record["updated_at"] = str(record.get("updated_at") or _now())
            if str(record.get("collection") or "") == "short-term":
                target = self._target_path(record)
                _write_markdown(target, self._record_meta(record), str(record.get("content") or ""))
                self._invalidate_records_cache()
                record["_path"] = target
                return self._public(record)
            archived = record["status"] == "archived"
            self._remove_section_by_id(record["id"], include_archived=True)
            sections = self._load_sections(record["category"], archived=archived)
            sections.append(self._record_to_section(record))
            self._save_sections(record["category"], sections, archived=archived)
            record["_path"] = self._archive_category_file(record["category"]) if archived else self._category_file(record["category"])
            return self._public(record)

    def _new_memory_id(self, content: str, category: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        digest = _content_hash(content)[:10]
        return f"{stamp}-{_slug(content, category)}-{digest}"

    def _find(self, memory_id: str) -> dict[str, Any] | None:
        wanted = _safe_id(str(memory_id))
        for record in self._all_records(include_archived=True):
            if record["id"] == wanted:
                return record
        return None

    def _find_exact_content(self, content: str) -> dict[str, Any] | None:
        digest = _content_hash(content)
        for record in self._all_records(include_archived=True):
            if _content_hash(record["content"]) == digest:
                return record
        return None

    def _dedupe_bullets(self, existing: str, addition: str) -> str:
        lines = [line.rstrip() for line in str(existing or "").splitlines() if line.strip()]
        normalized = {" ".join(line.lstrip("-* ").split()).lower() for line in lines}
        for raw_line in str(addition or "").splitlines() or [str(addition or "")]:
            text = raw_line.strip()
            if not text:
                continue
            key = " ".join(text.lstrip("-* ").split()).lower()
            if key in normalized:
                continue
            lines.append(text if text.startswith(("-", "*")) else f"- {text}")
            normalized.add(key)
        return "\n".join(lines).strip()

    def _heuristic_merge_decision(self, category: str, sections: list[Section], text: str) -> dict[str, Any]:
        best_section: Section | None = None
        best_similarity = 0.0
        for section in sections:
            record = self._section_to_record(section, category)
            similarity = self._lexical_similarity(record["content"], text)
            if similarity > best_similarity:
                best_section = section
                best_similarity = similarity
        if best_section is not None and best_similarity >= HEURISTIC_MERGE_MIN_SIMILARITY:
            return {
                "action": "merge",
                "section_id": best_section.id,
                "merged_body": self._dedupe_bullets(best_section.body, text),
                "reasoning": "heuristic_near_duplicate",
            }
        return {
            "action": "create",
            "new_title": self._title_from_content(text),
            "new_body": text if text.startswith(("-", "*")) else f"- {text}",
            "reasoning": "heuristic_new_section",
        }

    async def _llm_merge_decision_async(self, category: str, sections: list[Section], text: str) -> dict[str, Any] | None:
        if self.llm_client is None or not sections:
            return None
        section_list = [
            {
                "id": section.id,
                "title": section.title,
                "body_summary": " ".join(section.body.split())[:700],
            }
            for section in sections
        ]
        prompt = (
            "Merge a new durable memory into sectioned markdown storage. "
            "Return strict JSON only with action merge or create. "
            "For merge include section_id and merged_body. For create include new_title and new_body. "
            "Keep bodies concise markdown bullet lists and remove duplicates.\n\n"
            f"Category: {category}\n"
            f"Existing sections: {json.dumps(section_list, ensure_ascii=False)}\n"
            f"New memory: {text}"
        )
        try:
            raw = await self.llm_client.chat(
                [{"role": "user", "content": prompt}],
                system_prompt="You maintain Monaw's long-term memory. Return strict JSON only.",
            )
        except Exception as exc:
            logger.debug("Memory merge LLM unavailable: %s", exc)
            return None
        try:
            payload = json.loads(str(raw))
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", str(raw), re.S)
            if match is None:
                return None
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        if not isinstance(payload, dict):
            return None
        action = str(payload.get("action") or "").lower()
        if action == "merge" and payload.get("section_id") and payload.get("merged_body"):
            return payload
        if action == "create" and payload.get("new_title") and payload.get("new_body"):
            return payload
        return None

    def _llm_merge_decision(self, category: str, sections: list[Section], text: str) -> dict[str, Any] | None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._llm_merge_decision_async(category, sections, text))
        result: dict[str, Any] | None = None

        def run_in_thread() -> None:
            nonlocal result
            result = asyncio.run(self._llm_merge_decision_async(category, sections, text))

        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()
        thread.join(timeout=30)
        if thread.is_alive():
            logger.debug("Memory merge LLM timed out inside active event loop.")
            return None
        return result

    def remember(
        self,
        content: str,
        *,
        category: str = "fact",
        confidence: float = 1.0,
        review_state: str = "reviewed",
        importance: int = 5,
        kind: str = "fact",
        source: str = "",
        source_conversation_id: str = "",
        source_message_id: int | None = None,
        source_principal_id: str = "",
        source_permission_profile_id: str = "",
        source_execution_source: str = "",
        memory_id: str | None = None,
    ) -> dict[str, Any] | None:
        text = str(content or "").strip()
        category = self._normalize_category(category)
        confidence = max(0.0, min(1.0, float(confidence)))
        if kind not in VALID_KINDS:
            kind = "reflection" if category == "reflection" else "fact"
        if review_state not in VALID_REVIEW_STATES:
            review_state = "new"
        if not self._is_storable(text, confidence=confidence):
            self._audit(action="REJECT", reason="safety_or_quality_filter", candidate_content=text[:1000], source_conversation_id=source_conversation_id)
            return None

        now = _now()
        existing = self._find(memory_id) if memory_id else self._find_exact_content(text)
        if existing is not None:
            existing.update(
                {
                    "content": text,
                    "category": category,
                    "status": "active",
                    "review_state": review_state,
                    "confidence": max(float(existing.get("confidence", 0)), confidence),
                    "importance": max(_coerce_int(existing.get("importance"), 5), max(1, min(10, int(importance)))),
                    "kind": kind,
                    "source": source or existing.get("source", ""),
                    "source_conversation_id": source_conversation_id or existing.get("source_conversation_id", ""),
                    "source_message_id": source_message_id if source_message_id is not None else existing.get("source_message_id"),
                    "source_principal_id": source_principal_id or existing.get("source_principal_id", ""),
                    "source_permission_profile_id": source_permission_profile_id or existing.get("source_permission_profile_id", ""),
                    "source_execution_source": source_execution_source or existing.get("source_execution_source", ""),
                    "sensitivity": "sensitive" if self._contains_sensitive(text) else existing.get("sensitivity", "normal"),
                    "updated_at": now,
                    "collection": "long-term",
                }
            )
            saved = self._write_record(existing)
            self._audit(action="UPDATE", reason="refreshed_existing_memory", memory_id=saved["id"], source_conversation_id=source_conversation_id, candidate_content=text[:1000])
            return saved

        with self._lock:
            sections = self._load_sections(category)
            decision = self._llm_merge_decision(category, sections, text) if self.llm_client is not None and sections else None
            if decision is None:
                decision = self._heuristic_merge_decision(category, sections, text)
            action = str(decision.get("action") or "create").lower()

            if action == "merge":
                wanted = _safe_id(str(decision.get("section_id") or ""))
                for index, section in enumerate(sections):
                    if section.id != wanted:
                        continue
                    section.body = str(decision.get("merged_body") or section.body).strip()
                    section.meta.update(
                        {
                            "updated_at": now,
                            "status": "active",
                            "review_state": review_state,
                            "confidence": max(_coerce_float(section.meta.get("confidence"), 1.0), confidence),
                            "importance": max(_coerce_int(section.meta.get("importance"), 5), max(1, min(10, int(importance)))),
                            "kind": kind,
                            "source": source or section.meta.get("source", ""),
                            "source_conversation_id": source_conversation_id or section.meta.get("source_conversation_id", ""),
                            "source_message_id": source_message_id if source_message_id is not None else section.meta.get("source_message_id"),
                            "source_principal_id": source_principal_id or section.meta.get("source_principal_id", ""),
                            "source_permission_profile_id": source_permission_profile_id or section.meta.get("source_permission_profile_id", ""),
                            "source_execution_source": source_execution_source or section.meta.get("source_execution_source", ""),
                            "sensitivity": "sensitive" if self._contains_sensitive(section.body) else section.meta.get("sensitivity", "normal"),
                        }
                    )
                    sections[index] = section
                    self._save_sections(category, sections)
                    saved = self._public(self._section_to_record(section, category))
                    self._audit(action="MERGE_INTO_SECTION", reason=str(decision.get("reasoning") or "llm_merge"), memory_id=saved["id"], source_conversation_id=source_conversation_id, candidate_content=text[:1000])
                    return saved

            section_id = _safe_id(memory_id or f"sec_{self._new_memory_id(text, category)}")
            record = {
                "id": section_id,
                "content": str(decision.get("new_body") or text).strip(),
                "category": category,
                "status": "active",
                "review_state": review_state,
                "confidence": confidence,
                "importance": max(1, min(10, int(importance))),
                "kind": kind,
                "source": source,
                "source_conversation_id": source_conversation_id,
                "source_message_id": source_message_id,
                "source_principal_id": source_principal_id,
                "source_permission_profile_id": source_permission_profile_id,
                "source_execution_source": source_execution_source,
                "sensitivity": "sensitive" if self._contains_sensitive(str(decision.get("new_body") or text)) else "normal",
                "created_at": now,
                "updated_at": now,
                "last_used_at": "",
                "use_count": 0,
                "collection": "long-term",
                "_title": str(decision.get("new_title") or self._title_from_content(text)),
            }
            sections.append(self._record_to_section(record))
            self._save_sections(category, sections)
            saved = self._public(self._section_to_record(sections[-1], category))
            self._audit(action="CREATE_SECTION", reason=source or str(decision.get("reasoning") or "remember"), memory_id=saved["id"], source_conversation_id=source_conversation_id, candidate_content=text[:1000])
            return saved

    def get(self, memory_id: str | int) -> dict[str, Any] | None:
        record = self._find(str(memory_id))
        return self._public(record) if record is not None else None

    def list_memories(
        self,
        *,
        query: str = "",
        category: str = "",
        status: str = "active",
        review_state: str = "",
        limit: int = 100,
        include_short_term: bool = False,
    ) -> list[dict[str, Any]]:
        records = self._all_records(include_archived=True, include_short_term=include_short_term)
        if category:
            wanted_category = self._normalize_category(category)
            records = [record for record in records if record["category"] == wanted_category]
        if status:
            records = [record for record in records if record["status"] == status]
        if review_state:
            records = [record for record in records if record["review_state"] == review_state]
        if query.strip():
            scored = self.score(query, category=category, include_archived=(status != "active"), review_state=review_state, limit=max(limit, 100), mark_used=False)
            allowed = {item["id"] for item in scored}
            records = [record for record in records if record["id"] in allowed]
            records.sort(key=lambda item: next((score["score"] for score in scored if score["id"] == item["id"]), 0), reverse=True)
        else:
            records.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
        return [self._public(record) for record in records[: max(1, min(500, int(limit)))]]

    def _section_payload(self, section: Section, category: str, *, archived: bool = False) -> dict[str, Any]:
        record = self._section_to_record(
            section,
            category,
            status="archived" if archived else "active",
            path=self._archive_category_file(category) if archived else self._category_file(category),
        )
        return {
            **self._public(record),
            "title": section.title,
            "body": section.body,
        }

    def memory_file(self, category: str) -> dict[str, Any]:
        normalized = self._normalize_category(category)
        path = self._category_file(normalized)
        raw = path.read_text(encoding="utf-8") if path.exists() else self._dump_sectioned(normalized, [])
        sections = self._load_sections(normalized)
        meta, _body = _split_markdown(raw)
        return {
            "category": normalized,
            "raw_markdown": raw,
            "sections": [self._section_payload(section, normalized) for section in sections],
            "updated_at": str(meta.get("updated_at") or ""),
        }

    def save_memory_file(self, category: str, raw_markdown: str) -> dict[str, Any]:
        normalized = self._normalize_category(category)
        sections = self._parse_sectioned(raw_markdown, category=normalized)
        self._save_sections(normalized, sections)
        self._audit(action="UPDATE", reason="manual_file_update", source_conversation_id="", candidate_content=f"{normalized}.md")
        return self.memory_file(normalized)

    def update_section(self, section_id: str, **fields) -> dict[str, Any] | None:
        wanted = _safe_id(str(section_id))
        allowed = {"title", "body", "importance", "review_state"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported section update field(s): {', '.join(sorted(unknown))}")
        for category in VALID_CATEGORIES:
            for archived in (False, True):
                sections = self._load_sections(category, archived=archived)
                for index, section in enumerate(sections):
                    if section.id != wanted:
                        continue
                    if "body" in fields and fields["body"] is not None:
                        body = str(fields["body"]).strip()
                        if not self._is_storable(body, confidence=_coerce_float(section.meta.get("confidence"), 1.0)):
                            raise ValueError("Memory content was rejected by safety filters")
                        section.body = body
                    if "title" in fields and fields["title"] is not None:
                        section.title = str(fields["title"]).strip() or section.title
                    if "importance" in fields and fields["importance"] is not None:
                        section.meta["importance"] = max(1, min(10, int(fields["importance"])))
                    if "review_state" in fields and fields["review_state"] is not None:
                        review_state = str(fields["review_state"])
                        if review_state not in VALID_REVIEW_STATES:
                            raise ValueError("Invalid review state")
                        section.meta["review_state"] = review_state
                    section.meta["updated_at"] = _now()
                    sections[index] = section
                    self._save_sections(category, sections, archived=archived)
                    payload = self._section_payload(section, category, archived=archived)
                    self._audit(action="UPDATE", reason="manual_section_update", memory_id=section.id, source_conversation_id="", candidate_content=section.body[:1000])
                    return payload
        return None

    def search(
        self,
        query: str,
        *,
        category: str = "",
        limit: int = 6,
        mark_used: bool = True,
        include_short_term: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in item.items() if key not in {"score", "score_breakdown"}}
            for item in self.score(
                query,
                category=category,
                include_archived=False,
                limit=limit,
                mark_used=mark_used,
                include_short_term=include_short_term,
            )
        ]

    def score(
        self,
        query: str,
        *,
        category: str = "",
        include_archived: bool = False,
        review_state: str = "",
        limit: int = 10,
        mark_used: bool = False,
        include_short_term: bool = False,
    ) -> list[dict[str, Any]]:
        query_text = str(query or "").strip()
        query_tokens = set(_tokens(query_text))
        documents = self._search_documents(
            include_archived=include_archived,
            include_short_term=include_short_term,
        )
        if category:
            normalized_category = self._normalize_category(category)
            documents = [document for document in documents if document.record["category"] == normalized_category]
        if not include_archived:
            documents = [document for document in documents if document.record["status"] == "active"]
        if review_state:
            documents = [document for document in documents if document.record["review_state"] == review_state]

        scored: list[dict[str, Any]] = []
        for document in documents:
            score, breakdown = self._score_document(query_text, query_tokens, document)
            if query_text and score <= 0:
                continue
            item = self._public(document.record)
            item["score"] = round(score, 6)
            item["score_breakdown"] = {key: round(value, 6) for key, value in breakdown.items()}
            scored.append(item)

        scored.sort(key=lambda item: (-float(item["score"]), -int(item.get("importance", 0)), str(item.get("updated_at", "")), str(item.get("id", ""))))
        result = scored[: max(1, min(100, int(limit)))]
        if mark_used:
            self.mark_used([item["id"] for item in result])
        return result

    def _record_haystack(self, record: dict[str, Any]) -> str:
        content = str(record.get("content") or "")
        return content.lower()

    def _tokenized_record(self, record: dict[str, Any]) -> tuple[str, frozenset[str]]:
        key = (
            str(record.get("id") or ""),
            str(record.get("category") or ""),
            str(record.get("kind") or ""),
            str(record.get("content") or ""),
        )
        cached = self._tokenized_record_cache.get(key)
        if cached is not None:
            return cached
        haystack = self._record_haystack(record)
        tokenized = (haystack, frozenset(_tokens(haystack)))
        if len(self._tokenized_record_cache) >= MAX_TOKENIZED_RECORD_CACHE:
            self._tokenized_record_cache.pop(next(iter(self._tokenized_record_cache)))
        self._tokenized_record_cache[key] = tokenized
        return tokenized

    def _search_document(self, record: dict[str, Any]) -> SearchDocument:
        haystack, tokens = self._tokenized_record(record)
        return SearchDocument(
            record=record,
            haystack=haystack,
            tokens=tokens,
            category=str(record.get("category") or ""),
            importance_score=max(0.0, min(1.0, _coerce_int(record.get("importance"), 5) / 10)),
            use_score=max(0.0, min(1.0, _coerce_int(record.get("use_count"), 0) / 10)),
            recency_score=self._recency_score(str(record.get("updated_at") or record.get("created_at") or "")),
        )

    def _score_record(
        self,
        query: str,
        query_tokens: set[str],
        record: dict[str, Any],
    ) -> tuple[float, dict[str, float]]:
        return self._score_document(query, query_tokens, self._search_document(record))

    def _score_document(
        self,
        query: str,
        query_tokens: set[str],
        document: SearchDocument,
    ) -> tuple[float, dict[str, float]]:
        record = document.record
        if not query:
            exact_score = 0.0
            token_score = 0.0
        else:
            exact_score = 1.0 if query.lower() in document.haystack else 0.0
            overlap = query_tokens & document.tokens
            token_score = len(overlap) / max(1, len(document.tokens))

        category_score = 1.0 if document.category in query_tokens else 0.0
        lexical_score = exact_score * 0.35 + token_score * 0.35 + category_score * 0.05
        if exact_score > 0 or token_score > 0:
            score = lexical_score + (
                document.recency_score * 0.10
                + document.importance_score * 0.10
                + document.use_score * 0.05
            )
        else:
            score = 0.0
        return score, {
            "exact": exact_score,
            "token": token_score,
            "category": category_score,
            "recency": document.recency_score,
            "importance": document.importance_score,
            "use": document.use_score,
        }

    def _recency_score(self, value: str) -> float:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
        return 1.0 / (1.0 + (age_days / 30.0))

    def retrieve_for_turn(self, query: str, *, mark_used: bool = True) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        results = self.score(
            query,
            limit=self.retrieval_limit,
            mark_used=mark_used,
            include_short_term=False,
        )
        return [item for item in results if float(item.get("score", 0)) >= self.min_relevance_score]

    def build_prompt(self, query: str, *, mark_used: bool = True) -> str:
        if not self.enabled:
            return ""

        lines = ["## What I know about you"]
        used_chars = len(lines[0]) + 1
        injected_ids: list[str] = []

        personality_lines = self._personality_prompt_lines()
        if personality_lines:
            used_chars = self._append_prompt_section(lines, "Personality", personality_lines, used_chars)

        identity_memories = self._identity_memories(limit=self.retrieval_limit)
        if identity_memories:
            entries = [self._prompt_content(item["content"]) for item in identity_memories]
            used_chars = self._append_prompt_section(lines, "Stable Preferences", entries, used_chars)
            injected_ids.extend([item["id"] for item in identity_memories])

        contextual = [
            item
            for item in self.retrieve_for_turn(query, mark_used=False)
            if item["id"] not in set(injected_ids)
        ]
        if contextual:
            entries = [self._prompt_content(item["content"]) for item in contextual]
            used_chars = self._append_prompt_section(lines, "Relevant Memory", entries, used_chars)
            injected_ids.extend([item["id"] for item in contextual])

        if len(lines) == 1:
            return ""
        if mark_used:
            self.mark_used(injected_ids)
            self._audit(
                action="INJECT",
                reason="prompt_context",
                candidate_content=json.dumps(
                    {
                        "identity_count": len(identity_memories),
                        "contextual_count": len(contextual),
                        "memory_ids": injected_ids,
                    },
                    ensure_ascii=False,
                ),
            )
        return "\n".join(lines)[: self.max_injected_chars].rstrip()

    def _append_prompt_section(self, lines: list[str], title: str, entries: list[str], used_chars: int) -> int:
        if not entries:
            return used_chars
        section_header = f"### {title}"
        pending = [section_header]
        budget = self.max_injected_chars
        for entry in entries:
            bullet = f"- {entry.strip()}"
            projected = used_chars + sum(len(line) + 1 for line in pending) + len(bullet) + 1
            if projected > budget:
                break
            pending.append(bullet)
        if len(pending) > 1:
            lines.extend(pending)
            used_chars += sum(len(line) + 1 for line in pending)
        return used_chars

    def _personality_prompt_lines(self) -> list[str]:
        lines: list[str] = []
        for path in sorted((self.root / "personalities").glob("*.md")):
            try:
                meta, content = _read_markdown(path)
            except Exception:
                continue
            text = " ".join(content.split()).strip()
            if not text:
                continue
            name = str(meta.get("display_name") or meta.get("id") or path.stem).strip()
            lines.append(f"{name}: {text}")
        return lines[:4]

    def _identity_memories(self, *, limit: int) -> list[dict[str, Any]]:
        records = [
            record
            for record in self._all_records(include_archived=False, include_short_term=False)
            if record["category"] == "preference" and int(record["importance"]) >= 7
        ]
        records.sort(key=lambda item: (-int(item["importance"]), item["updated_at"]), reverse=False)
        return records[: max(1, int(limit))]

    def _prompt_content(self, content: str) -> str:
        text = re.sub(r"^[-*]\s+", "", " ".join(str(content or "").split()))
        text = re.sub(r"(?i)\b(ignore|override|forget)\b[^.?!]*(previous|system|developer|policy|instruction)[^.?!]*[.?!]?", "", text).strip()
        replacements = [
            (re.compile(r"^The user prefers\b", re.I), "You prefer"),
            (re.compile(r"^The user likes\b", re.I), "You like"),
            (re.compile(r"^The user works\b", re.I), "You work"),
            (re.compile(r"^The user's\b", re.I), "Your"),
            (re.compile(r"^The user\b", re.I), "You"),
        ]
        for pattern, replacement in replacements:
            text = pattern.sub(replacement, text)
        return text

    def update(self, memory_id: str | int, **fields) -> dict[str, Any] | None:
        record = self._find(str(memory_id))
        if record is None:
            return None
        allowed = {
            "content",
            "category",
            "status",
            "review_state",
            "confidence",
            "importance",
            "kind",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported memory update field(s): {', '.join(sorted(unknown))}")
        if "content" in fields and not self._is_storable(str(fields["content"]), confidence=_coerce_float(fields.get("confidence", record["confidence"]), record["confidence"])):
            raise ValueError("Memory content was rejected by safety filters")
        record.update({key: value for key, value in fields.items() if value is not None})
        record["category"] = self._normalize_category(str(record.get("category") or "fact"))
        record["updated_at"] = _now()
        saved = self._write_record(record)
        self._audit(action="UPDATE", reason="manual_update", memory_id=saved["id"], source_conversation_id=saved.get("source_conversation_id", ""), candidate_content=str(saved.get("content", ""))[:1000])
        return saved

    def archive(self, memory_id: str | int) -> dict[str, Any] | None:
        return self.update(memory_id, status="archived")

    def delete(self, memory_id: str | int) -> bool:
        record = self._find(str(memory_id))
        if record is None:
            return False
        if str(record.get("collection") or "") == "short-term":
            path = record.get("_path")
            if isinstance(path, Path) and path.exists():
                path.unlink()
        else:
            self._remove_section_by_id(record["id"], include_archived=True)
        with self._lock:
            try:
                self._db.execute("DELETE FROM memory_audit WHERE memory_id = ?", (record["id"],))
                self._db.commit()
            except sqlite3.Error:
                logger.debug("Unable to remove memory audit rows for %s", record["id"])
        self._audit(
            action="DELETE",
            reason="manual_delete",
            memory_id=record["id"],
            source_conversation_id=record.get("source_conversation_id", ""),
        )
        return True

    def mark_used(self, memory_ids: list[str | int]) -> None:
        if not memory_ids:
            return
        wanted = {_safe_id(str(memory_id)) for memory_id in memory_ids}
        now = _now()
        with self._lock:
            changed = False
            for category in VALID_CATEGORIES:
                sections = self._load_sections(category)
                category_changed = False
                for section in sections:
                    if section.id not in wanted:
                        continue
                    section.meta["use_count"] = _coerce_int(section.meta.get("use_count"), 0) + 1
                    section.meta["last_used_at"] = now
                    category_changed = True
                    changed = True
                if category_changed:
                    self._save_sections(category, sections)

            for path in self._memory_files(include_archived=False, include_short_term=True):
                record = self._load_record(path)
                if record is None or record["id"] not in wanted:
                    continue
                record["use_count"] = int(record.get("use_count") or 0) + 1
                record["last_used_at"] = now
                _write_markdown(path, self._record_meta(record), record["content"])
                changed = True

            if changed:
                self._invalidate_records_cache()

    def _candidate_public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(row.get("id") or ""),
            "kind": str(row.get("kind") or "fact"),
            "content": str(row.get("content") or ""),
            "category": self._normalize_category(str(row.get("category") or "fact")),
            "confidence": _coerce_float(row.get("confidence"), 0.8),
            "importance": _coerce_int(row.get("importance"), 5),
            "status": str(row.get("status") or "new"),
            "reason": str(row.get("reason") or ""),
            "source_conversation_id": str(row.get("source_conversation_id") or ""),
            "source_message_id": row.get("source_message_id"),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }

    def add_candidate(
        self,
        content: str,
        *,
        category: str = "fact",
        kind: str = "fact",
        confidence: float = 0.8,
        importance: int = 5,
        reason: str = "",
        source_conversation_id: str = "",
        source_message_id: int | None = None,
    ) -> dict[str, Any] | None:
        text = str(content or "").strip()
        confidence = max(0.0, min(1.0, float(confidence)))
        if not self._is_storable(text, confidence=confidence):
            self._audit(action="REJECT_CANDIDATE", reason="safety_or_quality_filter", source_conversation_id=source_conversation_id, candidate_content=text[:1000])
            return None
        category = self._normalize_category(category)
        if kind not in VALID_KINDS:
            kind = "reflection" if category == "reflection" else "fact"
        candidate_id = _safe_id(f"cand-{source_conversation_id}-{category}-{_content_hash(text)[:16]}")
        now = _now()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO memory_candidates (
                    id, kind, content, category, confidence, importance, status,
                    reason, source_conversation_id, source_message_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    confidence = MAX(memory_candidates.confidence, excluded.confidence),
                    importance = MAX(memory_candidates.importance, excluded.importance),
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (
                    candidate_id,
                    kind,
                    text,
                    category,
                    confidence,
                    max(1, min(10, int(importance))),
                    reason,
                    source_conversation_id,
                    source_message_id,
                    now,
                    now,
                ),
            )
            self._db.commit()
        self._audit(action="CANDIDATE", reason=reason or "turn_capture", source_conversation_id=source_conversation_id, candidate_content=text[:1000])
        row = self._db_one("SELECT * FROM memory_candidates WHERE id = ?", (candidate_id,))
        return self._candidate_public(row) if row else None

    def list_candidates(self, *, status: str = "new", limit: int = 100) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if status:
            where = "WHERE status = ?"
            params.append(status)
        rows = self._db_rows(
            f"""
            SELECT * FROM memory_candidates
            {where}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            tuple(params + [max(1, min(500, int(limit)))]),
        )
        return [self._candidate_public(row) for row in rows]

    def update_candidate(self, candidate_id: str, *, status: str, approve: bool = False) -> dict[str, Any] | None:
        row = self._db_one("SELECT * FROM memory_candidates WHERE id = ?", (_safe_id(candidate_id),))
        if row is None:
            return None
        if status not in {"new", "approved", "rejected"}:
            raise ValueError("Invalid candidate status")
        if approve:
            memory = self.remember(
                str(row.get("content") or ""),
                category=str(row.get("category") or "fact"),
                confidence=_coerce_float(row.get("confidence"), 0.8),
                review_state="reviewed",
                importance=_coerce_int(row.get("importance"), 5),
                kind=str(row.get("kind") or "fact"),
                source="candidate",
                source_conversation_id=str(row.get("source_conversation_id") or ""),
                source_message_id=row.get("source_message_id"),
            )
            # remember() returns None when the content fails the storability/safety
            # filter. Surface that as an error rather than silently flipping a
            # user-requested approval to "rejected"; leave the candidate as-is so
            # the user can edit it or reject it explicitly.
            if memory is None:
                raise ValueError(
                    "Candidate could not be stored: its content was rejected by the "
                    "memory safety/quality filter."
                )
            status = "approved"
        with self._lock:
            self._db.execute(
                "UPDATE memory_candidates SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), _safe_id(candidate_id)),
            )
            self._db.commit()
        row = self._db_one("SELECT * FROM memory_candidates WHERE id = ?", (_safe_id(candidate_id),))
        return self._candidate_public(row) if row else None

    def capture_turn_candidates(
        self,
        *,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
    ) -> list[dict[str, Any]]:
        if not self.enabled or not self.auto_learn_enabled or self.write_policy in {"off", "manual"}:
            return []
        messages = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_message},
        ]
        decisions = self._heuristic_curator_decisions(messages)
        captured: list[dict[str, Any]] = []
        auto_reviewed = self.write_policy == "auto_reviewed"

        for decision in decisions:
            action = str(decision.get("action") or "").strip().lower()
            content = str(decision.get("content") or "").strip()
            confidence = _coerce_float(decision.get("confidence"), 0.8)
            importance = _coerce_int(decision.get("importance"), 5)

            if action == "remember" and content:
                if auto_reviewed:
                    memory = self.remember(
                        content,
                        category=str(decision.get("category") or "fact"),
                        confidence=confidence,
                        review_state="reviewed",
                        importance=importance,
                        kind=str(decision.get("kind") or "fact"),
                        source="turn_feature_extract",
                        source_conversation_id=conversation_id,
                    )
                    if memory is not None:
                        captured.append(memory)
                else:
                    candidate = self.add_candidate(
                        content,
                        category=str(decision.get("category") or "fact"),
                        confidence=confidence,
                        importance=importance,
                        kind=str(decision.get("kind") or "fact"),
                        reason="turn_feature_extract",
                        source_conversation_id=conversation_id,
                    )
                    if candidate is not None:
                        captured.append(candidate)
                continue

            if action == "personality_update" and content:
                if auto_reviewed:
                    if self._update_personality(
                        str(decision.get("target") or "user"),
                        content,
                        conversation_id,
                    ):
                        captured.append(
                            {
                                "id": f"personality-{_safe_id(str(decision.get('target') or 'user'))}",
                                "kind": "personality",
                                "content": content,
                            }
                        )
                else:
                    candidate = self.add_candidate(
                        content,
                        category="fact",
                        confidence=confidence,
                        importance=importance,
                        kind="fact",
                        reason="turn_feature_extract",
                        source_conversation_id=conversation_id,
                    )
                    if candidate is not None:
                        captured.append(candidate)

        return captured

    def upsert_checkpoint(self, checkpoint_id: str, **fields: Any) -> dict[str, Any]:
        cid = _safe_id(checkpoint_id)
        now = _now()
        payload = {
            "id": cid,
            "scope": str(fields.get("scope") or "conversation"),
            "status": str(fields.get("status") or "active"),
            "conversation_id": str(fields.get("conversation_id") or ""),
            "goal": str(fields.get("goal") or ""),
            "last_known_state": str(fields.get("last_known_state") or ""),
            "next_action": str(fields.get("next_action") or ""),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._db.execute(
                """
                INSERT INTO memory_checkpoints (
                    id, scope, status, conversation_id, goal, last_known_state,
                    next_action, created_at, updated_at
                )
                VALUES (
                    :id, :scope, :status, :conversation_id, :goal, :last_known_state,
                    :next_action, :created_at, :updated_at
                )
                ON CONFLICT(id) DO UPDATE SET
                    scope = excluded.scope,
                    status = excluded.status,
                    conversation_id = excluded.conversation_id,
                    goal = excluded.goal,
                    last_known_state = excluded.last_known_state,
                    next_action = excluded.next_action,
                    updated_at = excluded.updated_at
                """,
                payload,
            )
            self._db.commit()
        row = self._db_one("SELECT * FROM memory_checkpoints WHERE id = ?", (cid,))
        return self._checkpoint_public(row or payload)

    def list_checkpoints(self, *, status: str = "active", limit: int = 100) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if status:
            where = "WHERE status = ?"
            params.append(status)
        rows = self._db_rows(
            f"""
            SELECT * FROM memory_checkpoints
            {where}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            tuple(params + [max(1, min(500, int(limit)))]),
        )
        return [self._checkpoint_public(row) for row in rows]

    def _checkpoint_public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(row.get("id") or ""),
            "scope": str(row.get("scope") or "conversation"),
            "status": str(row.get("status") or "active"),
            "conversation_id": str(row.get("conversation_id") or ""),
            "goal": str(row.get("goal") or ""),
            "last_known_state": str(row.get("last_known_state") or ""),
            "next_action": str(row.get("next_action") or ""),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }

    def list_episodes(self, *, conversation_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if conversation_id:
            where = "WHERE conversation_id = ?"
            params.append(conversation_id)
        rows = self._db_rows(
            f"""
            SELECT * FROM memory_episodes
            {where}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            tuple(params + [max(1, min(500, int(limit)))]),
        )
        return [self._episode_public(row) for row in rows]

    def _episode_public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(row.get("id") or ""),
            "conversation_id": str(row.get("conversation_id") or ""),
            "channel": str(row.get("channel") or "desktop"),
            "summary": str(row.get("summary") or ""),
            "artifacts": self._json_list(row.get("artifacts_json")),
            "errors": self._json_list(row.get("errors_json")),
            "source_message_start_id": row.get("source_message_start_id"),
            "source_message_end_id": row.get("source_message_end_id"),
            "tool_call_ids": self._json_list(row.get("tool_call_ids_json")),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }

    def stats(self) -> dict[str, Any]:
        records = self._all_records(include_archived=True, include_short_term=True)
        active = [record for record in records if record["status"] == "active"]
        archived = [record for record in records if record["status"] == "archived"]
        short_term = list((self.root / "short-term").glob("*.md"))
        personalities = list((self.root / "personalities").glob("*.md"))
        curated_sessions = list((self.root / ".system" / "curated").glob("*.json"))
        candidate_counts = {
            str(row.get("status") or ""): int(row.get("count") or 0)
            for row in self._db_rows("SELECT status, COUNT(*) AS count FROM memory_candidates GROUP BY status")
        }
        checkpoint_counts = {
            str(row.get("status") or ""): int(row.get("count") or 0)
            for row in self._db_rows("SELECT status, COUNT(*) AS count FROM memory_checkpoints GROUP BY status")
        }
        episode_count = int((self._db_one("SELECT COUNT(*) AS count FROM memory_episodes") or {}).get("count") or 0)
        audit_count = int((self._db_one("SELECT COUNT(*) AS count FROM memory_audit") or {}).get("count") or 0)
        audit_counters = {
            str(row.get("action") or ""): int(row.get("count") or 0)
            for row in self._db_rows("SELECT action, count FROM memory_audit_counters")
        }
        category_counts = {
            category: len([record for record in active if record["category"] == category])
            for category in VALID_CATEGORIES
        }
        candidate_count = sum(candidate_counts.values())
        unresolved_count = int(candidate_counts.get("new", 0))
        return {
            "total": len(records),
            "active": len(active),
            "archived": len(archived),
            "new": len([record for record in records if record["review_state"] == "new"]),
            "reviewed": len([record for record in records if record["review_state"] == "reviewed"]),
            "categories": category_counts,
            **category_counts,
            "fact": len([record for record in records if record["kind"] == "fact"]),
            "reflection": len([record for record in records if record["kind"] == "reflection"]),
            "candidates": candidate_count,
            "unresolved_candidates": unresolved_count,
            "short_term": len(short_term),
            "personalities": len(personalities),
            "curated_sessions": len(curated_sessions),
            "audit_events": audit_count,
            "archived_messages": self._db.count_archived_messages(),
            "episodes": episode_count,
            "active_checkpoints": int(checkpoint_counts.get("active", 0)),
            "audit_counters": audit_counters,
            "memory_root": str(self.root),
        }

    async def learn_from_turn(
        self,
        *,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
    ) -> list[dict[str, Any]]:
        return self.capture_turn_candidates(
            conversation_id=conversation_id,
            user_message=user_message,
            assistant_message=assistant_message,
        )

    async def curate_session(self, conversation_id: str) -> dict[str, int]:
        return await self._curate_session(conversation_id, allow_llm=True)

    def reconcile_session(self, conversation_id: str) -> dict[str, int]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._curate_session(conversation_id, allow_llm=True))
        logger.debug("Running synchronous memory curation inside an active event loop.")
        return self._curate_session_sync(conversation_id)

    def _curate_session_sync(self, conversation_id: str) -> dict[str, int]:
        return self._apply_curator_decisions(
            conversation_id,
            self._heuristic_curator_decisions(self._conversation_messages(conversation_id)),
            self._conversation_messages(conversation_id),
        )

    async def _curate_session(self, conversation_id: str, *, allow_llm: bool) -> dict[str, int]:
        messages = self._conversation_messages(conversation_id)
        if not self.curate_on_session_close:
            return {"processed": 0, "added": 0, "updated": 0, "skipped": 1, "summaries": 0, "personality_updates": 0, "archived": 0}
        if not messages:
            return {"processed": 0, "added": 0, "updated": 0, "skipped": 1, "summaries": 0, "personality_updates": 0, "archived": 0}
        if self._is_session_curated(conversation_id):
            return {"processed": 0, "added": 0, "updated": 0, "skipped": 1, "summaries": 0, "personality_updates": 0, "archived": 0}

        decisions = self._heuristic_curator_decisions(messages)
        if allow_llm and self.llm_client is not None:
            decisions.extend(await self._llm_curator_decisions(messages))
        return self._apply_curator_decisions(conversation_id, decisions, messages)

    def _apply_curator_decisions(
        self,
        conversation_id: str,
        decisions: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> dict[str, int]:
        if not self.enabled or not self.auto_learn_enabled or self.write_policy in {"off", "manual"}:
            self._mark_session_curated(conversation_id, {"skipped": "memory_disabled_or_manual"})
            return {"processed": 0, "added": 0, "updated": 0, "skipped": 1, "summaries": 0, "personality_updates": 0, "archived": 0}

        result = {"processed": 0, "added": 0, "updated": 0, "skipped": 0, "summaries": 0, "personality_updates": 0, "archived": 0}
        before_ids = {record["id"] for record in self._all_records(include_archived=True)}
        summary_written = False
        summary_text = ""
        review_state = "reviewed" if self.write_policy == "auto_reviewed" else "new"

        for decision in decisions:
            action = str(decision.get("action") or "").strip().lower()
            content = str(decision.get("content") or "").strip()
            result["processed"] += 1
            if action == "remember":
                memory = self.remember(
                    content,
                    category=self._normalize_category(str(decision.get("category") or "fact")),
                    confidence=max(self.min_confidence, _coerce_float(decision.get("confidence"), 0.8)),
                    review_state=review_state,
                    importance=max(1, min(10, _coerce_int(decision.get("importance"), 5))),
                    kind=str(decision.get("kind") or "fact"),
                    source="memory_curator",
                    source_conversation_id=conversation_id,
                )
                if memory is None:
                    result["skipped"] += 1
                elif memory["id"] in before_ids:
                    result["updated"] += 1
                else:
                    result["added"] += 1
                    before_ids.add(memory["id"])
            elif action == "personality_update":
                if self._update_personality(str(decision.get("target") or "user"), content, conversation_id):
                    result["personality_updates"] += 1
                else:
                    result["skipped"] += 1
            elif action == "session_summary":
                if self._write_session_summary(conversation_id, content, source="memory_curator"):
                    summary_written = True
                    summary_text = content
                    result["summaries"] += 1
            elif action == "archive":
                target_id = str(decision.get("memory_id") or "")
                if target_id and self.archive(target_id):
                    result["archived"] += 1
                else:
                    result["skipped"] += 1
            else:
                result["skipped"] += 1
                self._audit(
                    action="SKIP",
                    reason=str(decision.get("reason") or "curator_skip"),
                    source_conversation_id=conversation_id,
                    candidate_content=content[:1000],
                )

        if not summary_written:
            summary = self._default_session_summary(conversation_id, messages)
            if self._write_session_summary(conversation_id, summary, source="memory_curator_default"):
                summary_text = summary
                result["summaries"] += 1

        if self._write_episode(conversation_id, messages, summary_text or self._default_session_summary(conversation_id, messages)):
            result["episodes"] = 1
        else:
            result["episodes"] = 0

        self._audit(
            action="CURATE",
            reason="session_close",
            source_conversation_id=conversation_id,
            candidate_content=json.dumps(result, ensure_ascii=False),
        )
        self._mark_session_curated(conversation_id, result)
        return result

    def _conversation_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        count = max(1, self._db.count_messages(conversation_id))
        hot = self._db.get_messages(conversation_id, limit=max(count, 500))
        archived: list[dict[str, Any]] = []
        if _table_exists(self._db, "messages_archive"):
            try:
                archived = [
                    dict(row)
                    for row in self._db.fetchall(
                        "SELECT * FROM messages_archive WHERE conversation_id = ? ORDER BY id ASC",
                        (conversation_id,),
                    )
                ]
            except sqlite3.Error:
                archived = []
        combined = {int(row["id"]): row for row in archived + hot if row.get("id") is not None}
        return [combined[key] for key in sorted(combined)]

    def _conversation_tool_calls(self, conversation_id: str) -> list[dict[str, Any]]:
        return self._db_rows(
            """
            SELECT id, message_id, tool_name, input, output, status, created_at
            FROM tool_calls
            WHERE conversation_id = ?
            ORDER BY id ASC
            """,
            (conversation_id,),
        )

    def _write_episode(self, conversation_id: str, messages: list[dict[str, Any]], summary: str) -> bool:
        if not messages:
            return False
        tool_calls = self._conversation_tool_calls(conversation_id)
        message_ids = [int(item["id"]) for item in messages if item.get("id") is not None]
        errors = [
            {
                "tool": str(call.get("tool_name") or ""),
                "status": str(call.get("status") or ""),
                "output": str(call.get("output") or "")[:500],
            }
            for call in tool_calls
            if str(call.get("status") or "").lower() not in {"", "complete", "success", "ok"}
        ][:10]
        artifacts = [
            str(call.get("output") or "")[:300]
            for call in tool_calls
            if any(marker in str(call.get("output") or "").lower() for marker in ["path", "file", "saved", "created", "updated"])
        ][:10]
        episode_id = f"episode-{_safe_id(conversation_id)}"
        now = _now()
        try:
            with self._lock:
                self._db.execute(
                    """
                    INSERT INTO memory_episodes (
                        id, conversation_id, channel, summary, artifacts_json, errors_json,
                        source_message_start_id, source_message_end_id, tool_call_ids_json,
                        created_at, updated_at
                    )
                    VALUES (?, ?, 'desktop', ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        summary = excluded.summary,
                        artifacts_json = excluded.artifacts_json,
                        errors_json = excluded.errors_json,
                        source_message_start_id = excluded.source_message_start_id,
                        source_message_end_id = excluded.source_message_end_id,
                        tool_call_ids_json = excluded.tool_call_ids_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        episode_id,
                        conversation_id,
                        summary,
                        json.dumps(artifacts, ensure_ascii=False),
                        json.dumps(errors, ensure_ascii=False),
                        min(message_ids) if message_ids else None,
                        max(message_ids) if message_ids else None,
                        json.dumps([int(call["id"]) for call in tool_calls if call.get("id") is not None], ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                self._db.commit()
            return True
        except sqlite3.Error as exc:
            logger.debug("Unable to write memory episode: %s", exc)
            return False

    def _heuristic_curator_decisions(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decisions: list[dict[str, Any]] = []
        for message in messages:
            if str(message.get("role")) != "user":
                continue
            text = " ".join(str(message.get("content") or "").split())
            if self._should_skip_learning(text):
                continue
            decisions.extend(self._extract_user_signal_decisions(text))
        return self._dedupe_decisions(decisions)

    def _extract_user_signal_decisions(self, text: str) -> list[dict[str, Any]]:
        decisions: list[dict[str, Any]] = []
        segments = [
            segment.strip()
            for segment in re.split(r"(?<=[.!?])\s+|\n+", str(text or ""))
            if segment.strip()
        ]

        def remember(
            content: str,
            *,
            category: str,
            importance: int,
            confidence: float = 0.82,
            kind: str = "fact",
        ) -> None:
            cleaned = self._clean_signal_fragment(content)
            if not cleaned:
                return
            decisions.append(
                {
                    "action": "remember",
                    "content": cleaned,
                    "category": category,
                    "confidence": confidence,
                    "importance": importance,
                    "kind": kind,
                }
            )

        def personality_update(content: str, *, importance: int = 8, confidence: float = 0.9) -> None:
            cleaned = self._clean_signal_fragment(content)
            if not cleaned:
                return
            decisions.append(
                {
                    "action": "personality_update",
                    "target": "user",
                    "content": cleaned,
                    "confidence": confidence,
                    "importance": importance,
                }
            )

        for segment in segments:
            lowered = segment.lower()
            if self._contains_sensitive(segment):
                continue

            if match := re.search(r"\b(?:my name is|call me)\s+(.+?)(?:[.!?]|$)", segment, re.I):
                name = self._clean_signal_fragment(match.group(1))
                if name:
                    personality_update(f"The user's preferred name is {name}.", importance=8, confidence=0.9)

            if match := re.search(r"\b(?:i work on|i'm working on|i am working on)\s+(.+?)(?:[.!?]|$)", segment, re.I):
                project = self._clean_signal_fragment(match.group(1))
                if project:
                    remember(f"The user is currently working on {project}.", category="project", importance=7, confidence=0.84)

            if match := re.search(r"\bi work (?:in|with)\s+(.+?)(?:[.!?]|$)", segment, re.I):
                scope = self._clean_signal_fragment(match.group(1))
                if scope:
                    remember(f"The user works with {scope}.", category="fact", importance=6, confidence=0.82)

            if match := re.search(r"\b(?:this|current|main)\s+(?:repo|project|app)\s+(?:is|=)\s+(.+?)(?:[.!?]|$)", segment, re.I):
                project = self._clean_signal_fragment(match.group(1))
                if project:
                    remember(f"The user's current project is {project}.", category="project", importance=7, confidence=0.86)

            if match := re.search(r"\bi prefer\s+(.+?)(?:[.!?]|$)", segment, re.I):
                preference = self._clean_signal_fragment(match.group(1))
                if preference:
                    remember(f"The user prefers {preference}.", category="preference", importance=7, confidence=0.84)

            if match := re.search(r"\bi like\s+(.+?)(?:[.!?]|$)", segment, re.I):
                preference = self._clean_signal_fragment(match.group(1))
                if preference:
                    remember(f"The user likes {preference}.", category="preference", importance=6, confidence=0.8)

            if match := re.search(r"\bplease\s+always\s+(.+?)(?:[.!?]|$)", segment, re.I):
                behavior = self._clean_signal_fragment(match.group(1))
                if behavior:
                    remember(f"The user wants the assistant to always {behavior}.", category="behavior", importance=8, confidence=0.9)

            if match := re.search(r"\bask before\s+(.+?)(?:[.!?]|$)", segment, re.I):
                behavior = self._clean_signal_fragment(match.group(1))
                if behavior:
                    remember(f"The user wants the assistant to ask before {behavior}.", category="behavior", importance=8, confidence=0.9)

            if match := re.search(r"\bwhen i ask(?:ed)?(?:\s+for)?\s+(.+?)(?:[.!?]|$)", segment, re.I):
                workflow = self._clean_signal_fragment(match.group(1))
                if workflow:
                    remember(f"When the user asks for {workflow}, the assistant should follow that workflow consistently.", category="workflow", importance=7, confidence=0.84)

            if (
                any(term in lowered for term in ("reply", "response", "responses", "update", "updates", "messages"))
                and any(term in lowered for term in ("short", "concise", "brief", "direct", "precise", "detailed", "verbose"))
            ):
                remember(f"The user prefers {self._clean_signal_fragment(segment)}.", category="preference", importance=7, confidence=0.84)

        return decisions

    def _clean_signal_fragment(self, value: str) -> str:
        cleaned = " ".join(str(value or "").split()).strip(" \t\r\n'\"")
        cleaned = re.sub(r"^[,;:]+", "", cleaned).strip()
        cleaned = cleaned.rstrip(" .!?")
        if not cleaned:
            return ""
        return cleaned[:240]

    async def _llm_curator_decisions(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        transcript = self._transcript_text(messages)
        if not transcript:
            return []
        prompt = (
            "Analyze this closed assistant session and decide what should be saved as durable memory. "
            "Prioritize durable user preferences, working style, project context, workflow rules, and stable facts. "
            "Do not turn the transcript into a generic recap. Use session_summary only for a short operational handoff, "
            "not as a full conversation summary. "
            "Return only JSON with a decisions array. Each item must have action, content, category, "
            "confidence, importance, and optional target, kind, reason, memory_id. Valid actions are "
            "remember, personality_update, session_summary, skip, archive. Do not save secrets, tokens, "
            "credentials, one-off requests, raw logs, or sensitive personal data.\n\n"
            f"Transcript:\n{transcript}"
        )
        try:
            raw = await self.llm_client.chat(
                [{"role": "user", "content": prompt}],
                system_prompt="You are the Monaw memory curator. Return strict JSON only.",
            )
        except Exception as exc:
            logger.debug("Memory curator LLM unavailable: %s", exc)
            return []
        try:
            payload = json.loads(str(raw))
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", str(raw), re.S)
            if match is None:
                return []
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                return []
        decisions = payload.get("decisions") if isinstance(payload, dict) else payload
        if not isinstance(decisions, list):
            return []
        cleaned: list[dict[str, Any]] = []
        for item in decisions:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "").lower()
            content = str(item.get("content") or "").strip()
            if action not in {"remember", "personality_update", "session_summary", "skip", "archive"}:
                continue
            if action != "archive" and not content:
                continue
            if action in {"remember", "personality_update", "session_summary"} and not self._is_storable(content, confidence=_coerce_float(item.get("confidence"), 0.8)):
                cleaned.append({"action": "skip", "content": content, "reason": "safety_or_quality_filter"})
                continue
            cleaned.append(item)
        return self._dedupe_decisions(cleaned)

    def _dedupe_decisions(self, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        result: list[dict[str, Any]] = []
        for decision in decisions:
            key = (str(decision.get("action") or ""), _content_hash(str(decision.get("content") or "")))
            if key in seen:
                continue
            seen.add(key)
            result.append(decision)
        return result

    def _transcript_text(self, messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for message in messages:
            role = str(message.get("role") or "message")
            content = " ".join(str(message.get("content") or "").split())
            if content:
                lines.append(f"{role}: {content}")
        text = "\n".join(lines)
        return text[-MAX_TRANSCRIPT_CHARS:]

    def _default_session_summary(self, conversation_id: str, messages: list[dict[str, Any]]) -> str:
        last_user = next((str(item.get("content") or "") for item in reversed(messages) if item.get("role") == "user"), "")
        last_assistant = next((str(item.get("content") or "") for item in reversed(messages) if item.get("role") == "assistant"), "")
        conversation = self._db.get_conversation(conversation_id) or {}
        tool_calls = self._conversation_tool_calls(conversation_id)
        failed_tools = [
            call
            for call in tool_calls
            if str(call.get("status") or "").lower() not in {"", "complete", "success", "ok"}
        ]
        parts = [
            f"Session {conversation_id} had {len(messages)} messages.",
            f"Goal: {str(conversation.get('task_goal') or conversation.get('title') or '').strip() or 'Unknown.'}",
        ]
        if tool_calls:
            tool_names = []
            for call in tool_calls:
                name = str(call.get("tool_name") or "").strip()
                if name and name not in tool_names:
                    tool_names.append(name)
            parts.append(f"Tools used: {', '.join(tool_names[:12])}.")
        if failed_tools:
            parts.append(
                "Errors: "
                + "; ".join(
                    f"{call.get('tool_name')}: {str(call.get('output') or call.get('status') or '')[:160]}"
                    for call in failed_tools[:3]
                )
            )
        if last_user:
            parts.append(f"Last user request: {' '.join(last_user.split())[:500]}")
        if last_assistant:
            parts.append(f"Last assistant outcome: {' '.join(last_assistant.split())[:700]}")
        return "\n".join(parts)

    def _write_session_summary(self, conversation_id: str, content: str, *, source: str) -> dict[str, Any] | None:
        text = str(content or "").strip()
        if not text:
            return None
        now = _now()
        record = {
            "id": f"session-{_safe_id(conversation_id)}",
            "content": text,
            "category": "reflection",
            "status": "active",
            "review_state": "new",
            "confidence": 0.8,
            "importance": 5,
            "kind": "reflection",
            "source": source,
            "source_conversation_id": conversation_id,
            "source_message_id": None,
            "created_at": now,
            "updated_at": now,
            "last_used_at": "",
            "use_count": 0,
            "collection": "short-term",
        }
        return self._write_record(record)

    def _update_personality(self, target: str, content: str, conversation_id: str) -> bool:
        text = str(content or "").strip()
        if not text or self._contains_sensitive(text):
            return False
        target_id = "monaw" if str(target).lower() == "monaw" else "user"
        path = self.root / "personalities" / f"{target_id}.md"
        meta, body = _read_markdown(path) if path.exists() else ({}, "")
        if text.lower() in body.lower():
            return True
        now = _now()
        meta.update(
            {
                "id": target_id,
                "type": "personality",
                "status": "active",
                "updated_at": now,
                "created_at": meta.get("created_at") or now,
            }
        )
        body = f"{body.strip()}\n\n- {text}".strip()
        _write_markdown(path, meta, body)
        self._audit(action="PERSONALITY_UPDATE", reason=target_id, source_conversation_id=conversation_id, candidate_content=text[:1000])
        return True

    def _is_session_curated(self, conversation_id: str) -> bool:
        return (self.root / ".system" / "curated" / f"{_safe_id(conversation_id)}.json").exists()

    def _mark_session_curated(self, conversation_id: str, payload: dict[str, Any]) -> None:
        path = self.root / ".system" / "curated" / f"{_safe_id(conversation_id)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"conversation_id": conversation_id, "curated_at": _now(), "result": payload}, indent=2),
            encoding="utf-8",
        )

    def catch_up_unprocessed_sessions(self, *, max_sessions: int = 5) -> int:
        if not self.enabled or not self.auto_learn_enabled or not self.curate_on_session_close:
            return 0
        curated = 0
        for conversation in self._db.list_conversations()[: max(0, int(max_sessions))]:
            conversation_id = str(conversation.get("id") or "")
            if not conversation_id or self._is_session_curated(conversation_id):
                continue
            row = self._db.get_conversation(conversation_id) or {}
            updated_at = self._parse_dt(row.get("updated_at"))
            if updated_at and datetime.now(timezone.utc) - updated_at < timedelta(minutes=30):
                continue
            if self._db.count_messages(conversation_id) == 0:
                continue
            self._curate_session_sync(conversation_id)
            curated += 1
        return curated

    def maintain_memory_health(
        self,
        *,
        cooldown_hours: int | None = None,
        stale_days: int = 60,
        merge_threshold: float = 0.86,
        scan_limit: int = 200,
    ) -> dict[str, int]:
        cooldown = self.maintenance_cooldown_hours if cooldown_hours is None else int(cooldown_hours)
        marker = self.root / ".system" / "maintenance.json"
        # Retention is independent of the more expensive consolidation cooldown so
        # session-close maintenance still prunes ephemeral and audit data every time.
        self.enforce_retention()
        if cooldown > 0 and marker.exists():
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
                last = datetime.fromisoformat(str(payload.get("last_run_at", "")).replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - last < timedelta(hours=cooldown):
                    return {"archived_stale": 0, "merged": 0, "promoted": 0, "demoted": 0, "skipped": 1}
            except Exception:
                pass

        result = {"archived_stale": 0, "merged": 0, "promoted": 0, "demoted": 0, "skipped": 0}
        cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
        records = self._all_records(include_archived=False, include_short_term=False)[:scan_limit]
        for record in records:
            updated = self._parse_dt(record.get("last_used_at") or record.get("updated_at") or record.get("created_at"))
            if updated and updated < cutoff and int(record["importance"]) <= 3:
                if self.archive(record["id"]):
                    result["archived_stale"] += 1
                    record["status"] = "archived"
                    continue
            if int(record.get("use_count") or 0) >= 10 and int(record["importance"]) < 10:
                self.update(record["id"], importance=int(record["importance"]) + 1)
                result["promoted"] += 1
            created = self._parse_dt(record.get("created_at"))
            if created and created < cutoff and int(record.get("use_count") or 0) == 0 and int(record["importance"]) > 1:
                self.update(record["id"], importance=int(record["importance"]) - 1)
                result["demoted"] += 1

        active = [record for record in self._all_records(include_archived=False, include_short_term=False) if record["status"] == "active"]
        for index, left in enumerate(active[:scan_limit]):
            for right in active[index + 1 : scan_limit]:
                if left["category"] != right["category"]:
                    continue
                if self._lexical_similarity(left["content"], right["content"]) >= merge_threshold:
                    loser = right if int(left["importance"]) >= int(right["importance"]) else left
                    if self.archive(loser["id"]):
                        result["merged"] += 1
                    break

        marker.write_text(json.dumps({"last_run_at": _now(), "result": result}, indent=2), encoding="utf-8")
        return result

    def enforce_retention(self, *, max_age_days: int = MEMORY_RETENTION_DAYS) -> dict[str, int]:
        """Prune ephemeral memory artifacts and operational rows by age."""
        age_days = max(1, int(max_age_days))
        cutoff_epoch = time.time() - age_days * 24 * 60 * 60
        cutoff_iso = datetime.fromtimestamp(cutoff_epoch, timezone.utc).isoformat()
        result = {
            "short_term_deleted": 0,
            "curated_sessions_deleted": 0,
            "memory_candidates_deleted": 0,
            "memory_episodes_deleted": 0,
            "memory_checkpoints_deleted": 0,
            "memory_audit_deleted": 0,
        }

        for key, root in (
            ("short_term_deleted", self.root / "short-term"),
            ("curated_sessions_deleted", self.root / ".system" / "curated"),
        ):
            if not root.exists() or root.is_symlink():
                continue
            for path in root.rglob("*"):
                try:
                    if not path.is_file() or path.is_symlink() or path.stat().st_mtime >= cutoff_epoch:
                        continue
                    path.unlink()
                    result[key] += 1
                except OSError:
                    continue

        table_filters = (
            ("memory_candidates", "updated_at", "memory_candidates_deleted"),
            ("memory_episodes", "updated_at", "memory_episodes_deleted"),
            ("memory_checkpoints", "updated_at", "memory_checkpoints_deleted"),
            ("memory_audit", "created_at", "memory_audit_deleted"),
        )
        with self._lock:
            for table, column, result_key in table_filters:
                try:
                    cursor = self._db.execute(
                        f"DELETE FROM {table} WHERE {column} < ?",
                        (cutoff_iso,),
                    )
                    result[result_key] += max(0, int(cursor.rowcount))
                except sqlite3.Error:
                    continue
            try:
                self._db.commit()
            except sqlite3.Error:
                pass
        return result

    def clear_database_records(self) -> dict[str, int]:
        """Remove SQLite-backed memory state as part of a complete data delete."""
        deleted: dict[str, int] = {}
        with self._lock:
            for table in (
                "memory_audit",
                "memory_audit_counters",
                "memory_candidates",
                "memory_episodes",
                "memory_checkpoints",
                "memory_profile_fields",
            ):
                try:
                    cursor = self._db.execute(f"DELETE FROM {table}")
                    deleted[f"{table}_deleted"] = max(0, int(cursor.rowcount))
                except sqlite3.Error:
                    continue
            try:
                self._db.commit()
            except sqlite3.Error:
                pass
        return deleted

    def _parse_dt(self, value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None

    def _lexical_similarity(self, left: str, right: str) -> float:
        left_tokens = set(_tokens(left))
        right_tokens = set(_tokens(right))
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))

    def audit_log(
        self,
        *,
        conversation_id: str = "",
        memory_id: str | int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        wanted_memory = _safe_id(str(memory_id)) if memory_id is not None else ""
        where: list[str] = []
        params: list[Any] = []
        if conversation_id:
            where.append("source_conversation_id = ?")
            params.append(conversation_id)
        if wanted_memory:
            where.append("memory_id = ?")
            params.append(wanted_memory)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self._db_rows(
            f"""
            SELECT id, memory_id, action, reason, source_conversation_id,
                   candidate_content, created_at
            FROM memory_audit
            {clause}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            tuple(params + [max(1, min(500, int(limit)))]),
        )

    def _audit(
        self,
        *,
        action: str,
        reason: str = "",
        memory_id: str | int | None = None,
        source_conversation_id: str = "",
        candidate_content: str = "",
    ) -> str:
        now = _now()
        audit_id = f"audit-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}-{_slug(action, 'event')}"
        meta = {
            "id": audit_id,
            "action": str(action or "").upper(),
            "reason": reason,
            "memory_id": _safe_id(str(memory_id)) if memory_id is not None else "",
            "source_conversation_id": source_conversation_id,
            "created_at": now,
        }
        normalized_action = str(action or "").upper()
        try:
            with self._lock:
                if normalized_action in MAX_AUDIT_COUNTER_ACTIONS:
                    self._db.execute(
                        """
                        INSERT INTO memory_audit_counters (action, count)
                        VALUES (?, 1)
                        ON CONFLICT(action) DO UPDATE SET count = count + 1
                        """,
                        (normalized_action,),
                    )
                else:
                    self._db.execute(
                        """
                        INSERT INTO memory_audit (
                            id, action, reason, memory_id, source_conversation_id,
                            candidate_content, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            audit_id,
                            normalized_action,
                            reason,
                            _safe_id(str(memory_id)) if memory_id is not None else None,
                            source_conversation_id,
                            str(candidate_content or ""),
                            now,
                        ),
                    )
                self._db.commit()
        except sqlite3.Error:
            logger.debug("Unable to write memory audit event %s", normalized_action)
        return audit_id

    def _should_skip_learning(self, text: str) -> bool:
        value = " ".join(str(text or "").split())
        if not value:
            return True
        lowered = value.lower()
        if lowered in {"thanks", "thank you", "ok", "okay", "yes", "no", "continue", "proceed"}:
            return True
        high_signal_patterns = [
            r"\bi prefer\b",
            r"\bplease always\b",
            r"\bmy name is\b",
            r"\bcall me\b",
            r"\bi work\b",
            r"\bworking on\b",
            r"\bremember\b",
            r"\bask before\b",
            r"\bcurrent (?:repo|project|app)\b",
        ]
        if any(re.search(pattern, lowered) for pattern in high_signal_patterns):
            return False
        if len(value) < 20:
            return True
        if self._contains_sensitive(value):
            return True
        if value.startswith(("```", "{", "[")):
            return True
        return False

    def _is_storable(self, content: str, *, confidence: float) -> bool:
        text = " ".join(str(content or "").split())
        if len(text) < 8:
            return False
        if confidence < self.min_confidence:
            return False
        if self._contains_sensitive(text):
            return False
        return True

    def _contains_sensitive(self, content: str) -> bool:
        return any(pattern.search(content or "") for pattern in _SENSITIVE_PATTERNS)

_default_long_term_memory: LongTermMemory | None = None


def get_long_term_memory(llm_client=None, settings=None) -> LongTermMemory:
    global _default_long_term_memory
    if _default_long_term_memory is None:
        _default_long_term_memory = LongTermMemory(llm_client=llm_client, settings=settings)
    else:
        if llm_client is not None:
            _default_long_term_memory.llm_client = llm_client
        if settings is not None:
            _default_long_term_memory.settings = settings
    return _default_long_term_memory


def reset_long_term_memory() -> None:
    global _default_long_term_memory
    _default_long_term_memory = None
