from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.agent.runtime_paths import MONAW_HOME_DIR
from app.agent.settings_store import BrowserUseSettings

logger = logging.getLogger(__name__)

_RUNTIME_ROOT = MONAW_HOME_DIR / "browser"
_MANAGED_PROFILE_DIR = _RUNTIME_ROOT / "profiles" / "managed"
_DOWNLOADS_DIR = _RUNTIME_ROOT / "downloads"
_SCREENSHOTS_DIR = _RUNTIME_ROOT / "screenshots"
_TRACES_DIR = _RUNTIME_ROOT / "traces"
_CHROME_LOG_PATH = _RUNTIME_ROOT / "chrome.log"
_CHROME_LOG_MAX_BYTES = 5 * 1024 * 1024

_MANAGER: BrowserUseManager | None = None
_MANAGER_FINGERPRINT = ""


def _browser_use_installed() -> bool:
    return importlib.util.find_spec("browser_use") is not None


def ensure_browser_use_runtime_dirs() -> dict[str, str]:
    for path in (_MANAGED_PROFILE_DIR, _DOWNLOADS_DIR, _SCREENSHOTS_DIR, _TRACES_DIR):
        path.mkdir(parents=True, exist_ok=True)
    (_MANAGED_PROFILE_DIR / "Default").mkdir(parents=True, exist_ok=True)
    return {
        "managed_profile_dir": str(_MANAGED_PROFILE_DIR),
        "downloads_dir": str(_DOWNLOADS_DIR),
        "screenshots_dir": str(_SCREENSHOTS_DIR),
        "traces_dir": str(_TRACES_DIR),
    }


def _default_browser_config() -> dict[str, Any]:
    ensure_browser_use_runtime_dirs()
    return BrowserUseSettings().model_dump()


def _settings_to_browser_config(settings) -> dict[str, Any]:
    defaults = _default_browser_config()
    raw = getattr(settings, "browser", None)
    if raw is None:
        return defaults

    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    elif hasattr(raw, "__dict__"):
        raw = dict(vars(raw))

    if not isinstance(raw, dict):
        return defaults

    merged = dict(defaults)
    merged.update({key: value for key, value in raw.items() if key in defaults})
    for key, fallback in (
        ("managed_profile_dir", str(_MANAGED_PROFILE_DIR)),
        ("downloads_dir", str(_DOWNLOADS_DIR)),
        ("screenshots_dir", str(_SCREENSHOTS_DIR)),
        ("traces_dir", str(_TRACES_DIR)),
    ):
        if not merged.get(key):
            merged[key] = fallback
        Path(str(merged[key])).mkdir(parents=True, exist_ok=True)

    merged["allowed_domains"] = [
        str(item).strip()
        for item in (merged.get("allowed_domains") or [])
        if str(item).strip()
    ]
    return merged


def _config_fingerprint(config: dict[str, Any]) -> str:
    return json.dumps(config, sort_keys=True)


_CHROME_PROCESS_NAMES = {"chrome.exe", "google chrome", "google-chrome", "chromium", "chromium.exe"}
_CHROME_SESSION_RESTORE_PATHS = (
    "Current Session",
    "Current Tabs",
    "Last Session",
    "Last Tabs",
    "Session Storage",
    "Sessions",
)
_SYSTEM_LAUNCH_DISABLED_MESSAGE = (
    "System Chrome mode can only attach to an already-running DevTools endpoint. "
    "Chrome no longer exposes --remote-debugging-port for the default user-data-dir, "
    "so this app will not launch your real Chrome profile and wait on a port that will never bind. "
    "Start Chrome yourself with a non-default --user-data-dir and set system_cdp_url, "
    "or use managed mode."
)


class BrowserLaunchError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _rotate_file_if_needed(path: Path, *, max_bytes: int = _CHROME_LOG_MAX_BYTES) -> None:
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return
        rotated = path.with_name(f"{path.name}.1")
        if rotated.exists():
            rotated.unlink()
        path.replace(rotated)
    except OSError as exc:
        logger.debug("browser-use: could not rotate log %s: %s", path, exc)


def _open_chrome_log_handle():
    _CHROME_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _rotate_file_if_needed(_CHROME_LOG_PATH)
    return _CHROME_LOG_PATH.open("ab")


def _probe_cdp_endpoint(cdp_url: str, timeout: float = 1.5) -> str:
    """Return a usable CDP URL if Chrome is live at the given endpoint, else ''.

    Uses a simple HTTP probe of /json/version instead of full process discovery,
    so it does not require Chrome to be findable by process scan.
    """
    normalized = str(cdp_url or "").strip()
    if not normalized:
        return ""
    if normalized.startswith(("ws://", "wss://")):
        return normalized

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        return normalized

    probe_url = f"{parsed.scheme}://{parsed.netloc}/json/version"
    try:
        req = urllib.request.Request(probe_url, headers={"User-Agent": "browser-use-probe"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return ""
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return ""

    try:
        data = json.loads(body)
    except ValueError:
        return normalized

    ws_url = str(data.get("webSocketDebuggerUrl") or "").strip()
    return ws_url or normalized


def _system_chrome_user_data_dir() -> str:
    """Locate the user's real Chrome user-data-dir.

    Returns an empty string if none can be located (non-Windows or Chrome not installed).
    """
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
    preferences_path = profile_dir / "Preferences"
    try:
        data = json.loads(preferences_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return profile_dir.name

    profile = data.get("profile")
    if not isinstance(profile, dict):
        return profile_dir.name

    name = str(profile.get("name") or "").strip()
    return name or profile_dir.name


def _is_chrome_profile_dir(path: Path) -> bool:
    name = path.name
    if name in {"Default", "Guest Profile"}:
        return True
    return name.startswith("Profile ") and name.removeprefix("Profile ").isdigit()


def _list_local_chrome_profiles() -> list[dict[str, str]]:
    user_data_dir = _system_chrome_user_data_dir()
    if not user_data_dir:
        return []

    root = Path(user_data_dir)
    try:
        candidates = sorted(root.iterdir(), key=lambda item: item.name.lower())
    except OSError as exc:
        logger.debug("browser-use: could not list Chrome profiles under %s: %s", root, exc)
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
        profiles.append(
            {
                "name": _chrome_profile_display_name(candidate),
                "directory": directory,
            }
        )
    return profiles


async def _wait_for_cdp_endpoint(
    cdp_url: str,
    *,
    proc: subprocess.Popen[bytes] | None = None,
    timeout: float = 40.0,
) -> str:
    """Poll the CDP /json/version endpoint until alive or timeout.

    Returns the resolved ws/http URL on success, or '' on timeout.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(timeout, 1.0)
    while loop.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return ""
        resolved = await asyncio.to_thread(_probe_cdp_endpoint, cdp_url, 1.0)
        if resolved:
            return resolved
        await asyncio.sleep(0.25)
    return ""


def _clear_chrome_singleton_locks(user_data_dir: str) -> None:
    """Remove Chrome's SingletonLock/Socket/Cookie files.

    When Chrome processes are killed abruptly, these files can persist for
    several seconds, causing the next Chrome launch to forward to the (now
    dead) old instance instead of binding its own --remote-debugging-port.
    """
    if not user_data_dir:
        return
    root = Path(user_data_dir)
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        candidate = root / name
        try:
            if candidate.exists() or candidate.is_symlink():
                candidate.unlink()
        except OSError as exc:
            logger.debug("browser-use: could not remove %s: %s", candidate, exc)


def _mark_chrome_profile_cleanly_closed(profile_dir: Path) -> None:
    preferences_path = profile_dir / "Preferences"
    try:
        if not preferences_path.exists():
            return
        data = json.loads(preferences_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        profile = data.setdefault("profile", {})
        if not isinstance(profile, dict):
            profile = {}
            data["profile"] = profile
        profile["exit_type"] = "Normal"
        profile["exited_cleanly"] = True
        preferences_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    except (OSError, ValueError, TypeError) as exc:
        logger.debug("browser-use: could not update Chrome clean-exit marker %s: %s", preferences_path, exc)


def _clear_chrome_session_restore_state(user_data_dir: str) -> list[str]:
    """Clear Chrome tab/window restore state while preserving cookies/logins."""
    if not user_data_dir:
        return []

    root = Path(user_data_dir)
    default_profile = root / "Default"
    removed: list[str] = []

    for base in (root, default_profile):
        for name in _CHROME_SESSION_RESTORE_PATHS:
            candidate = base / name
            try:
                if candidate.is_dir():
                    shutil.rmtree(candidate)
                    removed.append(str(candidate))
                elif candidate.exists() or candidate.is_symlink():
                    candidate.unlink()
                    removed.append(str(candidate))
            except OSError as exc:
                logger.debug("browser-use: could not remove Chrome session state %s: %s", candidate, exc)

    _mark_chrome_profile_cleanly_closed(default_profile)
    return removed


def _extract_user_data_dir_from_cmdline(cmdline: list[str] | tuple[str, ...] | None) -> str:
    parts = [str(item) for item in (cmdline or []) if str(item).strip()]
    for index, part in enumerate(parts):
        if part.startswith("--user-data-dir="):
            return part.split("=", 1)[1].strip().strip('"')
        if part == "--user-data-dir" and index + 1 < len(parts):
            return parts[index + 1].strip().strip('"')
    return ""


def _normalize_path_for_match(path: str) -> str:
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except OSError:
        resolved = Path(path).expanduser()
    return os.path.normcase(os.path.normpath(str(resolved)))


def _path_is_within_root(path: str, root: str) -> bool:
    normalized_path = _normalize_path_for_match(path)
    normalized_root = _normalize_path_for_match(root)
    if not normalized_path or not normalized_root:
        return False
    prefix = normalized_root.rstrip("\\/")
    if normalized_path == prefix:
        return True
    return normalized_path.startswith(prefix + os.sep)


def _kill_existing_chrome_processes(user_data_root: str = "") -> int:
    """Terminate running Chrome/Chromium processes so the user-data-dir lock is released.

    Returns the number of processes asked to terminate. Chrome enforces one
    process per user-data-dir; if a matching profile is already in use, a launch
    attempt can silently activate the old window without enabling CDP. When
    ``user_data_root`` is provided, only runtime-managed Chrome sessions rooted
    under that directory are targeted.
    """
    try:
        import psutil
    except ImportError:
        logger.warning("browser-use cannot kill existing Chrome: psutil is not installed.")
        return 0

    normalized_root = str(user_data_root or "").strip()
    targets: list[Any] = []
    for proc in psutil.process_iter(attrs=["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name in _CHROME_PROCESS_NAMES:
            if normalized_root:
                try:
                    proc_user_data_dir = _extract_user_data_dir_from_cmdline(proc.info.get("cmdline"))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if not _path_is_within_root(proc_user_data_dir, normalized_root):
                    continue
            targets.append(proc)

    if not targets:
        return 0

    for proc in targets:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    try:
        _, alive = psutil.wait_procs(targets, timeout=3)
    except Exception:
        alive = []
    for proc in alive:
        try:
            proc.kill()
        except Exception:
            pass

    return len(targets)


def _terminate_subprocess(proc: subprocess.Popen[bytes] | None, timeout: float = 3.0) -> int | None:
    if proc is None:
        return None
    try:
        returncode = proc.poll()
    except Exception:
        returncode = None
    if returncode is not None:
        return returncode

    try:
        proc.terminate()
    except Exception:
        return None

    deadline = time.monotonic() + max(timeout, 0.1)
    while time.monotonic() < deadline:
        try:
            returncode = proc.poll()
        except Exception:
            returncode = None
        if returncode is not None:
            return returncode
        time.sleep(0.1)

    try:
        proc.kill()
    except Exception:
        return None
    try:
        return proc.wait(timeout=1.0)
    except Exception:
        return proc.poll()


def _build_managed_launch_args(
    chrome_executable: str,
    user_data_dir: str,
    cdp_port: int,
    *,
    headless: bool = False,
) -> list[str]:
    args = [
        chrome_executable,
        f"--remote-debugging-port={cdp_port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={user_data_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--disable-infobars",
        "--disable-blink-features=AutomationControlled",
        "--lang=en-US,en",
        "--disable-features=DevToolsDebuggingRestrictions",
    ]
    if headless:
        args.extend(["--headless=new", "--disable-gpu"])
    return args


def _target_id_from_info(info: Any) -> str:
    if isinstance(info, dict):
        return str(info.get("targetId") or info.get("target_id") or "")
    return str(getattr(info, "targetId", getattr(info, "target_id", "")) or "")


class BrowserUseManager:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self._lock = asyncio.Lock()
        self._browser = None
        self._current_mode = ""
        self._current_system_connection = ""
        self._current_profile_directory = ""
        self._current_cdp_url = ""
        self._last_error = ""
        self._chrome_executable = ""
        self._launched_chrome_proc: subprocess.Popen[bytes] | None = None
        self._restart_requested = False
        self._recent_launches: deque[dict[str, Any]] = deque(maxlen=10)
        self._dom_snapshot_cache: dict[str, dict[str, Any]] = {}
        self._dom_snapshot_cache_reason = ""

    def _record_launch(
        self,
        *,
        started_at: str,
        mode: str,
        reason_code: str,
        cdp_wait_seconds: float = 0.0,
        exit_code: int | None = None,
        error: str = "",
        cdp_url: str = "",
        cdp_port: int | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "started_at": started_at,
            "mode": mode,
            "reason_code": reason_code,
            "cdp_wait_seconds": round(max(cdp_wait_seconds, 0.0), 3),
        }
        if exit_code is not None:
            entry["exit_code"] = exit_code
        if error:
            entry["error"] = error
        if cdp_url:
            entry["cdp_url"] = cdp_url
        if cdp_port is not None:
            entry["cdp_port"] = cdp_port
        self._recent_launches.append(entry)

    def update(self, config: dict[str, Any]) -> None:
        if dict(config) != self.config:
            self._restart_requested = True
            self.clear_dom_snapshot_cache("settings_changed")
        self.config = dict(config)

    def _preferred_mode(self) -> str:
        configured = str(self.config.get("mode", "auto") or "").strip().lower()
        if configured in {"auto", "managed", "system"}:
            return configured
        return "auto"

    def _system_connection_strategy(self) -> str:
        configured = str(self.config.get("system_connection_strategy", "auto") or "").strip().lower()
        if configured in {"auto", "attach", "launch"}:
            return configured
        return "auto"

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        browser = self._browser
        proc = self._launched_chrome_proc
        previous_mode = self._current_mode
        self._browser = None
        self._launched_chrome_proc = None
        self._current_mode = ""
        self._current_system_connection = ""
        self._current_profile_directory = ""
        self._current_cdp_url = ""
        self.clear_dom_snapshot_cache("browser_stopped")
        if browser is not None:
            try:
                await browser.stop()
            except Exception as exc:
                logger.warning("browser-use stop failed: %s", exc)
        if proc is not None:
            returncode = _terminate_subprocess(proc)
            if returncode is None:
                logger.debug("browser-use launched-process terminate failed.")
        if previous_mode == "managed":
            _clear_chrome_singleton_locks(str(self.config.get("managed_profile_dir", "")))

    async def _existing_browser_or_start_default(self):
        return await self.ensure_browser("auto")

    def _browser_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headless": bool(self.config.get("headless", False)),
            "keep_alive": bool(self.config.get("keep_alive", True)),
            "downloads_path": self.config["downloads_dir"],
            "traces_dir": self.config["traces_dir"],
            "accept_downloads": False,
            "auto_download_pdfs": False,
            "allowed_domains": self.config.get("allowed_domains") or None,
            "window_size": {"width": 1440, "height": 900},
            "minimum_wait_page_load_time": 0.25,
            "wait_for_network_idle_page_load_time": 0.5,
            "wait_between_actions": 0.1,
        }
        return kwargs

    async def _start_managed_locked(self):
        import socket

        from browser_use import Browser
        from browser_use.skill_cli.utils import find_chrome_executable

        logger.info("browser-use[manager-v2]: entering _start_managed_locked (isolated profile via subprocess).")

        kwargs = self._browser_kwargs()
        managed_dir = str(self.config["managed_profile_dir"])
        Path(managed_dir).mkdir(parents=True, exist_ok=True)

        chrome_executable = find_chrome_executable()
        if not chrome_executable:
            raise BrowserLaunchError(
                "System Chrome executable not found; cannot launch managed Chrome. "
                "Install Chrome or configure a supported Chrome/Chromium executable.",
                reason_code="chrome_executable_missing",
            )
        self._chrome_executable = chrome_executable

        # Pick a free local port so the managed instance does not collide with the
        # system port (9222) used in system mode.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            cdp_port = sock.getsockname()[1]

        terminated = _kill_existing_chrome_processes(str(_RUNTIME_ROOT))
        if terminated:
            logger.info(
                "browser-use: terminated %d existing runtime Chrome process(es) before managed launch.",
                terminated,
            )
        _clear_chrome_singleton_locks(managed_dir)
        cleared_session_paths = _clear_chrome_session_restore_state(managed_dir)
        if cleared_session_paths:
            logger.info(
                "browser-use: cleared %d managed Chrome session restore item(s) before launch.",
                len(cleared_session_paths),
            )

        launch_args = _build_managed_launch_args(
            chrome_executable,
            managed_dir,
            cdp_port,
            headless=bool(self.config.get("headless", False)),
        )
        popen_kwargs: dict[str, Any] = {"close_fds": True}
        creation_flags = 0
        for flag_name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
            creation_flags |= getattr(subprocess, flag_name, 0)
        if creation_flags:
            popen_kwargs["creationflags"] = creation_flags

        cdp_base_url = f"http://127.0.0.1:{cdp_port}"

        async def launch_once(attempt: int) -> Any:
            started_at = _now_utc_iso()
            log_handle = None
            attempt_kwargs = dict(popen_kwargs)
            try:
                log_handle = _open_chrome_log_handle()
                attempt_kwargs.update({"stdout": log_handle, "stderr": log_handle})
                logger.info(
                    "browser-use: launching managed Chrome '%s' (user-data-dir: %s, CDP port %d, attempt %d).",
                    chrome_executable,
                    managed_dir,
                    cdp_port,
                    attempt + 1,
                )
                proc = subprocess.Popen(launch_args, **attempt_kwargs)
            except Exception as exc:
                message = f"Failed to launch managed Chrome subprocess: {exc}"
                self._record_launch(
                    started_at=started_at,
                    mode="managed",
                    reason_code="managed_subprocess_failed",
                    error=message,
                    cdp_port=cdp_port,
                )
                raise BrowserLaunchError(message, reason_code="managed_subprocess_failed") from exc
            finally:
                if log_handle is not None:
                    try:
                        log_handle.close()
                    except OSError:
                        pass

            self._launched_chrome_proc = proc
            wait_started = time.monotonic()
            resolved_cdp_url = await _wait_for_cdp_endpoint(cdp_base_url, proc=proc, timeout=40.0)
            cdp_wait_seconds = time.monotonic() - wait_started
            if not resolved_cdp_url:
                observed_exit_code = proc.poll()
                self._launched_chrome_proc = None
                if observed_exit_code is None:
                    _terminate_subprocess(proc)
                    _clear_chrome_singleton_locks(managed_dir)
                    message = (
                        f"Managed Chrome was launched but CDP did not come up on port {cdp_port} "
                        f"within 40s. See {_CHROME_LOG_PATH} for Chrome subprocess output."
                    )
                    self._record_launch(
                        started_at=started_at,
                        mode="managed",
                        reason_code="managed_cdp_timeout",
                        cdp_wait_seconds=cdp_wait_seconds,
                        error=message,
                        cdp_port=cdp_port,
                    )
                    raise BrowserLaunchError(message, reason_code="managed_cdp_timeout")

                _clear_chrome_singleton_locks(managed_dir)
                message = (
                    f"Managed Chrome exited before CDP came up on port {cdp_port} "
                    f"(exit code {observed_exit_code}). See {_CHROME_LOG_PATH} for Chrome subprocess output."
                )
                self._record_launch(
                    started_at=started_at,
                    mode="managed",
                    reason_code="managed_chrome_exited",
                    cdp_wait_seconds=cdp_wait_seconds,
                    exit_code=observed_exit_code,
                    error=message,
                    cdp_port=cdp_port,
                )
                raise BrowserLaunchError(message, reason_code="managed_chrome_exited")

            browser = Browser(cdp_url=resolved_cdp_url, is_local=True, **kwargs)
            try:
                await browser.start()
            except Exception as exc:
                message = f"Managed Chrome CDP resolved but browser-use failed to attach: {exc}"
                self._record_launch(
                    started_at=started_at,
                    mode="managed",
                    reason_code="managed_cdp_timeout",
                    cdp_wait_seconds=cdp_wait_seconds,
                    error=message,
                    cdp_url=resolved_cdp_url,
                    cdp_port=cdp_port,
                )
                raise BrowserLaunchError(message, reason_code="managed_cdp_timeout") from exc

            self._browser = browser
            self._current_mode = "managed"
            self._current_system_connection = ""
            self._current_profile_directory = "Default"
            self._current_cdp_url = resolved_cdp_url
            self._last_error = ""
            self._record_launch(
                started_at=started_at,
                mode="managed",
                reason_code="ok",
                cdp_wait_seconds=cdp_wait_seconds,
                cdp_url=resolved_cdp_url,
                cdp_port=cdp_port,
            )
            return browser

        try:
            return await launch_once(0)
        except BrowserLaunchError as exc:
            if exc.reason_code != "managed_chrome_exited":
                raise
            logger.info("browser-use: retrying managed Chrome launch once after early Chrome exit.")
            _kill_existing_chrome_processes(str(_RUNTIME_ROOT))
            await asyncio.sleep(1.0)
            _clear_chrome_singleton_locks(managed_dir)
            return await launch_once(1)

    async def _attach_system_locked(self, cdp_url: str, profile_directory: str = ""):
        from browser_use import Browser
        from browser_use.skill_cli.utils import find_chrome_executable

        kwargs = self._browser_kwargs()
        browser = Browser(
            cdp_url=cdp_url,
            is_local=True,
            **kwargs,
        )
        await browser.start()
        self._browser = browser
        self._current_mode = "system"
        self._current_system_connection = "attach"
        self._current_profile_directory = profile_directory or str(self.config.get("system_profile_directory", "")).strip()
        self._current_cdp_url = cdp_url
        self._last_error = ""
        self._chrome_executable = find_chrome_executable() or ""
        self._record_launch(
            started_at=_now_utc_iso(),
            mode="system",
            reason_code="ok",
            cdp_url=cdp_url,
        )
        return browser

    async def _reattach_current_locked(self, reason: str = ""):
        """Reconnect the Python browser-use handle without closing Chrome.

        A stopped turn can leave the visible Chrome window open while the
        in-memory browser-use object becomes stale. Reattaching to the same CDP
        endpoint preserves in-progress pages such as a Gmail compose draft.
        """
        cdp_url = str(self._current_cdp_url or "").strip()
        if not cdp_url:
            return None
        resolved_cdp_url = await asyncio.to_thread(_probe_cdp_endpoint, cdp_url)
        if not resolved_cdp_url:
            self._last_error = f"{reason}; CDP endpoint is no longer reachable at {cdp_url}.".strip("; ")
            return None

        from browser_use import Browser

        try:
            browser = Browser(cdp_url=resolved_cdp_url, is_local=True, **self._browser_kwargs())
            await browser.start()
        except Exception as exc:
            self._last_error = f"{reason}; browser reattach failed: {exc}".strip("; ")
            logger.warning("browser-use reattach failed: %s", exc)
            return None

        self._browser = browser
        self._current_cdp_url = resolved_cdp_url
        self._last_error = ""
        logger.info("browser-use: reattached to existing Chrome CDP endpoint after stale handle (%s).", reason)
        return browser

    async def _reconnect_current_browser(self, reason: str = ""):
        async with self._lock:
            return await self._reattach_current_locked(reason)

    async def _current_session_alive_locked(self) -> bool:
        browser = self._browser
        if browser is None:
            return False
        current_cdp_url = str(self._current_cdp_url or "").strip()
        if not current_cdp_url:
            return True

        try:
            is_cdp_connected = getattr(browser, "is_cdp_connected")
        except AttributeError:
            is_cdp_connected = None
        except Exception as exc:
            self._last_error = f"Could not inspect browser CDP connection state: {exc}"
            return False
        if is_cdp_connected is False:
            self._last_error = "Current browser-use CDP WebSocket is disconnected."
            return False

        resolved_cdp_url = await asyncio.to_thread(_probe_cdp_endpoint, current_cdp_url)
        if not resolved_cdp_url:
            self._last_error = (
                f"Current browser session is no longer reachable at {current_cdp_url}."
            )
            return False

        try:
            await browser.get_tabs()
        except Exception as exc:
            self._last_error = f"Current browser-use handle cannot list tabs: {exc}"
            return False

        self._current_cdp_url = resolved_cdp_url
        return True

    async def _launch_system_locked(self, profile_directory: str = ""):
        from browser_use.skill_cli.utils import find_chrome_executable

        selected_profile = (
            profile_directory
            or str(self.config.get("system_profile_directory", "")).strip()
            or "Default"
        )
        chrome_executable = find_chrome_executable()
        user_data_dir = _system_chrome_user_data_dir()
        configured_cdp_url = (
            str(self.config.get("system_cdp_url", "") or "").strip()
            or "http://127.0.0.1:9222"
        )
        parsed_cdp = urlparse(configured_cdp_url)
        cdp_port = parsed_cdp.port or 9222
        cdp_base_url = f"http://127.0.0.1:{cdp_port}"

        if not chrome_executable:
            raise BrowserLaunchError(
                "System Chrome executable not found. Install Chrome or set the path.",
                reason_code="chrome_executable_missing",
            )
        if not user_data_dir:
            raise BrowserLaunchError(
                "Could not locate the real Chrome user-data-dir. "
                "System mode requires a real Chrome install.",
                reason_code="system_attach_failed",
            )

        logger.warning(
            "browser-use: refusing system Chrome launch for profile '%s' on %s "
            "(chrome=%s, user-data-dir=%s). %s",
            selected_profile,
            cdp_base_url,
            chrome_executable,
            user_data_dir,
            _SYSTEM_LAUNCH_DISABLED_MESSAGE,
        )
        raise BrowserLaunchError(_SYSTEM_LAUNCH_DISABLED_MESSAGE, reason_code="system_launch_disabled")

    async def _start_system_locked(self, profile_directory: str = ""):
        selected_profile = profile_directory or str(self.config.get("system_profile_directory", "")).strip()
        configured_cdp_url = str(self.config.get("system_cdp_url", "") or "").strip()
        strategy = self._system_connection_strategy()

        errors: list[tuple[str, str]] = []

        async def attempt_attach() -> Any | None:
            if not configured_cdp_url:
                return None
            resolved = await asyncio.to_thread(_probe_cdp_endpoint, configured_cdp_url)
            if not resolved:
                logger.info(
                    "browser-use: no live Chrome DevTools endpoint at %s.",
                    configured_cdp_url,
                )
                return None
            try:
                return await self._attach_system_locked(resolved, selected_profile)
            except Exception as exc:
                reason_code = str(getattr(exc, "reason_code", "system_attach_failed") or "system_attach_failed")
                errors.append((f"system attach failed: {exc}", reason_code))
                logger.warning("browser-use system attach failed: %s", exc)
                await self._stop_locked()
                return None

        async def attempt_launch() -> Any | None:
            try:
                return await self._launch_system_locked(selected_profile)
            except Exception as exc:
                reason_code = str(getattr(exc, "reason_code", "system_launch_disabled") or "system_launch_disabled")
                errors.append((f"system launch failed: {exc}", reason_code))
                logger.warning("browser-use system launch failed: %s", exc)
                await self._stop_locked()
                return None

        if strategy == "attach":
            browser = await attempt_attach()
            if browser is not None:
                return browser
        elif strategy == "launch":
            browser = await attempt_launch()
            if browser is not None:
                return browser
        else:  # auto - prefer attaching if a live CDP already exists, else fail fast.
            browser = await attempt_attach()
            if browser is not None:
                return browser
            browser = await attempt_launch()
            if browser is not None:
                return browser

        if errors:
            message = " | ".join(item[0] for item in errors)
            reason_code = errors[-1][1]
        else:
            message = "Unable to start system Chrome."
            reason_code = "system_attach_failed"
        raise BrowserLaunchError(message, reason_code=reason_code)

    async def ensure_browser(self, mode: str = "auto", profile_directory: str = ""):
        if not _browser_use_installed():
            raise BrowserLaunchError("browser-use is not installed.", reason_code="browser_use_missing")

        incoming_mode = mode if mode in {"auto", "managed", "system"} else "auto"
        requested_mode = incoming_mode
        if requested_mode == "auto":
            requested_mode = self._preferred_mode()
        logger.info(
            "browser-use[manager-v2]: ensure_browser(incoming=%s, resolved=%s, preferred=%s, profile=%r).",
            incoming_mode,
            requested_mode,
            self._preferred_mode(),
            profile_directory,
        )
        async with self._lock:
            if self._restart_requested:
                logger.info("browser-use: restarting browser session after settings change.")
                await self._stop_locked()
                self._restart_requested = False

            # If we previously launched Chrome and the process has exited
            # (user closed it, crashed, etc.), drop the stale browser handle
            # so we don't hand out a dead CDP session and loop forever.
            proc = self._launched_chrome_proc
            if self._browser is not None and proc is not None and proc.poll() is not None:
                logger.info(
                    "browser-use: launched Chrome process exited (code=%s); resetting session.",
                    proc.returncode,
                )
                await self._stop_locked()

            if self._browser is not None and not await self._current_session_alive_locked():
                logger.info(
                    "browser-use: current %s session is no longer reachable; resetting stale handle.",
                    self._current_mode or "browser",
                )
                await self._stop_locked()

            if self._browser is not None:
                if requested_mode == self._current_mode:
                    if requested_mode != "system":
                        return self._browser
                    selected_profile = profile_directory or str(self.config.get("system_profile_directory", "")).strip()
                    strategy = self._system_connection_strategy()
                    if selected_profile and selected_profile != self._current_profile_directory:
                        await self._stop_locked()
                    elif strategy != "auto" and self._current_system_connection and strategy != self._current_system_connection:
                        await self._stop_locked()
                    else:
                        return self._browser
                elif requested_mode == "auto":
                    return self._browser
                if self._browser is not None:
                    await self._stop_locked()

            errors: list[tuple[str, str]] = []
            candidates: list[tuple[str, str]] = []
            if requested_mode == "auto":
                candidates.append(("managed", ""))
                if bool(self.config.get("enable_system_fallback", True)):
                    candidates.append(("system", profile_directory))
            elif requested_mode == "managed":
                candidates.append(("managed", ""))
            else:
                candidates.append(("system", profile_directory))

            for candidate_mode, candidate_profile in candidates:
                try:
                    if candidate_mode == "managed":
                        return await self._start_managed_locked()
                    return await self._start_system_locked(candidate_profile)
                except Exception as exc:
                    message = f"{candidate_mode} start failed: {exc}"
                    reason_code = str(getattr(exc, "reason_code", "browser_launch_failed") or "browser_launch_failed")
                    errors.append((message, reason_code))
                    self._last_error = message
                    logger.warning("browser-use %s", message)
                    await self._stop_locked()

            if errors:
                message = " | ".join(item[0] for item in errors)
                reason_code = errors[-1][1]
            else:
                message = "Unable to start browser session."
                reason_code = "browser_launch_failed"
            raise BrowserLaunchError(message, reason_code=reason_code)

    async def _get_page_once(self, browser, *, target_id: str = "", index: int | None = None, create_if_missing: bool = False):
        if target_id or index is not None:
            await self.switch_tab(target_id=target_id, index=index)
        page = await browser.get_current_page()
        if page is not None:
            return page

        pages = await browser.get_pages()
        if pages:
            await self.switch_tab(target_id=_target_id_from_info(await pages[-1].get_target_info()))
            return await browser.get_current_page()

        if create_if_missing:
            return await browser.new_page()
        return None

    async def get_page(self, *, target_id: str = "", index: int | None = None, create_if_missing: bool = False):
        browser = await self._existing_browser_or_start_default()
        try:
            return await self._get_page_once(
                browser,
                target_id=target_id,
                index=index,
                create_if_missing=create_if_missing,
            )
        except Exception as exc:
            self._last_error = str(exc)
            recovered = await self._reconnect_current_browser(f"get_page failed: {exc}")
            if recovered is None:
                raise
            return await self._get_page_once(
                recovered,
                target_id=target_id,
                index=index,
                create_if_missing=create_if_missing,
            )

    def clear_dom_snapshot_cache(self, reason: str = "") -> None:
        self._dom_snapshot_cache = {}
        self._dom_snapshot_cache_reason = reason

    async def _dom_cache_key(self, page) -> str:
        try:
            metadata = await self.page_metadata(page)
        except Exception:
            return f"page:{id(page)}"
        target_id = str(metadata.get("target_id") or "").strip()
        if target_id:
            return f"target:{target_id}"
        return f"url:{metadata.get('url', '')}"

    async def store_dom_snapshot_cache(
        self,
        page,
        *,
        snapshot_id: str,
        refs: dict[str, dict[str, Any]],
    ) -> None:
        self._dom_snapshot_cache = {
            "key": await self._dom_cache_key(page),
            "snapshot_id": snapshot_id,
            "refs": dict(refs),
            "reason": "",
        }
        self._dom_snapshot_cache_reason = ""

    async def resolve_dom_snapshot_ref(self, page, ref: str) -> dict[str, Any] | None:
        normalized_ref = str(ref or "").strip()
        if not normalized_ref:
            return None
        cache = self._dom_snapshot_cache
        refs = cache.get("refs") if isinstance(cache, dict) else None
        if not isinstance(refs, dict) or normalized_ref not in refs:
            return None
        if cache.get("key") != await self._dom_cache_key(page):
            return {"stale": True, "ref": normalized_ref, "reason": "page_changed"}
        cached = refs.get(normalized_ref)
        return dict(cached) if isinstance(cached, dict) else None

    async def page_metadata(self, page) -> dict[str, str]:
        target_info = await page.get_target_info()
        return {
            "target_id": _target_id_from_info(target_info),
            "url": await page.get_url(),
            "title": await page.get_title(),
        }

    async def _tabs_from_browser(self, browser) -> list[dict[str, str]]:
        tabs = await browser.get_tabs()
        current_target = await browser.get_current_target_info()
        current_target_id = _target_id_from_info(current_target)
        items: list[dict[str, str]] = []
        for index, tab in enumerate(tabs):
            target_id = str(getattr(tab, "target_id", "") or "")
            items.append(
                {
                    "index": str(index),
                    "target_id": target_id,
                    "url": str(getattr(tab, "url", "") or ""),
                    "title": str(getattr(tab, "title", "") or ""),
                    "active": "true" if target_id == current_target_id else "false",
                }
            )
        return items

    async def tabs(self) -> list[dict[str, str]]:
        browser = await self._existing_browser_or_start_default()
        try:
            return await self._tabs_from_browser(browser)
        except Exception as exc:
            self._last_error = str(exc)
            recovered = await self._reconnect_current_browser(f"tabs failed: {exc}")
            if recovered is None:
                raise
            return await self._tabs_from_browser(recovered)

    async def _switch_tab_once(self, browser, *, target_id: str = "", index: int | None = None) -> dict[str, str]:
        from browser_use.browser.events import SwitchTabEvent

        selected_target_id = target_id
        if not selected_target_id and index is not None:
            tabs = await browser.get_tabs()
            if index < 0 or index >= len(tabs):
                raise ValueError(f"Tab index {index} is out of range.")
            selected_target_id = str(getattr(tabs[index], "target_id", "") or "")
        if not selected_target_id:
            raise ValueError("Provide a tab target_id or index.")

        await browser.on_SwitchTabEvent(SwitchTabEvent(target_id=selected_target_id))
        page = await browser.get_current_page()
        if page is None:
            raise RuntimeError("No active page after switching tabs.")
        return await self.page_metadata(page)

    async def switch_tab(self, *, target_id: str = "", index: int | None = None) -> dict[str, str]:
        browser = await self._existing_browser_or_start_default()
        try:
            return await self._switch_tab_once(browser, target_id=target_id, index=index)
        except Exception as exc:
            self._last_error = str(exc)
            recovered = await self._reconnect_current_browser(f"switch_tab failed: {exc}")
            if recovered is None:
                raise
            return await self._switch_tab_once(recovered, target_id=target_id, index=index)

    async def list_system_profiles(self) -> list[dict[str, str]]:
        local_profiles = await asyncio.to_thread(_list_local_chrome_profiles)
        if local_profiles:
            return local_profiles
        if not _browser_use_installed():
            return []
        try:
            from browser_use import Browser

            profiles = Browser.list_chrome_profiles()
        except Exception as exc:
            logger.warning("browser-use profile listing failed: %s", exc)
            return []

        normalized: list[dict[str, str]] = []
        for item in profiles:
            if not isinstance(item, dict):
                continue
            directory = str(item.get("directory", "") or "").strip()
            if not directory:
                continue
            normalized.append(
                {
                    "name": str(item.get("name", "") or "").strip() or directory,
                    "directory": directory,
                }
            )
        return normalized

    async def diagnostics(self) -> dict[str, Any]:
        current_page = None
        tab_count = 0
        chrome_executable = self._chrome_executable
        current_cdp_url = str(self._current_cdp_url or "").strip()
        cdp_endpoint_alive = False
        session_active = self._browser is not None
        if not chrome_executable and _browser_use_installed():
            try:
                from browser_use.skill_cli.utils import find_chrome_executable

                chrome_executable = find_chrome_executable() or ""
            except Exception:
                chrome_executable = ""
        if current_cdp_url:
            cdp_endpoint_alive = bool(await asyncio.to_thread(_probe_cdp_endpoint, current_cdp_url))
            if session_active and not cdp_endpoint_alive:
                session_active = False
        if session_active and self._browser is not None:
            try:
                is_cdp_connected = getattr(self._browser, "is_cdp_connected")
                if is_cdp_connected is False:
                    session_active = False
            except AttributeError:
                pass
            except Exception as exc:
                self._last_error = str(exc)
                session_active = False
        if self._browser is not None:
            try:
                tabs = await self._browser.get_tabs()
                tab_count = len(tabs)
                page = await self._browser.get_current_page()
                if page is not None:
                    current_page = await self.page_metadata(page)
            except Exception as exc:
                self._last_error = str(exc)

        return {
            "available": _browser_use_installed(),
            "session_active": session_active,
            "preferred_mode": str(self.config.get("mode", "auto")),
            "headless": bool(self.config.get("headless", False)),
            "current_mode": self._current_mode,
            "current_system_connection": self._current_system_connection,
            "fallback_enabled": bool(self.config.get("enable_system_fallback", True)),
            "dom_inspection_engine": str(self.config.get("dom_inspection_engine", "auto")),
            "paint_order_filtering": bool(self.config.get("paint_order_filtering", True)),
            "cross_origin_iframes": bool(self.config.get("cross_origin_iframes", False)),
            "max_iframes": int(self.config.get("max_iframes", 5) or 0),
            "max_iframe_depth": int(self.config.get("max_iframe_depth", 2) or 0),
            "last_error": self._last_error,
            "system_connection_strategy": self._system_connection_strategy(),
            "system_cdp_url": str(self.config.get("system_cdp_url", "")),
            "current_cdp_url": current_cdp_url,
            "cdp_endpoint_alive": cdp_endpoint_alive,
            "managed_profile_dir": str(self.config.get("managed_profile_dir", "")),
            "downloads_dir": str(self.config.get("downloads_dir", "")),
            "screenshots_dir": str(self.config.get("screenshots_dir", "")),
            "traces_dir": str(self.config.get("traces_dir", "")),
            "system_profile_directory": str(self.config.get("system_profile_directory", "")),
            "chrome_executable": chrome_executable,
            "chrome_log_path": str(_CHROME_LOG_PATH),
            "recent_launches": list(self._recent_launches),
            "available_system_profiles": await self.list_system_profiles(),
            "current_page": current_page,
            "tab_count": tab_count,
        }


def configure_browser_use_manager(settings) -> BrowserUseManager:
    global _MANAGER, _MANAGER_FINGERPRINT

    config = _settings_to_browser_config(settings)
    fingerprint = _config_fingerprint(config)
    if _MANAGER is None:
        _MANAGER = BrowserUseManager(config)
        _MANAGER_FINGERPRINT = fingerprint
        return _MANAGER

    if fingerprint != _MANAGER_FINGERPRINT:
        _MANAGER.update(config)
        _MANAGER_FINGERPRINT = fingerprint
    return _MANAGER


async def get_browser_use_diagnostics(settings) -> dict[str, Any]:
    config = _settings_to_browser_config(settings)
    diagnostics: dict[str, Any] = {
        "available": _browser_use_installed(),
        "session_active": False,
        "preferred_mode": str(config.get("mode", "auto")),
        "headless": bool(config.get("headless", False)),
        "current_mode": "",
        "current_system_connection": "",
        "fallback_enabled": bool(config.get("enable_system_fallback", True)),
        "dom_inspection_engine": str(config.get("dom_inspection_engine", "auto")),
        "paint_order_filtering": bool(config.get("paint_order_filtering", True)),
        "cross_origin_iframes": bool(config.get("cross_origin_iframes", False)),
        "max_iframes": int(config.get("max_iframes", 5) or 0),
        "max_iframe_depth": int(config.get("max_iframe_depth", 2) or 0),
        "last_error": "",
        "system_connection_strategy": str(config.get("system_connection_strategy", "auto")),
        "system_cdp_url": str(config.get("system_cdp_url", "")),
        "current_cdp_url": "",
        "cdp_endpoint_alive": False,
        "managed_profile_dir": str(config.get("managed_profile_dir", "")),
        "downloads_dir": str(config.get("downloads_dir", "")),
        "screenshots_dir": str(config.get("screenshots_dir", "")),
        "traces_dir": str(config.get("traces_dir", "")),
        "system_profile_directory": str(config.get("system_profile_directory", "")),
        "chrome_executable": "",
        "chrome_log_path": str(_CHROME_LOG_PATH),
        "recent_launches": [],
        "available_system_profiles": [],
        "current_page": None,
        "tab_count": 0,
    }
    if not _browser_use_installed():
        return diagnostics

    manager = configure_browser_use_manager(settings)
    diagnostics.update(await manager.diagnostics())
    return diagnostics


async def reset_browser_use_runtime(*, clear_managed_session: bool = False) -> None:
    global _MANAGER, _MANAGER_FINGERPRINT
    managed_profile_dir = str(_MANAGED_PROFILE_DIR)
    if _MANAGER is not None:
        managed_profile_dir = str(_MANAGER.config.get("managed_profile_dir", managed_profile_dir) or managed_profile_dir)
        await _MANAGER.stop()
    _MANAGER = None
    _MANAGER_FINGERPRINT = ""
    _kill_existing_chrome_processes(str(_RUNTIME_ROOT))
    _clear_chrome_singleton_locks(managed_profile_dir)
    if clear_managed_session:
        _clear_chrome_session_restore_state(managed_profile_dir)
