"""Approve the native Windows Chrome prompt during a local connection attempt.

Chrome has no persisted approval. This bounded watcher clicks the native Allow
button identified through accessibility; it never acts on web content. Existing and
ambiguous dialogs are left to the user.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

logger = logging.getLogger("uvicorn.error.chrome_approval")
_TITLE = "Allow remote debugging?"


def _endpoint_chrome_pid(endpoint: str) -> int:
    import psutil

    parsed = urlparse(endpoint)
    if (parsed.scheme != "ws" or parsed.hostname != "127.0.0.1"
            or not parsed.path.startswith("/devtools/browser/")
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        return 0
    from browser_use.skill_cli.utils import find_chrome_executable
    from pathlib import Path

    executable = find_chrome_executable()
    if not executable:
        return 0
    for connection in psutil.net_connections(kind="tcp"):
        if (connection.status != psutil.CONN_LISTEN or not connection.pid
                or not connection.laddr or connection.laddr.port != parsed.port
                or connection.laddr.ip not in {"127.0.0.1", "::1"}):
            continue
        process = psutil.Process(connection.pid)
        if Path(process.exe()).resolve() == Path(executable).resolve():
            return process.pid
    return 0


def _native_approval_button(window):
    """Find a unique native dialog, excluding controls beneath any Document."""
    matches = []
    for button in window.descendants(control_type="Button", title="Allow"):
        if not button.is_visible() or not button.is_enabled():
            continue
        ancestors = []
        ancestor = button.parent()
        for _ in range(16):
            if ancestor is None:
                break
            if ancestor.element_info.control_type == "Document":
                ancestors = []
                break
            ancestors.append(ancestor)
            ancestor = ancestor.parent()
        # Use the named native dialog ancestor, not a page or the entire
        # browser window. Chrome inserts several unnamed layout containers.
        for container in ancestors:
            if container.window_text() != _TITLE:
                continue
            children = container.descendants()
            names = {child.window_text() for child in children}
            if container.window_text() == _TITLE:
                names.add(_TITLE)
            if {_TITLE, "Cancel", "Turn off in settings"}.issubset(names):
                matches.append(button)
                break
    return matches


def _approval_candidates(desktop, pid: int):
    candidates = {}
    for window in desktop.windows(process=pid, visible_only=True):
        if window.class_name() not in {"Chrome_WidgetWin_0", "Chrome_WidgetWin_1"}:
            continue
        for button in _native_approval_button(window):
            # An owned dialog can appear both as a top-level window and under
            # its owner. These are the same native button, not two prompts.
            runtime_id = tuple(button.element_info.runtime_id or ())
            if runtime_id:
                candidates[runtime_id] = button
    return list(candidates.values())


def _diagnose_missing_prompt(desktop, pid: int) -> None:
    details = []
    for window in desktop.windows(process=pid, visible_only=True):
        buttons = window.descendants(control_type="Button", title="Allow")
        details.append({"class": window.class_name(), "allow_buttons": len(buttons),
                        "named_dialogs": len(window.descendants(title=_TITLE)),
                        "buttons": [{"visible": b.is_visible(), "enabled": b.is_enabled()} for b in buttons]})
    logger.warning("Chrome approval waiting for native dialog; accessibility: %s", details)


def _watch_approval(endpoint: str, stopped: threading.Event, ready: threading.Event) -> None:
    initialized = False
    try:
        pid = _endpoint_chrome_pid(endpoint)
        if not pid:
            logger.warning("Chrome automatic approval: could not verify the local Chrome listener")
            return
        if stopped.is_set():
            return
        import pythoncom
        from pywinauto import Desktop

        pythoncom.CoInitialize()
        initialized = True
        desktop = Desktop(backend="uia")
        # Do not accept a prompt that predates Monaw's connection attempt.
        if _approval_candidates(desktop, pid):
            logger.warning("Chrome automatic approval: existing prompt requires manual approval")
            return
        ready.set()
        deadline = time.monotonic() + 60
        diagnosed = False
        while not stopped.wait(0.2) and time.monotonic() < deadline:
            candidates = _approval_candidates(desktop, pid)
            if not candidates and not diagnosed and time.monotonic() > deadline - 57:
                _diagnose_missing_prompt(desktop, pid)
                diagnosed = True
            if len(candidates) > 1:
                logger.warning("Chrome automatic approval: multiple prompts require manual approval")
                return
            if candidates:
                if not stopped.is_set():
                    button = candidates[0]
                    # Chrome's consent button ignores the UIA Invoke pattern.
                    # Use a real click on the verified native control instead.
                    button.top_level_parent().set_focus()
                    import win32gui
                    import win32process
                    _, foreground_pid = win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())
                    if (foreground_pid != pid or stopped.is_set()
                            or not button.is_visible() or not button.is_enabled()):
                        logger.warning("Chrome automatic approval: native dialog could not receive focus")
                        return
                    button.click_input()
                    logger.info("Chrome approval: clicked native Allow during local connection")
                return
    except Exception as exc:
        logger.warning("Chrome automatic approval unavailable; use manual approval: %s", exc)
    finally:
        ready.set()
        if initialized:
            pythoncom.CoUninitialize()


@asynccontextmanager
async def approve_chrome_connection(endpoint: str, *, enabled: bool = True):
    if not enabled or sys.platform != "win32":
        yield
        return
    stopped, ready = threading.Event(), threading.Event()
    thread = threading.Thread(target=_watch_approval, args=(endpoint, stopped, ready),
                              name="chrome-connection-approval", daemon=True)
    thread.start()
    try:
        # Finish the baseline observation before initiating the connection.
        if not await asyncio.to_thread(ready.wait, 3):
            stopped.set()
            logger.warning("Chrome automatic approval setup timed out; use manual approval")
        yield
    finally:
        stopped.set()
        await asyncio.to_thread(thread.join, 1)
