"""Markdown document primitives for long-term memory."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.agent.runtime_paths import MONAW_HOME_DIR

SCHEMA_VERSION = 1
SECTIONED_SCHEMA = "sectioned-v1"

TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_'-]*")
SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)(.*)\Z", re.S)
SECTION_HEADING_RE = re.compile(r"^##\s+(.+?)(?:\s+\{#([A-Za-z0-9_.-]+)\})?\s*$")
SECTION_META_RE = re.compile(r"^<!--\s*meta:\s*(\{.*\})\s*-->\s*$")


@dataclass
class Section:
    id: str
    title: str
    meta: dict[str, Any]
    body: str


@dataclass
class SearchDocument:
    record: dict[str, Any]
    haystack: str
    tokens: frozenset[str]
    category: str
    importance_score: float
    use_score: float
    recency_score: float


def default_memory_root() -> Path:
    explicit = os.getenv("AGENT_MEMORY_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return MONAW_HOME_DIR / "memory"


def tokens(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text or "")]


def slug(text: str, fallback: str = "memory") -> str:
    token_values = tokens(text)
    value = "-".join(token_values[:8]) or fallback
    value = SAFE_ID_RE.sub("-", value).strip("-_.").lower()
    return value[:64] or fallback


def safe_id(value: str) -> str:
    safe = SAFE_ID_RE.sub("-", str(value or "")).strip("-_.").lower()
    return safe[:160] or hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def content_hash(content: str) -> str:
    normalized = " ".join(str(content or "").split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def split_markdown(raw: str) -> tuple[dict[str, Any], str]:
    match = FRONT_MATTER_RE.match(raw or "")
    if match is None:
        return {}, (raw or "").strip()
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, match.group(2).strip()


def dump_markdown(meta: dict[str, Any], body: str) -> str:
    cleaned = {
        key: value
        for key, value in meta.items()
        if value is not None
    }
    front = yaml.safe_dump(cleaned, sort_keys=False, allow_unicode=False).strip()
    body_text = str(body or "").strip()
    return f"---\n{front}\n---\n\n{body_text}\n"


def read_markdown(path: Path) -> tuple[dict[str, Any], str]:
    return split_markdown(path.read_text(encoding="utf-8"))


def write_markdown(path: Path, meta: dict[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(dump_markdown(meta, body), encoding="utf-8")
    tmp.replace(path)
