from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from pathlib import Path

from app.agent.runtime_paths import runtime_path

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")
_DEFAULT_SESSION_PATH = runtime_path("telegram", "sessions.json")


def _compact_identifier(value: str, *, max_length: int) -> str:
    normalized = _SAFE_ID_RE.sub("_", value).strip("_")
    if not normalized:
        normalized = "chat"
    if len(normalized) <= max_length:
        return normalized
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    keep = max(1, max_length - len(digest) - 1)
    return f"{normalized[:keep]}_{digest}"


def telegram_chat_key(chat_id: int | str, thread_id: int | str | None = None) -> str:
    raw = str(chat_id)
    if thread_id not in (None, ""):
        raw = f"{raw}:{thread_id}"
    return raw


def default_conversation_id(chat_id: int | str, thread_id: int | str | None = None) -> str:
    compact = _compact_identifier(telegram_chat_key(chat_id, thread_id), max_length=55)
    return f"telegram_{compact}"[:64]


class TelegramSessionStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_SESSION_PATH
        self.model_path = self.path.with_name(f"{self.path.stem}.models{self.path.suffix}")
        self.effort_path = self.path.with_name(f"{self.path.stem}.efforts{self.path.suffix}")
        self._lock = threading.RLock()

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in data.items()
            if str(key).strip() and str(value).strip()
        }

    def _write(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def _read_models(self) -> dict[str, dict[str, str]]:
        if not self.model_path.exists():
            return {}
        try:
            data = json.loads(self.model_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}

        result: dict[str, dict[str, str]] = {}
        for key, value in data.items():
            if not isinstance(value, dict):
                continue
            conversation_id = str(key).strip()
            provider = str(value.get("provider") or "").strip()
            model_name = str(value.get("model_name") or "").strip()
            if conversation_id and provider and model_name:
                result[conversation_id] = {
                    "provider": provider,
                    "model_name": model_name,
                }
        return result

    def _write_models(self, data: dict[str, dict[str, str]]) -> None:
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        self.model_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def _read_efforts(self) -> dict[str, str]:
        if not self.effort_path.exists():
            return {}
        try:
            data = json.loads(self.effort_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): str(value).strip()
            for key, value in data.items()
            if str(key).strip() and str(value).strip()
        }

    def _write_efforts(self, data: dict[str, str]) -> None:
        self.effort_path.parent.mkdir(parents=True, exist_ok=True)
        self.effort_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def get_current(self, chat_id: int | str, thread_id: int | str | None = None) -> str:
        key = telegram_chat_key(chat_id, thread_id)
        fallback = default_conversation_id(chat_id, thread_id)
        with self._lock:
            data = self._read()
            conversation_id = data.get(key) or fallback
            if data.get(key) != conversation_id:
                data[key] = conversation_id
                self._write(data)
            return conversation_id

    def list_chat_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._read().keys())

    def list_chat_sessions(self) -> list[tuple[str, str]]:
        with self._lock:
            return sorted(self._read().items())

    def start_new(self, chat_id: int | str, thread_id: int | str | None = None) -> str:
        key = telegram_chat_key(chat_id, thread_id)
        base = default_conversation_id(chat_id, thread_id)
        suffix = uuid.uuid4().hex[:8]
        conversation_id = f"{base[:55]}_{suffix}"[:64]
        with self._lock:
            data = self._read()
            data[key] = conversation_id
            self._write(data)
        return conversation_id

    def set_current(
        self,
        chat_id: int | str,
        conversation_id: str,
        thread_id: int | str | None = None,
    ) -> str:
        normalized = str(conversation_id).strip()
        if not normalized:
            raise ValueError("conversation_id is required")
        key = telegram_chat_key(chat_id, thread_id)
        with self._lock:
            data = self._read()
            data[key] = normalized
            self._write(data)
        return normalized

    def get_model_selection(self, conversation_id: str) -> dict[str, str] | None:
        normalized = str(conversation_id).strip()
        if not normalized:
            return None
        with self._lock:
            return self._read_models().get(normalized)

    def set_model_selection(
        self,
        conversation_id: str,
        *,
        provider: str,
        model_name: str,
    ) -> dict[str, str]:
        normalized_id = str(conversation_id).strip()
        normalized_provider = str(provider).strip()
        normalized_model = str(model_name).strip()
        if not normalized_id:
            raise ValueError("conversation_id is required")
        if not normalized_provider:
            raise ValueError("provider is required")
        if not normalized_model:
            raise ValueError("model_name is required")
        selection = {
            "provider": normalized_provider,
            "model_name": normalized_model,
        }
        with self._lock:
            data = self._read_models()
            data[normalized_id] = selection
            self._write_models(data)
        return selection

    def clear_model_selection(self, conversation_id: str) -> bool:
        normalized = str(conversation_id).strip()
        if not normalized:
            return False
        with self._lock:
            data = self._read_models()
            existed = normalized in data
            if existed:
                data.pop(normalized, None)
                self._write_models(data)
            return existed

    def get_effort_selection(self, conversation_id: str) -> str:
        normalized = str(conversation_id).strip()
        if not normalized:
            return ""
        with self._lock:
            return self._read_efforts().get(normalized, "")

    def set_effort_selection(self, conversation_id: str, effort: str) -> str:
        normalized_id = str(conversation_id).strip()
        normalized_effort = str(effort).strip().lower()
        if not normalized_id:
            raise ValueError("conversation_id is required")
        if not normalized_effort:
            raise ValueError("effort is required")
        with self._lock:
            data = self._read_efforts()
            data[normalized_id] = normalized_effort
            self._write_efforts(data)
        return normalized_effort

    def clear_effort_selection(self, conversation_id: str) -> bool:
        normalized = str(conversation_id).strip()
        if not normalized:
            return False
        with self._lock:
            data = self._read_efforts()
            existed = normalized in data
            if existed:
                data.pop(normalized, None)
                self._write_efforts(data)
            return existed
