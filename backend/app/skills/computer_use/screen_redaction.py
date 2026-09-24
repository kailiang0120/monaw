"""Black out blocked windows in screen captures.

Screenshots grab raw screen pixels, so anything on top of (or behind) the
target shows up: a terminal, a password manager, the Monaw window itself. The
agent is not allowed to interact with those processes, so it must not be able
to read them through a screenshot either.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

from app.agent.controller_policy import is_blocked_process_alias

# Title fallback for windows whose owning process cannot be inspected
# (elevated shells surface with an empty process name).
_BLOCKED_TITLE_PATTERNS = ("command prompt", "powershell", "windows terminal")
_DWMWA_CLOAKED = 14

Rect = tuple[int, int, int, int]  # left, top, right, bottom


def _is_cloaked(hwnd: int) -> bool:
    try:
        cloaked = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        return bool(cloaked.value)
    except Exception:
        return False


def _is_blocked_window(win32gui, win32process, hwnd: int) -> bool:
    process_name = ""
    try:
        import psutil

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process_name = psutil.Process(pid).name() if pid else ""
    except Exception:
        process_name = ""
    if process_name:
        return is_blocked_process_alias(process_name)
    title = str(win32gui.GetWindowText(hwnd) or "").lower()
    return any(pattern in title for pattern in _BLOCKED_TITLE_PATTERNS)


def blocked_window_rects(*, above_hwnd: int = 0) -> list[Rect]:
    """Screen rects of visible blocked windows, top of the Z-order first.

    With ``above_hwnd`` only windows stacked above that window count, because
    the capture is of that window's area and anything below it is hidden.
    """
    try:
        import win32gui
        import win32process
    except ImportError:
        return []

    ordered: list[int] = []

    def _collect(hwnd, _):
        ordered.append(int(hwnd))
        return True

    try:
        win32gui.EnumWindows(_collect, None)  # Z-order, topmost first
    except Exception:
        return []

    rects: list[Rect] = []
    for hwnd in ordered:
        if above_hwnd and hwnd == int(above_hwnd):
            break
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd) or _is_cloaked(hwnd):
                continue
            if not _is_blocked_window(win32gui, win32process, hwnd):
                continue
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            continue
        if right > left and bottom > top:
            rects.append((int(left), int(top), int(right), int(bottom)))
    return rects


def redact_capture(path: str | Path, *, origin_x: int, origin_y: int, above_hwnd: int = 0) -> int:
    """Paint blocked windows black in a saved capture; returns how many were hit."""
    rects = blocked_window_rects(above_hwnd=above_hwnd)
    if not rects:
        return 0
    from PIL import Image, ImageDraw

    redacted = 0
    with Image.open(path) as image:
        image.load()
        width, height = image.size
        draw = ImageDraw.Draw(image)
        for left, top, right, bottom in rects:
            box = (
                max(0, left - origin_x),
                max(0, top - origin_y),
                min(width, right - origin_x),
                min(height, bottom - origin_y),
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            draw.rectangle(box, fill=(0, 0, 0))
            redacted += 1
        if redacted:
            image.save(path)
    return redacted
