"""Redaction helpers for observability persistence and exports."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MAX_TEXT_CHARS = 200_000

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|authorization|cookie|session|bearer)",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE)
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")
GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
GOOGLE_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b")
WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?:[^\s\"'<>|]+[\\/]?)+")
USER_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home)/[^\s\"'<>|]+")
INLINE_SECRET_RE = re.compile(
    r"\b(api[_-]?key|token|secret|password|authorization|cookie|session(?:id)?|bearer)\b(\s*[:=]\s*)([^\s,;]+)",
    re.IGNORECASE,
)
VALUE_REDACTIONS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED SSN]"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[REDACTED CARD]"),
)


def redact_text(value: str) -> str:
    text = WINDOWS_ABSOLUTE_PATH_RE.sub("[REDACTED PATH]", str(value or ""))
    text = USER_POSIX_PATH_RE.sub("[REDACTED PATH]", text)
    text = BEARER_RE.sub("Bearer [REDACTED]", text)
    text = OPENAI_KEY_RE.sub("sk-[REDACTED]", text)
    text = GITHUB_TOKEN_RE.sub("[REDACTED GITHUB TOKEN]", text)
    text = GOOGLE_API_KEY_RE.sub("[REDACTED GOOGLE API KEY]", text)
    text = INLINE_SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    for pattern, replacement in VALUE_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def safe_preview(value: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = redact_text(str(value or ""))
    if len(text) > limit:
        return text[:limit] + "\n[truncated]"
    return text


def redact(value: Any) -> Any:
    if isinstance(value, Path):
        return redact_text(str(value))
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SECRET_KEY_RE.search(key_text):
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return safe_preview(value)
    return value


def json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return redact(model_dump(exclude_none=True))
    if hasattr(value, "__dict__"):
        return redact({
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        })
    return redact_text(str(value))


def json_dumps(value: Any) -> str:
    return json.dumps(redact(value), ensure_ascii=False, default=json_default)


def without_sensitive_run_content(row: dict) -> dict:
    payload = redact(dict(row))
    payload["user_message"] = ""
    payload["final_output"] = ""
    payload["metadata_json"] = ""
    return payload


def without_sensitive_event_content(event: dict) -> dict:
    payload = redact(dict(event))
    payload["input"] = None
    payload["output"] = None
    if "input_json" in payload:
        payload["input_json"] = ""
    if "output_json" in payload:
        payload["output_json"] = ""
    return payload
