from __future__ import annotations

import ctypes
import ctypes.wintypes
from typing import Any


def _get_system_metric(index: int) -> int:
    if not hasattr(ctypes, "windll"):
        return 0
    try:
        return int(ctypes.windll.user32.GetSystemMetrics(index))
    except Exception:
        return 0


def _virtual_screen_fallback() -> dict[str, int]:
    return {
        "x": _get_system_metric(76),  # SM_XVIRTUALSCREEN
        "y": _get_system_metric(77),  # SM_YVIRTUALSCREEN
        "width": _get_system_metric(78) or _get_system_metric(0),
        "height": _get_system_metric(79) or _get_system_metric(1),
    }


def get_cursor_position() -> dict[str, int]:
    if not hasattr(ctypes, "windll"):
        return {"x": 0, "y": 0}
    point = ctypes.wintypes.POINT()
    try:
        ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        return {"x": int(point.x), "y": int(point.y)}
    except Exception:
        return {"x": 0, "y": 0}


def get_screen_layout() -> dict[str, Any]:
    monitors: list[dict[str, int]] = []
    source = "win32"
    try:
        import mss

        with mss.mss() as sct:
            for index, monitor in enumerate(sct.monitors):
                monitors.append(
                    {
                        "index": index,
                        "x": int(monitor["left"]),
                        "y": int(monitor["top"]),
                        "width": int(monitor["width"]),
                        "height": int(monitor["height"]),
                    }
                )
        source = "mss"
    except Exception:
        monitors = []

    if monitors:
        virtual_screen = dict(monitors[0])
    else:
        virtual_screen = {"index": 0, **_virtual_screen_fallback()}
        monitors = [virtual_screen]

    return {
        "status": "ok",
        "coordinate_system": "absolute Windows virtual-screen coordinates",
        "source": source,
        "virtual_screen": virtual_screen,
        "monitors": monitors,
        "cursor": get_cursor_position(),
    }


def _rect_payload(left: int, top: int, right: int, bottom: int) -> dict[str, int]:
    return {
        "x": int(left),
        "y": int(top),
        "width": max(0, int(right) - int(left)),
        "height": max(0, int(bottom) - int(top)),
        "right": int(right),
        "bottom": int(bottom),
    }


def get_window_rect(title: str = "", hwnd: int = 0) -> dict[str, Any]:
    try:
        import win32gui
        import win32process
    except ImportError:
        return {"status": "error", "error": "'pywin32' package not installed."}

    target_hwnd = int(hwnd or 0)
    matches: list[dict[str, Any]] = []

    def _window_payload(candidate_hwnd: int) -> dict[str, Any]:
        window_title = win32gui.GetWindowText(candidate_hwnd)
        left, top, right, bottom = win32gui.GetWindowRect(candidate_hwnd)
        _, pid = win32process.GetWindowThreadProcessId(candidate_hwnd)
        process_name = ""
        try:
            import psutil

            process_name = psutil.Process(pid).name()
        except Exception:
            process_name = ""
        return {
            "hwnd": int(candidate_hwnd),
            "title": window_title,
            "pid": int(pid),
            "process_name": process_name,
            "rect": _rect_payload(left, top, right, bottom),
        }

    if target_hwnd:
        if not win32gui.IsWindow(target_hwnd) or not win32gui.IsWindowVisible(target_hwnd):
            return {"status": "error", "error": f"No visible window with hwnd {target_hwnd}."}
        return {"status": "ok", "window": _window_payload(target_hwnd), "matches": []}

    query = (title or "").strip().lower()
    if not query:
        return {"status": "error", "error": "Provide a window title or hwnd."}

    def _enum_cb(candidate_hwnd: int, _arg: object) -> None:
        if not win32gui.IsWindowVisible(candidate_hwnd):
            return
        window_title = win32gui.GetWindowText(candidate_hwnd)
        if window_title.strip() and query in window_title.lower():
            matches.append(_window_payload(candidate_hwnd))

    win32gui.EnumWindows(_enum_cb, None)
    if not matches:
        return {"status": "error", "error": f"No visible window matching '{title}'.", "matches": []}

    exact = [item for item in matches if item["title"].lower() == query]
    if len(exact) == 1:
        return {"status": "ok", "window": exact[0], "matches": matches[:10]}
    if len(matches) == 1:
        return {"status": "ok", "window": matches[0], "matches": matches}

    return {
        "status": "error",
        "error": f"Multiple visible windows matched '{title}'.",
        "reason_code": "window_ambiguous",
        "matches": matches[:10],
    }


def _monitor_rect(monitor_index: int) -> dict[str, int] | None:
    layout = get_screen_layout()
    for monitor in layout.get("monitors", []):
        if int(monitor.get("index", -1)) == int(monitor_index):
            return {
                "x": int(monitor["x"]),
                "y": int(monitor["y"]),
                "width": int(monitor["width"]),
                "height": int(monitor["height"]),
            }
    return None


def _validate_normalized(x: float, y: float) -> str | None:
    if not (0 <= x <= 1 and 0 <= y <= 1):
        return "normalized coordinates must be between 0 and 1."
    return None


def _validate_inside_bounds(point_x: int, point_y: int, bounds: dict[str, int]) -> str | None:
    width = int(bounds.get("width", 0))
    height = int(bounds.get("height", 0))
    if width <= 0 or height <= 0:
        return None
    left = int(bounds.get("x", 0))
    top = int(bounds.get("y", 0))
    if not (left <= point_x < left + width and top <= point_y < top + height):
        return f"mapped point ({point_x}, {point_y}) is outside bounds {bounds}."
    return None


def map_precision_coordinates(
    *,
    coordinate_mode: str,
    x: float,
    y: float,
    origin_x: int = 0,
    origin_y: int = 0,
    width: int = 0,
    height: int = 0,
    monitor: int = 0,
    title: str = "",
    hwnd: int = 0,
    verify_bounds: bool = True,
) -> dict[str, Any]:
    mode = (coordinate_mode or "absolute").strip().lower()
    aliases = {
        "screen": "absolute",
        "image": "screenshot",
        "region": "screenshot",
        "window_relative": "window",
        "monitor_relative": "monitor",
    }
    mode = aliases.get(mode, mode)

    source_point = {"x": x, "y": y}
    bounds: dict[str, int] = {}
    window: dict[str, Any] | None = None

    if mode == "absolute":
        abs_x = round(x)
        abs_y = round(y)
        virtual = get_screen_layout().get("virtual_screen", {})
        bounds = {
            "x": int(virtual.get("x", 0)),
            "y": int(virtual.get("y", 0)),
            "width": int(virtual.get("width", 0)),
            "height": int(virtual.get("height", 0)),
        }
    elif mode == "screenshot":
        bounds = {
            "x": int(origin_x),
            "y": int(origin_y),
            "width": int(width),
            "height": int(height),
        }
        if verify_bounds and width > 0 and height > 0 and not (0 <= x < width and 0 <= y < height):
            return {"status": "error", "error": "screenshot coordinates are outside the screenshot region."}
        abs_x = int(origin_x) + round(x)
        abs_y = int(origin_y) + round(y)
    elif mode == "normalized":
        error = _validate_normalized(float(x), float(y))
        if error:
            return {"status": "error", "error": error}
        if width <= 0 or height <= 0:
            return {"status": "error", "error": "width and height are required for normalized coordinates."}
        bounds = {
            "x": int(origin_x),
            "y": int(origin_y),
            "width": int(width),
            "height": int(height),
        }
        abs_x = int(origin_x) + round(float(x) * max(0, int(width) - 1))
        abs_y = int(origin_y) + round(float(y) * max(0, int(height) - 1))
    elif mode in {"monitor", "monitor_normalized"}:
        monitor_rect = _monitor_rect(int(monitor))
        if monitor_rect is None:
            return {"status": "error", "error": f"Monitor index {monitor} was not found."}
        bounds = monitor_rect
        if mode == "monitor_normalized":
            error = _validate_normalized(float(x), float(y))
            if error:
                return {"status": "error", "error": error}
            abs_x = bounds["x"] + round(float(x) * max(0, bounds["width"] - 1))
            abs_y = bounds["y"] + round(float(y) * max(0, bounds["height"] - 1))
        else:
            abs_x = bounds["x"] + round(x)
            abs_y = bounds["y"] + round(y)
    elif mode in {"window", "window_normalized"}:
        window_result = get_window_rect(title=title, hwnd=hwnd)
        if window_result.get("status") != "ok":
            return window_result
        window = window_result["window"]
        bounds = dict(window["rect"])
        if mode == "window_normalized":
            error = _validate_normalized(float(x), float(y))
            if error:
                return {"status": "error", "error": error}
            abs_x = bounds["x"] + round(float(x) * max(0, bounds["width"] - 1))
            abs_y = bounds["y"] + round(float(y) * max(0, bounds["height"] - 1))
        else:
            abs_x = bounds["x"] + round(x)
            abs_y = bounds["y"] + round(y)
    else:
        return {
            "status": "error",
            "error": (
                "coordinate_mode must be one of absolute, screenshot, normalized, "
                "monitor, monitor_normalized, window, window_normalized."
            ),
        }

    if verify_bounds:
        error = _validate_inside_bounds(abs_x, abs_y, bounds)
        if error:
            return {"status": "error", "error": error, "bounds": bounds}

    payload: dict[str, Any] = {
        "status": "ok",
        "coordinate_mode": mode,
        "source_point": source_point,
        "mapped": {"x": int(abs_x), "y": int(abs_y)},
        "bounds": bounds,
    }
    if window is not None:
        payload["window"] = window
    return payload
