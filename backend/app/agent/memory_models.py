"""Durable memory in two scopes: global (about the user) and workspace (about one folder).

Each scope is one human-editable markdown file. Entries are bullets grouped under
``## Rules``, ``## Preferences`` and ``## Facts``; a trailing HTML comment carries
the entry id and bookkeeping, and a bullet written by hand without one is picked
up on the next read. Rules and preferences are always in the prompt; facts are
added only when they match the user's message.

Memory comes only from the user's own words: the ``memory_*`` tools refuse content
the user did not state in the current turn, and the background learner reads the
user's message, never assistant replies or tool output.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.agent.runtime_paths import MONAW_HOME_DIR, WORKSPACE_DIR

logger = logging.getLogger(__name__)

SCHEMA = "monaw-memory-v2"
SCOPES = ("global", "workspace")
KINDS = ("rule", "preference", "fact")
PINNED_KINDS = ("rule", "preference")
_KIND_HEADINGS = {"rule": "Rules", "preference": "Preferences", "fact": "Facts"}
_HEADING_KINDS = {title.lower(): kind for kind, title in _KIND_HEADINGS.items()}

MIN_CONTENT_CHARS = 8
MAX_CONTENT_CHARS = 500
DUPLICATE_SIMILARITY = 0.8
MIN_GROUNDED_TERM_SHARE = 0.5
# Share of the prompt budget rules and preferences may take before the rest of
# them compete with facts on relevance.
PINNED_BUDGET_SHARE = 0.6
# Coverage of a query is measured against at most this much term weight, so a
# long message is not penalised for words no memory could match.
RELEVANCE_QUERY_WEIGHT_CAP = 5.0
SHORT_QUERY_TERMS = 3
MAX_EXPANSION_MESSAGES = 2
MAX_EXPANSION_CHARS = 500
MAX_LEARNING_INPUT_CHARS = 4000
MAX_LEARNING_CONTEXT_ENTRIES = 80

LEGACY_LAYOUT = ("long-term", "short-term", "archive", "personalities", ".system", "audit")

_ENTRY_RE = re.compile(r"^[-*]\s+(?P<text>.*?)\s*(?:<!--\s*(?P<meta>.*?)\s*-->)?\s*$")
_META_PAIR_RE = re.compile(r"(\w+):(\S+)")
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)(.*)\Z", re.S)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_'-]*")

# Function words plus the boilerplate every memory carries ("The user prefersâ€¦"),
# which would otherwise make every memory look related to every query.
_STOPWORDS = frozenset(
    """
    a about above after again all also am an and any are as at be because been before being
    below between both but by can could did do does doing done down during each else ever few
    for from further get gets got had has have having he her here hers him his how i if in into
    is it its itself just let lets me more most much must my myself no nor not now of off ok okay
    on once only or other our ours out over own please same she should so some such than thank
    thanks that the their theirs them then there these they this those through to too under
    until up us very was way we were what when where which while who whom why will with would
    yes yet you your yours yourself
    user users assistant prefer prefers preferred like likes want wants wanted always never
    remember note
    """.split()
)

# Words a paraphrased memory adds on top of what the user actually said.
_GROUNDING_FILLER = frozenset(
    {"currently", "current", "work", "works", "working", "use", "uses", "using", "instead",
     "project", "repo", "repository", "workspace", "folder"}
)

_SECRET_PATTERNS = [
    re.compile(r"\b(?:api[_ -]?key|secret key|client secret|password|passwd|passphrase|bearer)\b\s*[:=]", re.I),
    re.compile(r"\b(?:password|passwd|passphrase)\s+(?:is|was)\b", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    re.compile(r"\b(?:cvv|bank account|medical record|diagnosis)\b", re.I),
]

# A turn only goes to the learner when the user says something that sounds durable.
_DURABLE_SIGNAL_RE = re.compile(
    r"\b(?:remember|don'?t forget|do not forget|keep in mind|note that|make a note|from now on|going forward"
    r"|in (?:the )?future|always|never|i prefer|i'?d prefer|prefer(?:ably)?|i like|i love|i hate|i dislike"
    r"|i don'?t like|i do not like|call me|my name is|i'?m an?\b|i am an?\b|i work|i use|we use|our team"
    r"|our convention|convention is|this (?:repo|project|workspace|folder) (?:uses|is)|stop (?:doing|using)"
    r"|don'?t (?:ever|use)|instead of)\b",
    re.I,
)

_PROMPT_PERSON_REPLACEMENTS = (
    (re.compile(r"^The user prefers\b", re.I), "You prefer"),
    (re.compile(r"^The user likes\b", re.I), "You like"),
    (re.compile(r"^The user wants\b", re.I), "You want"),
    (re.compile(r"^The user works\b", re.I), "You work"),
    (re.compile(r"^The user's\b", re.I), "Your"),
    (re.compile(r"^The user\b", re.I), "You"),
)
_PROMPT_OVERRIDE_RE = re.compile(
    r"(?i)\b(ignore|override|disregard)\b[^.?!]*(previous|system|developer|policy|instruction|permission)[^.?!]*[.?!]?"
)


class MemoryWriteError(ValueError):
    """A memory write was refused; the message is safe to show the agent and the user."""


@dataclass(frozen=True)
class MemoryEntry:
    id: str
    scope: str
    kind: str
    content: str
    importance: int = 5
    source: str = "user"
    created_at: str = ""
    updated_at: str = ""
    workspace: str = ""
    source_refs: tuple[str, ...] = ()
    revision: int = 1
    supersedes_revision: int | None = None

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _ScopeFile:
    scope: str
    path: Path
    workspace: str
    entries: list[MemoryEntry]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clamp_importance(value: Any) -> int:
    try:
        return max(1, min(10, int(value)))
    except (TypeError, ValueError):
        return 5


def _stem(term: str) -> str:
    if len(term) > 4 and term.endswith("ies"):
        return f"{term[:-3]}y"
    if len(term) > 3 and term.endswith("s") and not term.endswith(("ss", "us", "is")):
        return term[:-1]
    return term


def search_terms(text: str) -> list[str]:
    """Content-bearing, lightly stemmed terms (hyphenated words also yield their parts)."""
    terms: list[str] = []
    for token in _TOKEN_RE.findall(str(text or "").lower()):
        if token.endswith("'s"):
            token = token[:-2]
        parts = [token, *re.split(r"[-_']+", token)] if re.search(r"[-_']", token) else [token]
        for part in parts:
            part = part.strip("-_'")
            if len(part) < 2 or part in _STOPWORDS:
                continue
            terms.append(_stem(part))
    return terms


def _similarity(left: str, right: str) -> float:
    left_terms, right_terms = set(search_terms(left)), set(search_terms(right))
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def _clean_content(content: str) -> str:
    text = " ".join(str(content or "").split())
    return re.sub(r"^[-*]\s+", "", text).replace("<!--", "").replace("-->", "").strip()


def contains_secret(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _SECRET_PATTERNS)


def has_durable_signal(text: str) -> bool:
    return bool(_DURABLE_SIGNAL_RE.search(text or ""))


def grounded_in(content: str, user_text: str) -> bool:
    """True when ``content`` is mostly the user's own words from ``user_text``."""
    wanted = set(search_terms(content)) - _GROUNDING_FILLER
    if not wanted:
        return False
    said = set(search_terms(user_text))
    return len(wanted & said) / len(wanted) >= MIN_GROUNDED_TERM_SHARE


def normalize_workspace(path: str | os.PathLike[str] | None) -> str:
    """Resolve a workspace folder; an empty value means Monaw's default workspace."""
    raw = str(path or "").strip() or str(WORKSPACE_DIR)
    try:
        resolved = Path(os.path.expandvars(raw)).expanduser().resolve()
    except (OSError, RuntimeError):
        resolved = Path(raw)
    return str(resolved)


def _workspace_key(workspace: str) -> str:
    digest = hashlib.sha256(os.path.normcase(workspace).encode("utf-8")).hexdigest()[:10]
    name = re.sub(r"[^a-z0-9]+", "-", Path(workspace).name.lower()).strip("-")[:40] or "workspace"
    return f"{name}-{digest}"


def default_memory_root() -> Path:
    explicit = os.getenv("AGENT_MEMORY_DIR")
    return Path(explicit).expanduser() if explicit else MONAW_HOME_DIR / "memory"


def _safe_revision(value: Any) -> int:
    try:
        return max(1, min(1000000, int(value)))
    except (ValueError, TypeError):
        return 1


def _decode_refs(value: str) -> tuple[str, ...]:
    try:
        refs = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        return tuple(str(ref) for ref in refs) if isinstance(refs, list) else ()
    except (ValueError, TypeError, UnicodeDecodeError):
        return ()


def _parse_scope_file(raw: str, *, scope: str, workspace: str, fallback_time: str) -> list[MemoryEntry]:
    match = _FRONT_MATTER_RE.match(raw or "")
    body = match.group(2) if match else (raw or "")
    entries: list[MemoryEntry] = []
    seen: set[str] = set()
    kind = "fact"
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        content = _clean_content(current["text"])
        meta = current["meta"]
        if len(content) >= MIN_CONTENT_CHARS:
            entry_id = str(meta.get("id") or "")
            if not re.fullmatch(r"m-[0-9a-f]{6,16}", entry_id) or entry_id in seen:
                # Hand-written bullets get a stable id from their content.
                entry_id = f"m-{hashlib.sha256(f'{scope}:{workspace}:{content}'.encode()).hexdigest()[:8]}"
            if entry_id not in seen:
                seen.add(entry_id)
                entries.append(
                    MemoryEntry(
                        id=entry_id,
                        scope=scope,
                        kind=current["kind"],
                        content=content,
                        importance=_clamp_importance(meta.get("importance", 5)),
                        source=str(meta.get("source") or "user"),
                        created_at=str(meta.get("created") or fallback_time),
                        updated_at=str(meta.get("updated") or meta.get("created") or fallback_time),
                        workspace=workspace,
                        source_refs=(_decode_refs(str(meta.get("refs") or "")) if meta.get("hash") == hashlib.sha256(content.encode("utf-8")).hexdigest()[:16] else ()),
                        revision=_safe_revision(meta.get("rev")),
                        supersedes_revision=_safe_revision(meta.get("prev")) if meta.get("prev") else None,
                    )
                )
        current = None

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            heading = stripped.lstrip("#").strip().lower()
            if stripped.startswith("## "):
                kind = _HEADING_KINDS.get(heading, "fact")
            continue
        entry = _ENTRY_RE.match(stripped)
        if entry and not line.startswith(("  ", "\t")):
            flush()
            meta = {key: value for key, value in _META_PAIR_RE.findall(entry.group("meta") or "")}
            current = {
                "text": entry.group("text"),
                "kind": kind,
                "meta": {
                    "id": meta.get("id", ""),
                    "importance": meta.get("i", 5),
                    "source": meta.get("s", ""),
                    "created": meta.get("c", ""),
                    "updated": meta.get("u", ""),
                    "refs": meta.get("refs", ""),
                    "hash": meta.get("ch", ""),
                    "rev": meta.get("rev", ""),
                    "prev": meta.get("prev", ""),
                },
            }
        elif current is not None and stripped:
            current["text"] = f"{current['text']} {stripped}"
        elif not stripped:
            flush()
    flush()
    return entries


def _dump_scope_file(scope_file: _ScopeFile) -> str:
    front: dict[str, Any] = {"schema": SCHEMA, "scope": scope_file.scope}
    if scope_file.scope == "workspace":
        front["workspace"] = scope_file.workspace
    front["updated_at"] = _now()
    if scope_file.scope == "global":
        title = "# Global memory"
        intro = "Applies to every conversation. Edit freely: one bullet per memory, under Rules, Preferences, or Facts."
    else:
        title = f"# Workspace memory: {Path(scope_file.workspace).name or scope_file.workspace}"
        intro = "Applies only to conversations in this folder. Edit freely: one bullet per memory, under Rules, Preferences, or Facts."
    lines = ["---", yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip(), "---", "", title, "", intro]
    for kind in KINDS:
        lines.extend(["", f"## {_KIND_HEADINGS[kind]}", ""])
        for entry in scope_file.entries:
            if entry.kind != kind:
                continue
            meta = f"id:{entry.id} i:{entry.importance} s:{entry.source} c:{entry.created_at} u:{entry.updated_at}"
            if entry.revision > 1:
                meta += f" rev:{entry.revision} prev:{entry.supersedes_revision or entry.revision - 1}"
            if entry.source_refs:
                meta += " refs:" + base64.urlsafe_b64encode(json.dumps(entry.source_refs).encode()).decode().rstrip("=")
                meta += " ch:" + hashlib.sha256(entry.content.encode("utf-8")).hexdigest()[:16]
            lines.append(f"- {entry.content} <!-- {meta} -->")
    return "\n".join(lines).rstrip() + "\n"



