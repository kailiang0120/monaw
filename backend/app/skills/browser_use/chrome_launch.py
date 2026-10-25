"""Find and open the user's own Chrome for the Monaw extension.

Chrome is opened normally, with no debugging flags: the Monaw extension
connects to Monaw by itself once Chrome is running. Monaw never owns or
closes the user's Chrome.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

_CHROME_PROCESS_NAMES = {"chrome.exe", "google chrome", "google-chrome", "chromium", "chromium.exe"}
_WINDOWS_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"


def _windows_registry_chrome_paths() -> list[Path]:
    try:
        import winreg
    except ImportError:
        return []
    paths: list[Path] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, _WINDOWS_APP_PATHS_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        cleaned = str(value or "").strip().strip('"')
        if cleaned:
            paths.append(Path(os.path.expandvars(cleaned)))
    return paths


def _chrome_executable_candidates() -> Iterator[Path]:
    if os.name == "nt":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA", "PROGRAMW6432"):
            base = os.environ.get(variable, "").strip()
            if base:
                yield Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
        # Only consulted when no standard install location exists.
        yield from _windows_registry_chrome_paths()
    elif sys.platform == "darwin":
        app = Path("Google Chrome.app") / "Contents" / "MacOS" / "Google Chrome"
        yield Path("/Applications") / app
        yield Path.home() / "Applications" / app


def find_chrome_executable() -> str:
    """Return the installed Google Chrome executable, or '' when none is found.

    Windows: standard Program Files / LocalAppData installs, then the App Paths
    registry key. macOS: /Applications. Elsewhere: Chrome on PATH.
    """
    for candidate in _chrome_executable_candidates():
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    names = ("chrome",) if os.name == "nt" else ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def system_chrome_user_data_dir() -> str:
    """Locate the user's real Chrome user-data-dir ('' when none exists)."""
    candidates: list[Path] = []
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        candidates.append(Path(local_appdata) / "Google" / "Chrome" / "User Data")
    home = Path.home()
    candidates.append(home / "AppData" / "Local" / "Google" / "Chrome" / "User Data")
    candidates.append(home / "Library" / "Application Support" / "Google" / "Chrome")
    candidates.append(home / ".config" / "google-chrome")

    for candidate in candidates:
        try:
            if candidate.exists():
                return str(candidate)
        except OSError:
            continue
    return ""


def _extract_user_data_dir_from_cmdline(cmdline: list[str] | tuple[str, ...] | None) -> str:
    parts = [str(item) for item in (cmdline or []) if str(item).strip()]
    for index, part in enumerate(parts):
        if part.startswith("--user-data-dir="):
            return part.split("=", 1)[1].strip().strip('"')
        if part == "--user-data-dir" and index + 1 < len(parts):
            return parts[index + 1].strip().strip('"')
    return ""


def chrome_running(user_data_dir: str) -> bool:
    """Whether the user's Chrome (the default user-data folder) is running."""
    import psutil

    for process in psutil.process_iter(attrs=["name", "cmdline"]):
        try:
            if str(process.info.get("name") or "").lower() not in _CHROME_PROCESS_NAMES:
                continue
            command = process.info.get("cmdline") or []
            if any(str(arg).startswith("--type=") for arg in command):
                continue
            root = _extract_user_data_dir_from_cmdline(command)
            if not root or Path(root).resolve() == Path(user_data_dir).resolve():
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
    return False


def launch_chrome_detached(args: list[str]) -> None:
    """Start Chrome outside Monaw's process tree so it outlives Monaw."""
    if os.name == "nt":
        # Electron terminates the backend's process tree on exit. A short-lived
        # launcher keeps the user's Chrome outside that tree once it exits.
        bootstrap = (
            "import subprocess,sys; "
            "subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, "
            "creationflags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP)"
        )
        subprocess.run(
            [sys.executable, "-c", bootstrap, *args], check=True, timeout=10,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
