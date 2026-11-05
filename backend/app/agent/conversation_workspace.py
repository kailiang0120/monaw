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
    """Give a chat a stable, date-grouped folder without using its editable title."""
    day = datetime.fromisoformat(created_at).astimezone().date().isoformat()
    # Conversation IDs may also come from integrations: never use raw path input.
    chat_id = uuid.uuid5(uuid.NAMESPACE_URL, conversation_id).hex
    path = DEFAULT_CHAT_WORKSPACE_ROOT / day / f"chat-{chat_id}"
    path.mkdir(parents=True, exist_ok=True)
    return str(path.resolve())


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
