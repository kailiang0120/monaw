"""Conversation workspaces without changing the process-wide working directory."""

import os
import uuid
from contextvars import ContextVar, Token
from datetime import datetime
from pathlib import Path

from app.agent.runtime_paths import WORKSPACE_DIR

_WORKSPACE: ContextVar[str] = ContextVar("conversation_workspace", default="")
DEFAULT_CHAT_WORKSPACE_ROOT = WORKSPACE_DIR


def create_default_chat_workspace(conversation_id: str, created_at: str) -> str:
    """Use the chat's creation-date folder as its stable default working folder."""
    day = datetime.fromisoformat(created_at).astimezone().date().isoformat()
    path = DEFAULT_CHAT_WORKSPACE_ROOT / day
    path.mkdir(parents=True, exist_ok=True)
    return str(path.resolve())


def is_default_chat_workspace(value: str, conversation_id: str) -> bool:
    """Recognize default folders centrally, including this chat's legacy child."""
    if not value:
        return True
    try:
        relative = Path(value).resolve().relative_to(DEFAULT_CHAT_WORKSPACE_ROOT.resolve())
        if not relative.parts:
            return True
        day = relative.parts[0]
        if datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day:
            return False
        if len(relative.parts) == 1:
            return True
        legacy_name = "chat-" + uuid.uuid5(uuid.NAMESPACE_URL, conversation_id).hex
        return len(relative.parts) == 2 and relative.parts[1] == legacy_name
    except (OSError, ValueError, RuntimeError):
        return False


def selected_workspace_dir() -> str:
    return _WORKSPACE.get()


def current_workspace_dir() -> Path:
    return Path(selected_workspace_dir()) if selected_workspace_dir() else WORKSPACE_DIR


def set_workspace_dir(path: str) -> Token[str]:
    return _WORKSPACE.set(path or "")


def reset_workspace_dir(token: Token[str]) -> None:
    _WORKSPACE.reset(token)


def workspace_path(path: str) -> Path:
    target = Path(os.path.expandvars(path)).expanduser()
    return target if target.is_absolute() else current_workspace_dir() / target


def validate_workspace_dir(value: str) -> str:
    """Only persist existing absolute directories; empty means the default."""
    if not value.strip():
        return ""
    try:
        path = Path(os.path.expandvars(value.strip())).expanduser()
        if not path.is_absolute():
            raise ValueError("Choose an absolute folder path.")
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise ValueError("The working folder must be a directory.")
        return str(path)
    except (OSError, RuntimeError) as exc:
        raise ValueError("The working folder does not exist or cannot be accessed.") from exc
