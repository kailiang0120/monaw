"""Locations and constants shared by the extension server and the backend."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.agent.runtime_paths import MONAW_HOME_DIR, RUNTIME_DIR

# Fixed by the public key in browser-extension/manifest.json.
EXTENSION_ID = "nklbigohmenhbcfdincebbokjpeaaihc"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
PROTOCOL_VERSION = 1
DEFAULT_PORT = 8436
# Set by builtin.py for the server process.
PORT_ENV = "MONAW_BROWSER_EXTENSION_PORT"
SOURCE_ENV = "MONAW_BROWSER_EXTENSION_SOURCE"
PAIRING_FILE = "pairing.json"

_BACKEND_DIR = Path(__file__).resolve().parents[4]


def bundled_extension_dir() -> Path:
    """The extension shipped with Monaw: a sibling of the backend folder."""
    explicit = os.environ.get(SOURCE_ENV, "").strip()
    return Path(explicit) if explicit else _BACKEND_DIR.parent / "browser-extension"


def installed_extension_dir() -> Path:
    """The copy the user loads into Chrome. It stays put across Monaw updates."""
    return MONAW_HOME_DIR / "browser" / "extension"


def status_file() -> Path:
    return RUNTIME_DIR / "browser-extension-status.json"


def manifest_version(folder: Path) -> str:
    try:
        data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return ""
    return str(data.get("version") or "") if isinstance(data, dict) else ""


def read_pairing(folder: Path | None = None) -> dict[str, Any] | None:
    path = (folder or installed_extension_dir()) / PAIRING_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    if not isinstance(token, str) or len(token) < 32:
        return None
    return data


def configured_port() -> int:
    try:
        port = int(os.environ.get(PORT_ENV, "") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT
    return port if 1024 <= port <= 65535 else DEFAULT_PORT


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
