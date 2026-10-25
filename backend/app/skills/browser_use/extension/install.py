"""Copy the bundled extension to a stable folder and pair it with Monaw.

Chrome 137+ ignores ``--load-extension``, so the user loads the copied folder
once with "Load unpacked". The pairing file in that folder tells the extension
which local port to use and the token it must present.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from . import common
from .common import PAIRING_FILE, PROTOCOL_VERSION, manifest_version, read_pairing, write_json_atomic


class ExtensionInstallError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _sync_tree(source: Path, target: Path) -> None:
    """Make ``target`` match ``source`` in place, keeping the pairing file.

    Chrome keeps the loaded folder's path, so the folder itself is never
    replaced or renamed.
    """
    target.mkdir(parents=True, exist_ok=True)
    wanted: set[Path] = set()
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        wanted.add(relative)
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
    for item in sorted(target.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        relative = item.relative_to(target)
        if relative == Path(PAIRING_FILE) or relative in wanted:
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)


def write_pairing(port: int, folder: Path | None = None) -> dict[str, Any]:
    target = folder or common.installed_extension_dir()
    existing = read_pairing(target)
    # Keep the token across updates so a loaded extension stays paired.
    token = existing["token"] if existing else secrets.token_urlsafe(32)
    pairing = {"protocol": PROTOCOL_VERSION, "port": int(port), "token": token}
    write_json_atomic(target / PAIRING_FILE, pairing)
    return pairing


def ensure_pairing_port(port: int) -> None:
    """Point an installed extension at ``port`` if the setting changed."""
    folder = common.installed_extension_dir()
    pairing = read_pairing(folder)
    if pairing and int(pairing.get("port") or 0) != int(port):
        write_pairing(port, folder)


def install_extension(port: int) -> dict[str, Any]:
    source = common.bundled_extension_dir()
    if not manifest_version(source):
        raise ExtensionInstallError(
            "The Monaw extension files are missing from this installation.",
            reason_code="extension_source_missing",
        )
    target = common.installed_extension_dir()
    try:
        _sync_tree(source, target)
        write_pairing(port, target)
    except OSError as exc:
        raise ExtensionInstallError(
            "Could not copy the Monaw extension into your Monaw folder.",
            reason_code="extension_install_failed",
        ) from exc
    return {"folder": str(target), "version": manifest_version(target)}


def _pid_alive(pid: Any) -> bool:
    try:
        import psutil

        return bool(pid) and psutil.pid_exists(int(pid))
    except (ImportError, ValueError, TypeError):
        return False


def read_connection_status() -> dict[str, Any]:
    """What the extension server last reported, if that server is still running."""
    try:
        data = json.loads(common.status_file().read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(data, dict) or not _pid_alive(data.get("pid")):
        return {}
    return data


def extension_status() -> dict[str, Any]:
    folder = common.installed_extension_dir()
    installed_version = manifest_version(folder)
    bundled_version = manifest_version(common.bundled_extension_dir())
    connection = read_connection_status()
    connected = bool(connection.get("connected"))
    return {
        "installed": bool(installed_version),
        "paired": read_pairing(folder) is not None,
        "installed_version": installed_version,
        "bundled_version": bundled_version,
        "update_available": bool(installed_version and bundled_version and installed_version != bundled_version),
        "server_running": bool(connection),
        "connected": connected,
        "connected_version": str(connection.get("version") or "") if connected else "",
        "last_error": str(connection.get("error") or ""),
    }


def write_connection_status(**fields: Any) -> None:
    payload = {"pid": os.getpid(), "updated_at": time.time(), **fields}
    try:
        write_json_atomic(common.status_file(), payload)
    except OSError:
        pass
