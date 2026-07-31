"""Startup security checks for the local control plane."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from app.agent.runtime_paths import RUNTIME_DIR
from app.security.control_plane import CONTROL_SECRET_ENV


def validate_control_plane_secret() -> None:
    secret = os.environ.get(CONTROL_SECRET_ENV, "").strip()
    if len(secret) < 32:
        raise RuntimeError("control-plane secret is missing or too short")


def validate_runtime_directory(path: Path = RUNTIME_DIR) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError("runtime directory cannot be created") from exc
    if path.is_symlink():
        raise RuntimeError("runtime directory must not be a symlink")
    probe = path / f".startup-check-{secrets.token_hex(8)}.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
        if probe.read_text(encoding="utf-8") != "ok":
            raise RuntimeError("runtime directory write verification failed")
    except OSError as exc:
        raise RuntimeError("runtime directory is not writable") from exc
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass


def validate_startup_security() -> None:
    validate_control_plane_secret()
    validate_runtime_directory()
