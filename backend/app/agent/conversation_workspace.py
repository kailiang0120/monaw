"""Conversation workspaces without changing the process-wide working directory."""

import os
from contextvars import ContextVar, Token
from pathlib import Path

from app.agent.runtime_paths import WORKSPACE_DIR

_WORKSPACE: ContextVar[str] = ContextVar("conversation_workspace", default="")


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
