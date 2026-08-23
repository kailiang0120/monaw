"""Probe and persist capabilities tied to immutable Docker images."""

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path

from app.agent.runtime_paths import runtime_path


_INVENTORY_VERSION = 1
_INVENTORY_PATH = runtime_path("sandbox", "image-inventories.json")
_INVENTORY_LOCK = threading.RLock()
_PINNED_IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-fA-F]{64}$")
_PYTHON_IMPORT_PROBE = r"""
import importlib
import json
import subprocess
import sys

available = []
for name in sorted(module for module in sys.stdlib_module_names if not module.startswith("_")):
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import importlib; importlib.import_module(" + repr(name) + ")",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        continue
    if completed.returncode == 0:
        available.append(name)
print(json.dumps(available, separators=(",", ":")))
""".strip()


class ImageInventoryProbeError(RuntimeError):
    """Raised when an image cannot provide a trustworthy module inventory."""


def _is_pinned_image(image: str) -> bool:
    return bool(_PINNED_IMAGE_RE.fullmatch(str(image or "").strip()))


def _read_inventory(path: Path) -> dict[str, list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != _INVENTORY_VERSION:
        return {}
    images = payload.get("python_stdlib_modules")
    if not isinstance(images, dict):
        return {}
    inventories: dict[str, list[str]] = {}
    for image, names in images.items():
        if not isinstance(image, str) or not _is_pinned_image(image) or not isinstance(names, list):
            continue
        normalized_names = sorted(
            {
                name
                for name in names
                if isinstance(name, str) and name and not name.startswith("_")
            }
        )
        if {"builtins", "os", "sys"}.issubset(normalized_names):
            inventories[image] = normalized_names
    return inventories


def load_python_module_inventory(
    image: str,
    *,
    path: Path | None = None,
) -> frozenset[str] | None:
    """Load a previously probed inventory only for the exact pinned image."""
    normalized = str(image or "").strip()
    if not _is_pinned_image(normalized):
        return None
    inventory_path = path or _INVENTORY_PATH
    with _INVENTORY_LOCK:
        names = _read_inventory(inventory_path).get(normalized)
    return frozenset(names) if names else None


def store_python_module_inventory(
    image: str,
    modules: frozenset[str] | set[str] | list[str],
    *,
    path: Path | None = None,
) -> None:
    """Atomically cache an inventory under its immutable image reference."""
    normalized = str(image or "").strip()
    if not _is_pinned_image(normalized):
        raise ValueError(
            "Docker image inventories require an immutable @sha256 reference."
        )
    names = sorted(
        {str(name) for name in modules if str(name) and not str(name).startswith("_")}
    )
    if not {"builtins", "os", "sys"}.issubset(names):
        raise ValueError("Python module inventory is incomplete.")

    inventory_path = path or _INVENTORY_PATH
    with _INVENTORY_LOCK:
        inventories = _read_inventory(inventory_path)
        inventories[normalized] = names
        payload = {
            "version": _INVENTORY_VERSION,
            "python_stdlib_modules": inventories,
        }
        inventory_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = inventory_path.with_suffix(inventory_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(inventory_path)


def probe_python_module_inventory(
    image: str, *, timeout: float = 180.0
) -> frozenset[str]:
    """Return modules that can actually be imported in a pinned image.

    Each candidate import runs in its own isolated interpreter so a broken or
    side-effecting module cannot corrupt the rest of the inventory.
    """
    normalized = str(image or "").strip()
    if not _is_pinned_image(normalized):
        raise ImageInventoryProbeError(
            "Docker image is not pinned with an @sha256 digest."
        )

    command = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "64",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--entrypoint",
        "python",
        normalized,
        "-I",
        "-c",
        _PYTHON_IMPORT_PROBE,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        raise ImageInventoryProbeError(
            "Docker is unavailable for the image inventory probe."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ImageInventoryProbeError(
            "Python module inventory probe timed out."
        ) from exc

    if completed.returncode != 0:
        detail = (
            (completed.stderr or completed.stdout or "probe failed")
            .strip()
            .splitlines()[-1]
        )
        raise ImageInventoryProbeError(
            f"Python module inventory probe failed: {detail[:300]}"
        )
    try:
        raw_modules = json.loads((completed.stdout or "").strip())
    except (ValueError, TypeError) as exc:
        raise ImageInventoryProbeError(
            "Python module inventory probe returned invalid JSON."
        ) from exc
    if not isinstance(raw_modules, list) or not all(
        isinstance(name, str) for name in raw_modules
    ):
        raise ImageInventoryProbeError(
            "Python module inventory probe returned an invalid module list."
        )

    modules = frozenset(
        name for name in raw_modules if name and not name.startswith("_")
    )
    if not {"builtins", "os", "sys"}.issubset(modules):
        raise ImageInventoryProbeError(
            "Python module inventory probe returned an incomplete result."
        )
    return modules
