from __future__ import annotations

import json

from app.agent.approval_broker import create_ticket
from app.agent.audit import AuditLogger
from app.agent.controller_policy import ActionType, resolve_permission
from app.agent.execution_resume import register_executor
from app.skills.computer_use import desktop_runtime, window_ops

_audit = AuditLogger()

_DESKTOP_OBSERVATION_METADATA = {
    "parallel_safe": False,
    "resource_locks": ["desktop"],
    "mutates_state": False,
    "observation": True,
    "repeat_safe": True,
    "risk_level": "low",
}

_DESKTOP_ACTION_METADATA = {
    "parallel_safe": False,
    "requires_foreground": True,
    "resource_locks": ["desktop"],
    "mutates_state": True,
    "risk_level": "medium",
}


def _launch_app(alias: str, url: str = "", args: str = "") -> str:
    return desktop_runtime.launch_app(alias, url, args)


def _diagnose_app_launch(alias: str) -> str:
    return desktop_runtime.diagnose_app_launch(alias)


def _screenshot(
    region: dict | None = None,
    mode: str = "full",
    monitor: int = 0,
    hwnd: int = 0,
    window_title: str = "",
) -> str:
    normalized_mode = str(mode or "full").strip().lower()
    if region:
        normalized_mode = "region"
    if normalized_mode == "window":
        return window_ops._ctrl_screenshot_window(title=window_title, hwnd=int(hwnd or 0))
    if normalized_mode == "region":
        region = region or {}
        return window_ops._ctrl_screenshot_region(
            int(region.get("x", 0)),
            int(region.get("y", 0)),
            int(region.get("width", 0)),
            int(region.get("height", 0)),
        )
    if normalized_mode not in {"full", "screen", "monitor"}:
        return json.dumps(
            {
                "status": "error",
                "error": "mode must be one of full, monitor, window, or region.",
                "supported_modes": ["full", "monitor", "window", "region"],
            }
        )
    return desktop_runtime.capture_full_screenshot(int(monitor or 0))


def _screen_info() -> str:
    return desktop_runtime.screen_info()


def _active_window() -> dict:
    try:
        import win32gui
        import win32process

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return {}
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return {"hwnd": int(hwnd), "title": title, "pid": int(pid)}
    except Exception:
        return {}


def _desktop_snapshot(
    include_screenshot: bool = True,
    include_uia: bool = True,
    window_title: str = "",
    hwnd: int = 0,
    app: str = "",
    limit: int = 100,
    screenshot_mode: str = "full",
    monitor: int = 0,
) -> str:
    payload: dict = {
        "status": "ok",
        "active_window": _active_window(),
        "windows": [],
        "monitors": [],
        "cursor": {},
        "screenshot": {},
        "uia": {},
        "next_actions": [],
    }
    try:
        screen = json.loads(desktop_runtime.screen_info())
        payload["monitors"] = screen.get("monitors", [])
        payload["cursor"] = screen.get("cursor", {})
        payload["virtual_screen"] = screen.get("virtual_screen", {})
    except Exception as exc:
        payload["screen_error"] = str(exc)
    try:
        windows_payload = json.loads(desktop_runtime.list_windows())
        if isinstance(windows_payload, list):
            payload["windows"] = windows_payload
        elif isinstance(windows_payload, dict):
            payload["windows"] = windows_payload.get("windows", [])
        else:
            payload["windows"] = []
    except Exception as exc:
        payload["windows_error"] = str(exc)
    if include_screenshot:
        try:
            payload["screenshot"] = json.loads(
                _screenshot(
                    mode=screenshot_mode,
                    monitor=monitor,
                    hwnd=hwnd,
                    window_title=window_title,
                )
            )
        except Exception as exc:
            payload["screenshot"] = {"status": "error", "error": str(exc)}
    if include_uia:
        uia_app = app or window_title
        if uia_app:
            try:
                payload["uia"] = json.loads(
                    _inspect_ui(
                        uia_app,
                        window_title=window_title,
                        limit=max(1, min(int(limit or 100), 200)),
                    )
                )
            except Exception as exc:
                payload["uia"] = {"status": "error", "error": str(exc)}
        else:
            payload["uia"] = {"status": "skipped", "reason": "Provide app or window_title to include UIA elements."}
    if hwnd:
        payload["next_actions"].append({"tool": "focus_window", "arguments": {"hwnd": int(hwnd)}})
    elif window_title:
        payload["next_actions"].append({"tool": "focus_window", "arguments": {"title": window_title}})
    payload["coordinate_hint"] = "Use precision_click with coordinate_mode='absolute' for screen coordinates, or prefer click_ui_element/type_ui_element when UIA elements are available."
    return json.dumps(payload, ensure_ascii=False)


def _window_action(
    title: str,
    action: str,
    x: int = 0,
    y: int = 0,
    width: int = 0,
    height: int = 0,
    hwnd: int = 0,
) -> str:
    return window_ops._ctrl_window_action(title, action, width, height, x, y, hwnd)


def _list_processes(sort_by: str = "memory", limit: int = 20) -> str:
    raw = window_ops._ctrl_list_processes("")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    processes = data.get("processes", [])
    reverse = sort_by != "name"
    key = "memory_mb" if sort_by == "memory" else "name"
    processes = sorted(processes, key=lambda item: item.get(key, 0), reverse=reverse)
    data["processes"] = processes[: max(1, min(limit, 100))]
    data["count"] = len(data["processes"])
    return json.dumps(data, ensure_ascii=False)


def _parse_json_object(input_str: str) -> dict:
    try:
        value = json.loads(input_str) if input_str else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _terminate_process(name_or_pid: str, force: bool = False) -> str:
    target = str(name_or_pid or "").strip()
    if not target:
        return json.dumps({"status": "error", "error": "name_or_pid is required"})

    try:
        import psutil

        target_pid = int(target) if target.isdigit() else None
        killed: list[dict] = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if target_pid is not None and proc.info["pid"] != target_pid:
                    continue
                if target_pid is None and target.lower() not in (proc.info["name"] or "").lower():
                    continue
                proc.kill() if force else proc.terminate()
                killed.append({"pid": proc.info["pid"], "name": proc.info["name"]})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        _audit.log(
            "desktop.kill_process",
            data={"target": target, "force": force, "count": len(killed)},
        )
        return json.dumps({"status": "ok", "killed": killed}, ensure_ascii=False)
    except ImportError:
        return json.dumps({
            "status": "error",
            "error": "psutil not installed. Run: pip install psutil",
        })
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)})


def _kill_process(name_or_pid: str, force: bool = False) -> str:
    force_value = _coerce_bool(force)
    decision = resolve_permission(ActionType.PROCESS_KILL)
    if decision.blocked:
        return json.dumps(
            {
                "status": "blocked",
                "reason": decision.reason,
                "reason_code": decision.reason_code,
                "policy_source": decision.policy_source,
            }
        )
    if decision.requires_confirmation:
        payload_args = {"name_or_pid": name_or_pid, "force": force_value}
        ticket = create_ticket(
            action_type="process_kill",
            tool_name="kill_process",
            target_app=name_or_pid,
            risk_level="high",
            reason=decision.reason,
            action_description=f"Kill process: {name_or_pid}",
            payload={
                "input_str": json.dumps(payload_args, ensure_ascii=False, sort_keys=True),
                "args": payload_args,
            },
        )
        return json.dumps(
            {
                "status": "pending_approval",
                "ticket_id": ticket.id,
                "action": f"Kill process: {name_or_pid}",
                "reason": decision.reason,
                "reason_code": decision.reason_code,
                "policy_source": decision.policy_source,
            }
        )

    return _terminate_process(name_or_pid, force_value)


def _resume_kill_process(input_str: str) -> str:
    args = _parse_json_object(input_str)
    target = args.get("name_or_pid", args.get("pid", ""))
    return _terminate_process(str(target), _coerce_bool(args.get("force", False)))


register_executor("kill_process", _resume_kill_process)


def _click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    if clicks >= 2:
        return window_ops._ctrl_double_click(x, y, button)
    return window_ops._screen_click(x, y, button)


def _precision_click(
    x: float,
    y: float,
    button: str = "left",
    clicks: int = 1,
    coordinate_mode: str = "absolute",
    origin_x: int = 0,
    origin_y: int = 0,
    width: int = 0,
    height: int = 0,
    monitor: int = 0,
    title: str = "",
    hwnd: int = 0,
    verify_bounds: bool = True,
) -> str:
    return window_ops._precision_click(
        x,
        y,
        button,
        clicks,
        coordinate_mode,
        origin_x,
        origin_y,
        width,
        height,
        monitor,
        title,
        hwnd,
        verify_bounds,
    )


def _inspect_ui(app: str, window_title: str = "", contains: str = "", control_type: str = "", limit: int = 80) -> str:
    return window_ops._uia_interact_app(
        app,
        "get_elements",
        {
            "window_title": window_title,
            "contains": contains,
            "control_type": control_type,
            "limit": limit,
        },
    )


def _click_ui_element(
    app: str,
    element: str = "",
    automation_id: str = "",
    control_type: str = "",
    occurrence: int = -1,
    window_title: str = "",
) -> str:
    params = {
        "element": element,
        "automation_id": automation_id,
        "control_type": control_type,
        "window_title": window_title,
    }
    if occurrence >= 0:
        params["occurrence"] = occurrence
    return window_ops._uia_interact_app(app, "click_element", params)


def _read_ui_element(
    app: str,
    element: str = "",
    automation_id: str = "",
    control_type: str = "",
    occurrence: int = -1,
    window_title: str = "",
) -> str:
    params = {
        "element": element,
        "automation_id": automation_id,
        "control_type": control_type,
        "window_title": window_title,
    }
    if occurrence >= 0:
        params["occurrence"] = occurrence
    return window_ops._uia_interact_app(app, "read_element", params)


def _type_ui_element(
    app: str,
    text: str,
    element: str = "",
    automation_id: str = "",
    control_type: str = "",
    occurrence: int = -1,
    window_title: str = "",
    clear: bool = True,
) -> str:
    params = {
        "element": element,
        "automation_id": automation_id,
        "control_type": control_type,
        "window_title": window_title,
        "text": text,
        "clear": clear,
    }
    if occurrence >= 0:
        params["occurrence"] = occurrence
    return window_ops._uia_interact_app(app, "type_element", params)


def _set_ui_value(
    app: str,
    text: str,
    element: str = "",
    automation_id: str = "",
    control_type: str = "",
    occurrence: int = -1,
    window_title: str = "",
) -> str:
    params = {
        "element": element,
        "automation_id": automation_id,
        "control_type": control_type,
        "window_title": window_title,
        "text": text,
        "clear": True,
    }
    if occurrence >= 0:
        params["occurrence"] = occurrence
    return window_ops._uia_interact_app(app, "set_value", params)


def _invoke_ui_element(
    app: str,
    element: str = "",
    automation_id: str = "",
    control_type: str = "",
    occurrence: int = -1,
    window_title: str = "",
) -> str:
    params = {
        "element": element,
        "automation_id": automation_id,
        "control_type": control_type,
        "window_title": window_title,
    }
    if occurrence >= 0:
        params["occurrence"] = occurrence
    return window_ops._uia_interact_app(app, "invoke_element", params)


def _select_ui_option(
    app: str,
    option: str,
    element: str = "",
    automation_id: str = "",
    control_type: str = "",
    occurrence: int = -1,
    window_title: str = "",
) -> str:
    params = {
        "element": element,
        "automation_id": automation_id,
        "control_type": control_type,
        "window_title": window_title,
        "option": option,
    }
    if occurrence >= 0:
        params["occurrence"] = occurrence
    return window_ops._uia_interact_app(app, "select_option", params)


def _wait_ui_element(
    app: str,
    element: str = "",
    automation_id: str = "",
    control_type: str = "",
    window_title: str = "",
    timeout_seconds: float = 5,
) -> str:
    return window_ops._uia_interact_app(
        app,
        "wait_element",
        {
            "element": element,
            "automation_id": automation_id,
            "control_type": control_type,
            "window_title": window_title,
            "timeout_seconds": timeout_seconds,
        },
    )


def _type_text(
    text: str,
    clear: bool = False,
    press_enter: bool = False,
    hwnd: int = 0,
    window_title: str = "",
) -> str:
    if clear:
        result = window_ops._ctrl_hotkey(
            "ctrl+a",
            target_hwnd=int(hwnd or 0),
            target_title=window_title,
        )
        if json.loads(result).get("status") != "ok":
            return result
        result = window_ops._ctrl_hotkey(
            "backspace",
            target_hwnd=int(hwnd or 0),
            target_title=window_title,
        )
        if json.loads(result).get("status") != "ok":
            return result
    result = window_ops._screen_type(
        text,
        target_hwnd=int(hwnd or 0),
        target_title=window_title,
    )
    if json.loads(result).get("status") != "ok":
        return result
    if press_enter:
        enter_result = window_ops._ctrl_hotkey(
            "enter",
            target_hwnd=int(hwnd or 0),
            target_title=window_title,
        )
        if json.loads(enter_result).get("status") != "ok":
            return enter_result
    return result


def _hotkey(keys: str, hwnd: int = 0, window_title: str = "") -> str:
    return window_ops._ctrl_hotkey(
        keys,
        target_hwnd=int(hwnd or 0),
        target_title=window_title,
    )


def _scroll(direction: str = "down", amount: int = 3, x: int = -1, y: int = -1) -> str:
    return window_ops._ctrl_scroll(direction=direction, clicks=amount, x=x, y=y)


def _drag(from_x: int, from_y: int, to_x: int, to_y: int) -> str:
    return window_ops._ctrl_drag(from_x, from_y, to_x, to_y)


def _desktop_recipe(app: str, action: str = "open", query: str = "") -> str:
    normalized_app = app.strip().lower()
    normalized_action = action.strip().lower()
    aliases = {
        "explorer": "explorer",
        "notepad": "notepad",
        "outlook": "outlook",
        "teams": "teams",
        "chrome": "chrome",
        "edge": "edge",
    }
    if normalized_app not in aliases:
        return json.dumps({"status": "error", "error": f"Unsupported recipe app: {app}", "supported_apps": sorted(aliases)})
    alias = aliases[normalized_app]
    if normalized_action == "open":
        return _launch_app(alias)
    if normalized_action == "snapshot":
        return _desktop_snapshot(include_screenshot=True, include_uia=True, app=alias, window_title=query or alias)
    if normalized_action == "inspect":
        return _inspect_ui(alias, window_title=query, limit=100)
    return json.dumps({"status": "error", "error": f"Unsupported recipe action: {action}", "supported_actions": ["open", "snapshot", "inspect"]})


def register_tools(registry, _settings=None) -> None:
    registry.extend(
        [
            {
                "name": "desktop_snapshot",
                "description": "Capture desktop context in one call: active window, visible windows, monitor/cursor geometry, optional screenshot, and optional UIA elements.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_screenshot": {"type": "boolean", "default": True},
                        "include_uia": {"type": "boolean", "default": True},
                        "window_title": {"type": "string", "default": ""},
                        "hwnd": {"type": "integer", "default": 0},
                        "app": {"type": "string", "default": ""},
                        "limit": {"type": "integer", "default": 100},
                        "screenshot_mode": {
                            "type": "string",
                            "enum": ["full", "monitor", "window", "region"],
                            "default": "full",
                        },
                        "monitor": {"type": "integer", "default": 0},
                    },
                    "required": [],
                },
                "callable": _desktop_snapshot,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_OBSERVATION_METADATA,
            },
            {
                "name": "desktop_recipe",
                "description": "Thin recipes for common apps using generic desktop primitives: open, snapshot, or inspect Explorer, Notepad, Outlook, Teams, Chrome, or Edge.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app": {"type": "string", "enum": ["explorer", "notepad", "outlook", "teams", "chrome", "edge"]},
                        "action": {"type": "string", "enum": ["open", "snapshot", "inspect"], "default": "open"},
                        "query": {"type": "string", "default": ""},
                    },
                    "required": ["app"],
                },
                "callable": _desktop_recipe,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "launch_app",
                "description": "Launch a Windows application. Unknown apps require an access grant unless full-access mode or an existing grant allows them.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string"},
                        "url": {"type": "string", "default": ""},
                        "args": {"type": "string", "default": ""},
                    },
                    "required": ["alias"],
                },
                "callable": _launch_app,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "diagnose_app_launch",
                "description": "Check how an app alias will be resolved before launching it, including permission status, configured app paths, Start Menu shortcut matches, and the next recommended fix.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {"type": "string"},
                    },
                    "required": ["alias"],
                },
                "callable": _diagnose_app_launch,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_OBSERVATION_METADATA,
            },
            {
                "name": "list_windows",
                "description": "List visible top-level windows.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": desktop_runtime.list_windows,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_OBSERVATION_METADATA,
            },
            {
                "name": "screen_info",
                "description": "Return monitor geometry, virtual screen origin, and current cursor position for precise desktop coordinates.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": _screen_info,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_OBSERVATION_METADATA,
            },
            {
                "name": "focus_window",
                "description": "Focus a window by title match or exact hwnd.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "default": ""},
                        "hwnd": {"type": "integer", "default": 0},
                    },
                    "required": [],
                },
                "callable": desktop_runtime.focus_window,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "window_action",
                "description": "Manage a window state or placement.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "default": ""},
                        "action": {
                            "type": "string",
                            "enum": [
                                "minimize",
                                "maximize",
                                "restore",
                                "close",
                                "snap_left",
                                "snap_right",
                                "resize",
                                "move",
                            ],
                        },
                        "x": {"type": "integer", "default": 0},
                        "y": {"type": "integer", "default": 0},
                        "width": {"type": "integer", "default": 0},
                        "height": {"type": "integer", "default": 0},
                        "hwnd": {"type": "integer", "default": 0},
                    },
                    "required": ["action"],
                },
                "callable": _window_action,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "list_processes",
                "description": "List running processes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sort_by": {"type": "string", "enum": ["memory", "name"], "default": "memory"},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": [],
                },
                "callable": _list_processes,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_OBSERVATION_METADATA,
            },
            {
                "name": "kill_process",
                "description": "Terminate a process by name or PID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name_or_pid": {"type": "string"},
                        "force": {"type": "boolean", "default": False},
                    },
                    "required": ["name_or_pid"],
                },
                "callable": _kill_process,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {**_DESKTOP_ACTION_METADATA, "risk_level": "high"},
            },
            {
                "name": "inspect_ui",
                "description": "Inspect accessible UI elements in an allowlisted Windows app. Returns labels, roles, bounds, and center screen coordinates; use before clicking Teams/contact/chat controls.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app": {"type": "string"},
                        "window_title": {"type": "string", "default": ""},
                        "contains": {"type": "string", "default": ""},
                        "control_type": {"type": "string", "default": ""},
                        "limit": {"type": "integer", "default": 80},
                    },
                    "required": ["app"],
                },
                "callable": _inspect_ui,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_OBSERVATION_METADATA,
            },
            {
                "name": "click_ui_element",
                "description": "Click an accessible UI element in an allowlisted Windows app by visible label, automation id, role, and optional occurrence. Prefer this over raw coordinates.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app": {"type": "string"},
                        "element": {"type": "string", "default": ""},
                        "automation_id": {"type": "string", "default": ""},
                        "control_type": {"type": "string", "default": ""},
                        "occurrence": {"type": "integer", "default": -1},
                        "window_title": {"type": "string", "default": ""},
                    },
                    "required": ["app"],
                },
                "callable": _click_ui_element,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "read_ui_element",
                "description": "Read an accessible UI element's metadata and value in a Windows app.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app": {"type": "string"},
                        "element": {"type": "string", "default": ""},
                        "automation_id": {"type": "string", "default": ""},
                        "control_type": {"type": "string", "default": ""},
                        "occurrence": {"type": "integer", "default": -1},
                        "window_title": {"type": "string", "default": ""},
                    },
                    "required": ["app"],
                },
                "callable": _read_ui_element,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_OBSERVATION_METADATA,
            },
            {
                "name": "type_ui_element",
                "description": "Type into an accessible UI element in a Windows app. Prefer this over raw keyboard typing when a UIA element can be identified.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app": {"type": "string"},
                        "text": {"type": "string"},
                        "element": {"type": "string", "default": ""},
                        "automation_id": {"type": "string", "default": ""},
                        "control_type": {"type": "string", "default": ""},
                        "occurrence": {"type": "integer", "default": -1},
                        "window_title": {"type": "string", "default": ""},
                        "clear": {"type": "boolean", "default": True},
                    },
                    "required": ["app", "text"],
                },
                "callable": _type_ui_element,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "set_ui_value",
                "description": "Set an accessible UI element value in a Windows app.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app": {"type": "string"},
                        "text": {"type": "string"},
                        "element": {"type": "string", "default": ""},
                        "automation_id": {"type": "string", "default": ""},
                        "control_type": {"type": "string", "default": ""},
                        "occurrence": {"type": "integer", "default": -1},
                        "window_title": {"type": "string", "default": ""},
                    },
                    "required": ["app", "text"],
                },
                "callable": _set_ui_value,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "invoke_ui_element",
                "description": "Invoke an accessible UI element in a Windows app.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app": {"type": "string"},
                        "element": {"type": "string", "default": ""},
                        "automation_id": {"type": "string", "default": ""},
                        "control_type": {"type": "string", "default": ""},
                        "occurrence": {"type": "integer", "default": -1},
                        "window_title": {"type": "string", "default": ""},
                    },
                    "required": ["app"],
                },
                "callable": _invoke_ui_element,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "select_ui_option",
                "description": "Select an option from an accessible UI element in a Windows app.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app": {"type": "string"},
                        "option": {"type": "string"},
                        "element": {"type": "string", "default": ""},
                        "automation_id": {"type": "string", "default": ""},
                        "control_type": {"type": "string", "default": ""},
                        "occurrence": {"type": "integer", "default": -1},
                        "window_title": {"type": "string", "default": ""},
                    },
                    "required": ["app", "option"],
                },
                "callable": _select_ui_option,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "wait_ui_element",
                "description": "Wait for an accessible UI element in a Windows app.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "app": {"type": "string"},
                        "element": {"type": "string", "default": ""},
                        "automation_id": {"type": "string", "default": ""},
                        "control_type": {"type": "string", "default": ""},
                        "window_title": {"type": "string", "default": ""},
                        "timeout_seconds": {"type": "number", "default": 5},
                    },
                    "required": ["app"],
                },
                "callable": _wait_ui_element,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_OBSERVATION_METADATA,
            },
            {
                "name": "click",
                "description": "Click screen coordinates.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "button": {"type": "string", "enum": ["left", "right"], "default": "left"},
                        "clicks": {"type": "integer", "default": 1},
                    },
                    "required": ["x", "y"],
                },
                "callable": _click,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "precision_click",
                "description": "Click a point after mapping screenshot, monitor, window, or normalized coordinates into absolute Windows screen coordinates.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "button": {"type": "string", "enum": ["left", "right"], "default": "left"},
                        "clicks": {"type": "integer", "default": 1},
                        "coordinate_mode": {
                            "type": "string",
                            "enum": [
                                "absolute",
                                "screenshot",
                                "normalized",
                                "monitor",
                                "monitor_normalized",
                                "window",
                                "window_normalized",
                            ],
                            "default": "absolute",
                        },
                        "origin_x": {"type": "integer", "default": 0},
                        "origin_y": {"type": "integer", "default": 0},
                        "width": {"type": "integer", "default": 0},
                        "height": {"type": "integer", "default": 0},
                        "monitor": {"type": "integer", "default": 0},
                        "title": {"type": "string", "default": ""},
                        "hwnd": {"type": "integer", "default": 0},
                        "verify_bounds": {"type": "boolean", "default": True},
                    },
                    "required": ["x", "y"],
                },
                "callable": _precision_click,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "type_text",
                "description": "Type text into the focused field or target window.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "clear": {"type": "boolean", "default": False},
                        "press_enter": {"type": "boolean", "default": False},
                        "hwnd": {
                            "type": "integer",
                            "default": 0,
                            "description": "Optional exact target window handle. Prefer this when known.",
                        },
                        "window_title": {
                            "type": "string",
                            "default": "",
                            "description": "Optional target window title for replay validation.",
                        },
                    },
                    "required": ["text"],
                },
                "callable": _type_text,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "hotkey",
                "description": "Send a keyboard shortcut to the current or specified target window.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keys": {"type": "string"},
                        "hwnd": {
                            "type": "integer",
                            "default": 0,
                            "description": "Optional exact target window handle. Prefer this when known.",
                        },
                        "window_title": {
                            "type": "string",
                            "default": "",
                            "description": "Optional target window title for replay validation.",
                        },
                    },
                    "required": ["keys"],
                },
                "callable": _hotkey,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "scroll",
                "description": "Scroll the mouse wheel, optionally first moving the cursor to the target pane coordinates.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": ["up", "down"], "default": "down"},
                        "amount": {"type": "integer", "default": 3},
                        "x": {"type": "integer", "default": -1},
                        "y": {"type": "integer", "default": -1},
                    },
                    "required": [],
                },
                "callable": _scroll,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "drag",
                "description": "Drag from one screen coordinate to another.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "from_x": {"type": "integer"},
                        "from_y": {"type": "integer"},
                        "to_x": {"type": "integer"},
                        "to_y": {"type": "integer"},
                    },
                    "required": ["from_x", "from_y", "to_x", "to_y"],
                },
                "callable": _drag,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
            {
                "name": "screenshot",
                "description": "Capture a full virtual-screen/monitor screenshot, a window screenshot, or an explicit region.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["full", "monitor", "window", "region"],
                            "default": "full",
                        },
                        "monitor": {"type": "integer", "default": 0},
                        "hwnd": {
                            "type": "integer",
                            "default": 0,
                            "description": "Exact window handle for mode='window'. Prefer this when known.",
                        },
                        "window_title": {
                            "type": "string",
                            "default": "",
                            "description": "Window title match for mode='window' when hwnd is unavailable.",
                        },
                        "region": {
                            "type": "object",
                            "default": {},
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "width": {"type": "integer"},
                                "height": {"type": "integer"},
                            },
                        }
                    },
                    "required": [],
                },
                "callable": _screenshot,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_OBSERVATION_METADATA,
            },
            {
                "name": "clipboard",
                "description": "Read, write, or clear the clipboard.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["read", "write", "clear"], "default": "read"},
                        "text": {"type": "string", "default": ""},
                    },
                    "required": ["action"],
                },
                "callable": window_ops._ctrl_clipboard,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _DESKTOP_ACTION_METADATA,
            },
        ]
    )
