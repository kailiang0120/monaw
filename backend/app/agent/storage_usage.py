"""How much disk Monaw uses, by category, and cleanup for the parts that are safe to clear."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.agent.runtime_paths import MONAW_HOME_DIR, RUNTIME_DIR, WORKSPACE_DIR

# The Electron window's own caches; the app clears these through its session.
APP_CACHE_DIRS = ("Cache", "Code Cache", "GPUCache", "DawnWebGPUCache", "DawnGraphiteCache")


class StorageCleanupError(RuntimeError):
    """Raised when a category cannot be cleaned right now."""


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    description: str
    paths: Callable[[], list[Path]]
    # "backend" cleans here, "app" asks the desktop app to, None has no cleanup.
    clean: str | None = None
    clean_label: str = ""
    warning: str = ""


def _size(path: Path) -> int:
    """Bytes under path, not following links."""
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _within_home(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    return any(resolved.is_relative_to(root.resolve(strict=False)) for root in (MONAW_HOME_DIR, RUNTIME_DIR))


def _remove(path: Path) -> None:
    """Delete a file or folder inside Monaw's folders; files in use are skipped."""
    if path.is_symlink() or not _within_home(path) or not path.exists():
        return
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass
        return
    shutil.rmtree(path, ignore_errors=True)


def _log_paths() -> list[Path]:
    return [
        *RUNTIME_DIR.glob("backend.log*"),
        *RUNTIME_DIR.glob("debug-*.log"),
        RUNTIME_DIR / "harness_logs",
        RUNTIME_DIR / "policy",
        RUNTIME_DIR / "mcp",
        RUNTIME_DIR / "mcp_bridge",
        MONAW_HOME_DIR / "browser" / "screenshots",
    ]


def _database_paths() -> list[Path]:
    from app.agent.database import get_db

    path = get_db()._path
    return [path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")]


def _clean_chats() -> None:
    from app.agent.database import get_db
    from app.agent.memory_manager import delete_conversation_file, list_conversation_metadata, peek_memory_manager
    from app.agent.runtime import active_runtime_run_count
    from app.agent.ui_events import publish_ui_event

    if active_runtime_run_count():
        raise StorageCleanupError("Wait for the current reply to finish before deleting chats.")
    manager = peek_memory_manager()
    for conversation in list_conversation_metadata():
        conversation_id = str(conversation.get("id", ""))
        found = manager.delete(conversation_id) if manager is not None else delete_conversation_file(conversation_id)
        if found:
            publish_ui_event("conversation.changed", {"conversation_id": conversation_id, "action": "deleted"})
    get_db().compact()


def _clean_activity() -> None:
    from app.agent.observability.recorder import get_observability_recorder
    from app.agent.ui_events import publish_ui_event

    recorder = get_observability_recorder()
    recorder.delete_all()
    recorder.compact()
    publish_ui_event("observability.changed", {"reason": "storage_cleaned"})


def _clean_logs() -> None:
    from app.agent.observability.recorder import BACKEND_LOG_PATH

    for path in _log_paths():
        if path == BACKEND_LOG_PATH:
            # The backend is writing to it, so empty it instead of deleting it.
            try:
                path.write_text("", encoding="utf-8")
            except OSError:
                pass
        else:
            _remove(path)


def _remove_contents(paths: Callable[[], list[Path]]) -> Callable[[], None]:
    def clean() -> None:
        for path in paths():
            if path.is_dir() and not path.is_symlink():
                for child in path.iterdir():
                    _remove(child)
            else:
                _remove(path)
    return clean


CATEGORIES: tuple[Category, ...] = (
    Category(
        "chats", "Chat history", "Conversations, messages and tool results.",
        lambda: _database_paths(), clean="backend", clean_label="Delete all chats",
        warning="Every conversation is deleted. This cannot be undone.",
    ),
    Category(
        "uploads", "Uploaded files", "Files attached to chats and sent from Telegram.",
        lambda: [RUNTIME_DIR / "uploads"], clean="backend", clean_label="Delete uploads",
        warning="Chats that used these files will no longer open them.",
    ),
    Category(
        "activity", "Activity log", "Traces, token usage and errors shown under Activity log.",
        lambda: [RUNTIME_DIR / "observability"], clean="backend", clean_label="Clear activity log",
    ),
    Category(
        "logs", "Logs and screenshots", "Backend logs, diagnostics and browser screenshots.",
        _log_paths, clean="backend", clean_label="Clear logs",
    ),
    Category(
        "outputs", "Command outputs", "Files saved from commands the agent ran.",
        lambda: [RUNTIME_DIR / "exec", RUNTIME_DIR / "sandbox"], clean="backend", clean_label="Clear outputs",
    ),
    Category(
        "app_cache", "App cache", "Cached pages and graphics for the Monaw window. It refills as you use the app.",
        lambda: [RUNTIME_DIR / "electron" / name for name in APP_CACHE_DIRS],
        clean="app", clean_label="Clear app cache",
    ),
    Category(
        "legacy_browser", "Old browser profile",
        "The Chrome profile earlier versions launched for the agent. Monaw now uses your own Chrome through its extension.",
        lambda: [MONAW_HOME_DIR / "browser" / "profiles"], clean="backend", clean_label="Delete profile",
        warning="Sites signed in there are signed out; your own Chrome is not affected.",
    ),
    Category(
        "legacy_speech", "Old voice model", "The offline Whisper model earlier versions downloaded. Voice input no longer uses it.",
        lambda: [MONAW_HOME_DIR / "models" / "faster-whisper"], clean="backend", clean_label="Delete model",
    ),
    Category(
        "memory", "Memory", "What the agent remembers about you. Manage it under Identity.",
        lambda: [MONAW_HOME_DIR / "memory", RUNTIME_DIR / "memory-index"],
    ),
    Category(
        "account", "ChatGPT sign-in", "The runtime for signing in with a ChatGPT account. Sign out to remove it.",
        lambda: [MONAW_HOME_DIR / "codex-account"],
    ),
    Category(
        "workspace", "Workspace", "Files in Monaw's default work folder.",
        lambda: [WORKSPACE_DIR] if _within_home(WORKSPACE_DIR) else [],
    ),
)

_CLEANERS: dict[str, Callable[[], None]] = {
    "chats": _clean_chats,
    "uploads": _remove_contents(lambda: [RUNTIME_DIR / "uploads"]),
    "activity": _clean_activity,
    "logs": _clean_logs,
    "outputs": _remove_contents(lambda: [RUNTIME_DIR / "exec", RUNTIME_DIR / "sandbox"]),
    # Leftovers from earlier versions go entirely, folder and all.
    "legacy_browser": lambda: _remove(MONAW_HOME_DIR / "browser" / "profiles"),
    "legacy_speech": lambda: _remove(MONAW_HOME_DIR / "models" / "faster-whisper"),
}


def _home_roots() -> list[Path]:
    roots = [MONAW_HOME_DIR]
    if not RUNTIME_DIR.resolve(strict=False).is_relative_to(MONAW_HOME_DIR.resolve(strict=False)):
        roots.append(RUNTIME_DIR)
    return roots


def storage_usage() -> dict:
    total = sum(_size(root) for root in _home_roots())
    categories = []
    for category in CATEGORIES:
        size = sum(_size(path) for path in category.paths())
        if category.id.startswith("legacy_") and not size:
            continue
        categories.append({
            "id": category.id,
            "label": category.label,
            "description": category.description,
            "bytes": size,
            "cleanable_bytes": size if category.clean else 0,
            "clean": category.clean,
            "clean_label": category.clean_label,
            "warning": category.warning,
        })
    other = max(0, total - sum(item["bytes"] for item in categories))
    categories.append({
        "id": "other", "label": "Settings and other", "description": "Settings, skills you created and smaller app files.",
        "bytes": other, "cleanable_bytes": 0, "clean": None, "clean_label": "", "warning": "",
    })
    return {"location": str(MONAW_HOME_DIR), "total_bytes": total, "categories": categories}


def clean_storage(category_id: str) -> dict:
    cleaner = _CLEANERS.get(category_id)
    if cleaner is None:
        raise KeyError(category_id)
    category = next(item for item in CATEGORIES if item.id == category_id)
    before = sum(_size(path) for path in category.paths())
    cleaner()
    usage = storage_usage()
    after = next((item["bytes"] for item in usage["categories"] if item["id"] == category_id), 0)
    return {"freed_bytes": max(0, before - after), "usage": usage}
