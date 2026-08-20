"""Storage repository for sectioned long-term memory files."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.agent.memory_documents import (
    SECTIONED_SCHEMA,
    SECTION_HEADING_RE,
    SECTION_META_RE,
    Section,
    dump_markdown,
    safe_id,
    slug,
    split_markdown,
)

logger = logging.getLogger(__name__)


class MemorySectionRepository:
    """File-backed repository for sectioned memory documents."""

    def __init__(
        self,
        root: Path,
        valid_categories: tuple[str, ...],
        *,
        now: Callable[[], str],
    ) -> None:
        self.root = root
        self.valid_categories = valid_categories
        self.now = now

    def normalize_category(self, category: str) -> str:
        value = str(category or "fact").strip().lower()
        if value == "style":
            return "preference"
        return value if value in self.valid_categories else "fact"

    def category_file(self, category: str) -> Path:
        return self.root / "long-term" / f"{self.normalize_category(category)}.md"

    def archive_category_file(self, category: str) -> Path:
        return self.root / "archive" / f"{self.normalize_category(category)}.md"

    def section_default_meta(self, section_id: str, category: str) -> dict[str, Any]:
        now = self.now()
        return {
            "id": safe_id(section_id),
            "importance": 5,
            "confidence": 1.0,
            "created_at": now,
            "updated_at": now,
            "review_state": "new",
            "kind": "reflection" if self.normalize_category(category) == "reflection" else "fact",
            "status": "active",
            "source": "",
            "source_conversation_id": "",
            "source_message_id": None,
            "last_used_at": "",
            "use_count": 0,
            "sources": [],
        }

    def parse_sectioned(self, text: str, *, category: str = "fact") -> list[Section]:
        _meta, body = split_markdown(text or "")
        lines = body.splitlines()
        sections: list[Section] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            if not line.strip():
                index += 1
                continue
            match = SECTION_HEADING_RE.match(line)
            if match is None:
                raise ValueError("Sectioned memory files may only contain level-2 sections after frontmatter")
            title = match.group(1).strip()
            section_id = safe_id(match.group(2) or f"sec_{slug(title, 'memory')}")
            index += 1
            while index < len(lines) and not lines[index].strip():
                index += 1
            meta = self.section_default_meta(section_id, category)
            if index < len(lines):
                meta_match = SECTION_META_RE.match(lines[index].strip())
                if meta_match is not None:
                    try:
                        loaded = json.loads(meta_match.group(1))
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed metadata for memory section %s", section_id)
                    else:
                        if isinstance(loaded, dict):
                            meta.update(loaded)
                        else:
                            logger.warning("Skipping non-object metadata for memory section %s", section_id)
                    index += 1
            meta["id"] = safe_id(str(meta.get("id") or section_id))
            section_id = meta["id"]
            body_lines: list[str] = []
            while index < len(lines) and SECTION_HEADING_RE.match(lines[index]) is None:
                body_lines.append(lines[index])
                index += 1
            section_body = "\n".join(body_lines).strip()
            if section_body:
                sections.append(Section(id=section_id, title=title, meta=meta, body=section_body))
        return sections

    def dump_sectioned(self, category: str, sections: list[Section]) -> str:
        parts: list[str] = []
        for section in sections:
            section_id = safe_id(section.id)
            title = str(section.title or section_id).strip() or section_id
            meta = dict(section.meta)
            meta["id"] = section_id
            parts.append(f"## {title} {{#{section_id}}}")
            parts.append(f"<!-- meta: {json.dumps(meta, ensure_ascii=False, sort_keys=True)} -->")
            parts.append("")
            parts.append(str(section.body or "").strip())
            parts.append("")
        return dump_markdown(
            {
                "category": self.normalize_category(category),
                "schema": SECTIONED_SCHEMA,
                "updated_at": self.now(),
            },
            "\n".join(parts).strip(),
        )

    def load_sections(self, category: str, *, archived: bool = False) -> list[Section]:
        path = self.archive_category_file(category) if archived else self.category_file(category)
        if not path.exists():
            return []
        try:
            return self.parse_sectioned(path.read_text(encoding="utf-8"), category=category)
        except Exception as exc:
            logger.warning("Unable to read sectioned memory file %s: %s", path, exc)
            return []

    def save_sections(self, category: str, sections: list[Section], *, archived: bool = False) -> None:
        path = self.archive_category_file(category) if archived else self.category_file(category)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(self.dump_sectioned(category, sections), encoding="utf-8")
        tmp.replace(path)

    def remove_section_by_id(
        self,
        memory_id: str,
        *,
        include_archived: bool = True,
    ) -> bool:
        wanted = safe_id(str(memory_id))
        removed = False
        for category in self.valid_categories:
            for archived in ([False, True] if include_archived else [False]):
                sections = self.load_sections(category, archived=archived)
                kept = [section for section in sections if section.id != wanted]
                if len(kept) != len(sections):
                    self.save_sections(category, kept, archived=archived)
                    removed = True
        return removed
