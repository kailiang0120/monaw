"""Prepare and launch the user's own Chrome for Chrome DevTools MCP.

Monaw drives the browser through ``chrome-devtools-mcp --autoConnect``, which
attaches to Chrome's user-approved remote-debugging endpoint (Chrome 144+).
This module only gets that endpoint ready: it finds Chrome, enables remote
debugging in the profile's ``Local State`` while Chrome is closed, launches the
selected profile detached from Monaw's process tree, and waits for
``DevToolsActivePort``. It never owns or closes the user's Chrome.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_CHROME_PROCESS_NAMES = {"chrome.exe", "google chrome", "google-chrome", "chromium", "chromium.exe"}
_WINDOWS_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"


class BrowserLaunchError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


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


def _chrome_profile_display_name(profile_dir: Path) -> str:
    try:
        data = json.loads((profile_dir / "Preferences").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return profile_dir.name
    profile = data.get("profile") if isinstance(data, dict) else None
    if not isinstance(profile, dict):
        return profile_dir.name
    return str(profile.get("name") or "").strip() or profile_dir.name


def _is_chrome_profile_dir(path: Path) -> bool:
    name = path.name
    if name in {"Default", "Guest Profile"}:
        return True
    return name.startswith("Profile ") and name.removeprefix("Profile ").isdigit()


def list_local_chrome_profiles() -> list[dict[str, str]]:
    """List the Chrome profiles in the user's data folder as {name, directory}."""
    user_data_dir = system_chrome_user_data_dir()
    if not user_data_dir:
        return []
    root = Path(user_data_dir)
    try:
        candidates = sorted(root.iterdir(), key=lambda item: item.name.lower())
    except OSError as exc:
        logger.debug("Could not list Chrome profiles under %s: %s", root, exc)
        return []

    profiles: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.is_dir() or not _is_chrome_profile_dir(candidate):
            continue
        if not (candidate / "Preferences").exists() and candidate.name != "Default":
            continue
        directory = candidate.name.strip()
        if not directory or directory.lower() in seen:
            continue
        seen.add(directory.lower())
        profiles.append({"name": _chrome_profile_display_name(candidate), "directory": directory})
    return profiles


def last_used_chrome_profile(user_data_dir: str = "") -> str:
    """The profile Chrome itself opens by default ('' when unknown)."""
    root = user_data_dir or system_chrome_user_data_dir()
    if not root:
        return ""
    try:
        state = json.loads((Path(root) / "Local State").read_text(encoding="utf-8"))
        return str(state.get("profile", {}).get("last_used") or "")
    except (OSError, ValueError, UnicodeError, AttributeError):
        return ""


def _extract_user_data_dir_from_cmdline(cmdline: list[str] | tuple[str, ...] | None) -> str:
    parts = [str(item) for item in (cmdline or []) if str(item).strip()]
    for index, part in enumerate(parts):
        if part.startswith("--user-data-dir="):
            return part.split("=", 1)[1].strip().strip('"')
        if part == "--user-data-dir" and index + 1 < len(parts):
            return parts[index + 1].strip().strip('"')
    return ""


def _system_chrome_running(user_data_dir: str) -> bool:
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


def prepare_local_chrome_debugging(user_data_dir: str) -> None:
    """Enable Chrome's user-approved remote debugging while its preferences are not in use."""
    state_path = Path(user_data_dir) / "Local State"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if state.get("devtools", {}).get("remote_debugging", {}).get("user-enabled") is True:
            return
        if _system_chrome_running(user_data_dir):
            raise BrowserLaunchError(
                "Close Chrome once so Monaw can finish automatic browser setup, then retry. "
                "Your profile and logins are preserved.", reason_code="chrome_setup_requires_restart",
            )
        state.setdefault("devtools", {}).setdefault("remote_debugging", {})["user-enabled"] = True
        temporary = state_path.with_name("Local State.monaw.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(state_path)
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        raise BrowserLaunchError("Could not prepare your Chrome profile for automation.",
                                 reason_code="chrome_profile_setup_failed") from exc


def read_system_cdp_url(user_data_dir: str) -> str:
    """Discover Chrome's user-approved debugging endpoint from DevToolsActivePort (Chrome 144+)."""
    try:
        lines = (Path(user_data_dir) / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
        port = int(lines[0].strip())
        endpoint = lines[1].strip()
        if 0 < port <= 65535 and endpoint.startswith("/devtools/browser/") and not any(
            char in endpoint for char in ("?", "#", " ", "\\")
        ):
            return f"ws://127.0.0.1:{port}{endpoint}"
    except (OSError, ValueError, IndexError, UnicodeError):
        pass
    return ""


def _local_debugging_endpoint_alive(endpoint: str) -> bool:
    try:
        parsed = urlparse(endpoint)
        with socket.create_connection(("127.0.0.1", parsed.port), timeout=0.5):
            return True
    except (OSError, ValueError, TypeError):
        return False


def _launch_personal_chrome(args: list[str]) -> None:
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


async def launch_local_chrome(profile_directory: str = "") -> tuple[str, str]:
    """Open the user's real profile and return (approved CDP endpoint, profile directory).

    Reuses a live endpoint instead of opening another window. Monaw never owns
    the launched Chrome; disconnecting leaves it running.
    """
    executable = find_chrome_executable()
    user_data_dir = system_chrome_user_data_dir()
    if not executable:
        raise BrowserLaunchError("Chrome executable not found.", reason_code="chrome_executable_missing")
    if not user_data_dir:
        raise BrowserLaunchError("Chrome profile folder not found.", reason_code="chrome_profile_missing")
    selected_profile = profile_directory or last_used_chrome_profile(user_data_dir) or "Default"
    if selected_profile not in {profile["directory"] for profile in list_local_chrome_profiles()}:
        raise BrowserLaunchError("Choose an existing Chrome profile.", reason_code="chrome_profile_missing")

    await asyncio.to_thread(prepare_local_chrome_debugging, user_data_dir)

    # Chrome 136+ blocks debugging-port flags on the default data folder, so use
    # the Chrome 144+ user-approved endpoint shared with chrome-devtools-mcp.
    existing = read_system_cdp_url(user_data_dir)
    if existing and await asyncio.to_thread(_local_debugging_endpoint_alive, existing):
        return existing, selected_profile
    args = [executable, f"--user-data-dir={user_data_dir}", f"--profile-directory={selected_profile}",
            "--enable-features=DevToolsAcceptDebuggingConnections", "--start-maximized", "--new-window", "about:blank"]
    await asyncio.to_thread(_launch_personal_chrome, args)
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        endpoint = await asyncio.to_thread(read_system_cdp_url, user_data_dir)
        if endpoint and await asyncio.to_thread(_local_debugging_endpoint_alive, endpoint):
            return endpoint, selected_profile
        await asyncio.sleep(0.25)
    raise BrowserLaunchError(
        "Chrome opened with your profile, but the automation connection is not ready. "
        "If Chrome was already running, close it once and retry so Monaw's launch settings can take effect. "
        "Chrome 144+ is required; its connection prompt still needs your approval.",
        reason_code="system_debugging_not_enabled",
    )
