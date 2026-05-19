"""Stable filesystem locations for runtime data."""

from __future__ import annotations

import os
from pathlib import Path


def _explicit_path(*env_names: str) -> Path | None:
    for env_name in env_names:
        value = os.getenv(env_name)
        if value:
            return Path(value).expanduser()
    return None


def _default_monaw_home_dir() -> Path:
    explicit = _explicit_path("MONAW_HOME", "AGENT_HOME")
    if explicit:
        return explicit

    runtime_dir = _explicit_path("AGENT_RUNTIME_DIR")
    if runtime_dir:
        return runtime_dir.parent if runtime_dir.name.lower() == "runtime" else runtime_dir

    return Path.home() / ".monaw"


def _default_runtime_dir() -> Path:
    explicit = _explicit_path("AGENT_RUNTIME_DIR")
    if explicit:
        return explicit

    return MONAW_HOME_DIR / "runtime"


def _default_workspace_dir() -> Path:
    explicit = _explicit_path("AGENT_WORKSPACE_DIR")
    if explicit:
        return explicit
    return MONAW_HOME_DIR / "workspace"


MONAW_HOME_DIR = _default_monaw_home_dir()
RUNTIME_DIR = _default_runtime_dir()
WORKSPACE_DIR = _default_workspace_dir()
MONAW_HOME_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)


def runtime_path(*parts: str) -> Path:
    path = RUNTIME_DIR.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
