from __future__ import annotations

import json
from typing import Any

from app.agent.access_grant_broker import create_grant_ticket
from app.agent.approval_broker import create_ticket
from app.agent.audit import AuditLogger
from app.agent.controller_policy import ActionType, is_blocked_process_alias, resolve_permission
from app.agent.execution_resume import register_executor
from app.agent.settings_store import load_agent_settings
from app.skills.computer_use import desktop_runtime, window_ops
from app.skills.computer_use.screen_geometry import get_window_rect
from app.skills.computer_use.session import get_state, store_state

_audit = AuditLogger()

_OBSERVATION_METADATA = {
    "parallel_safe": False,
    "resource_locks": ["desktop"],
    "mutates_state": False,
    "observation": True,
    "repeat_safe": True,
    "risk_level": "low",
}

_ACTION_METADATA = {
    "parallel_safe": False,
    "requires_foreground": True,
    "resource_locks": ["desktop"],
    "mutates_state": True,
    "risk_level": "medium",
}

_BLOCKED_SELF_OR_TERMINAL = {
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "windowsterminal.exe",
    "wt.exe",
    "codex.exe",
    "monaw.exe",
}


def _json_loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _active_window() -> dict[str, Any]:
    try:
        import win32gui
        import win32process

        hwnd = int(win32gui.GetForegroundWindow())
        if not hwnd:
            return {}
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return {"hwnd": hwnd, "title": title, "pid": int(pid)}
    except Exception:
        return {}


def _normalize_app_id(value: str) -> str:
    return str(value or "").strip().lower()


def _is_blocked_app(value: str) -> bool:
    normalized = _normalize_app_id(value)
    if not normalized:
        return False
    basename = normalized.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    return basename in _BLOCKED_SELF_OR_TERMINAL or is_blocked_process_alias(normalized)


def _window_from_rect_payload(payload: dict[str, Any]) -> dict[str, Any]:
    window = dict(payload.get("window") or payload)
    if "handle" in window and "hwnd" not in window:
        window["hwnd"] = window["handle"]
    if "app" not in window:
        window["app"] = window.get("process_name") or ""
    return window


def _list_visible_windows() -> list[dict[str, Any]]:
    windows = _json_loads(desktop_runtime.list_windows(), [])
    if isinstance(windows, dict):
        windows = windows.get("windows", [])
    return [dict(window) for window in windows if isinstance(window, dict)]


def _computer_functions_list_apps(include_windows: bool = True) -> str:
    apps: dict[str, dict[str, Any]] = {}
    try:
        settings = load_agent_settings()
        for rule in settings.permissions.app_rules:
            if not rule.enabled:
                continue
            app_id = _normalize_app_id(rule.alias)
            if not app_id or _is_blocked_app(app_id):
                continue
            apps[app_id] = {
                "id": app_id,
                "displayName": rule.display_name or rule.alias,
                "isRunning": False,
                "launch_allowed": bool(rule.launch_allowed),
                "uia_allowed": bool(rule.uia_allowed),
                "screen_fallback_allowed": bool(rule.screen_fallback_allowed),
                "windows": [],
            }
    except Exception:
        pass

    windows = _list_visible_windows() if include_windows else []
    for window in windows:
        app_id = _normalize_app_id(window.get("process_name") or window.get("title") or "unknown")
        if _is_blocked_app(app_id):
            continue
        app = apps.setdefault(
            app_id,
            {
                "id": app_id,
                "displayName": window.get("process_name") or app_id,
                "isRunning": True,
                "launch_allowed": False,
                "uia_allowed": True,
                "screen_fallback_allowed": False,
                "windows": [],
            },
        )
        app["isRunning"] = True
        app["windows"].append(_window_from_rect_payload(window))

    return json.dumps({"status": "ok", "apps": sorted(apps.values(), key=lambda item: item["id"])}, ensure_ascii=False)


def _computer_functions_get_window(
    hwnd: int = 0,
    title: str = "",
    app: str = "",
) -> str:
    if app and _is_blocked_app(app):
        return json.dumps({"status": "blocked", "reason_code": "blocked_app", "error": f"Blocked app target: {app}"})

    if hwnd:
        result = get_window_rect(hwnd=int(hwnd))
        if result.get("status") != "ok":
            return json.dumps(result, ensure_ascii=False)
        window = _window_from_rect_payload(result)
        process = str(window.get("process_name") or window.get("app") or "")
        if _is_blocked_app(process):
            return json.dumps(
                {
                    "status": "blocked",
                    "reason_code": "blocked_app",
                    "error": f"Blocked app target: {process}",
                },
                ensure_ascii=False,
            )
        return json.dumps({"status": "ok", "window": window}, ensure_ascii=False)

    query_title = str(title or "").strip().lower()
    query_app = _normalize_app_id(app)
    matches = []
    for window in _list_visible_windows():
        window_title = str(window.get("title") or "").lower()
        process = _normalize_app_id(window.get("process_name") or "")
        if query_title and query_title not in window_title:
            continue
        if query_app and query_app not in process and query_app not in window_title:
            continue
        if _is_blocked_app(process):
            continue
        matches.append(_window_from_rect_payload(window))

    if not matches:
        return json.dumps({"status": "error", "reason_code": "window_not_found", "error": "No matching visible window."})
    exact = [item for item in matches if query_title and str(item.get("title", "")).lower() == query_title]
    if len(exact) == 1:
        matches = exact
    if len(matches) > 1:
        return json.dumps({"status": "error", "reason_code": "window_ambiguous", "matches": matches[:10]}, ensure_ascii=False)
    return json.dumps({"status": "ok", "window": matches[0]}, ensure_ascii=False)


def _computer_functions_activate_window(window: dict | None = None, hwnd: int = 0, title: str = "") -> str:
    target = _resolve_window(window=window, hwnd=hwnd, title=title)
    if target.get("status") != "ok":
        return json.dumps(target, ensure_ascii=False)
    selected = target["window"]
    return desktop_runtime.focus_window(
        title=str(selected.get("title") or ""),
        hwnd=int(selected.get("hwnd") or 0),
    )


def _computer_functions_get_window_state(
    window: dict | None = None,
    hwnd: int = 0,
    title: str = "",
    app: str = "",
    include_screenshot: bool = True,
    include_text: bool = False,
    limit: int = 100,
) -> str:
    target = _resolve_window(window=window, hwnd=hwnd, title=title, app=app)
    if target.get("status") != "ok":
        return json.dumps(target, ensure_ascii=False)
    selected = target["window"]

    screenshot: dict[str, Any] = {"status": "skipped"}
    if include_screenshot:
        screenshot = _json_loads(
            window_ops._ctrl_screenshot_window(
                title=str(selected.get("title") or ""),
                hwnd=int(selected.get("hwnd") or 0),
            ),
            {"status": "error", "error": "Invalid screenshot response."},
        )

    uia: dict[str, Any] = {"status": "skipped"}
    if include_text:
        uia_app = app or selected.get("process_name") or selected.get("app") or selected.get("title") or ""
        uia = _json_loads(
            window_ops._uia_interact_app(
                str(uia_app),
                "get_elements",
                {
                    "window_title": selected.get("title") or "",
                    "limit": max(1, min(int(limit or 100), 200)),
                },
            ),
            {"status": "error", "error": "Invalid UIA response."},
        )

    screen = _json_loads(desktop_runtime.screen_info(), {})
    state_id = store_state(window=selected, screenshot=screenshot, uia=uia)
    return json.dumps(
        {
            "status": "ok",
            "state_id": state_id,
            "window": selected,
            "active_window": _active_window(),
            "screen": screen,
            "screenshot": screenshot,
            "uia": uia,
        },
        ensure_ascii=False,
    )


def _resolve_window(
    *,
    window: dict | None = None,
    hwnd: int = 0,
    title: str = "",
    app: str = "",
) -> dict[str, Any]:
    source = dict(window or {})
    target_hwnd = int(hwnd or source.get("hwnd") or source.get("handle") or 0)
    target_title = str(title or source.get("title") or "")
    target_app = str(app or source.get("app") or source.get("process_name") or "")
    raw = _computer_functions_get_window(hwnd=target_hwnd, title=target_title, app=target_app)
    payload = _json_loads(raw, {})
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return payload if isinstance(payload, dict) else {"status": "error", "error": "Invalid window response."}
    return payload


def _map_window_point(window: dict[str, Any], x: float, y: float) -> tuple[int, int]:
    rect = dict(window.get("rect") or {})
    return int(rect.get("x", 0)) + round(float(x)), int(rect.get("y", 0)) + round(float(y))


def _normalize_key_chord(keys: str) -> str:
    aliases = {
        "control_l": "ctrl",
        "control_r": "ctrl",
        "control": "ctrl",
        "shift_l": "shift",
        "shift_r": "shift",
        "alt_l": "alt",
        "alt_r": "alt",
        "return": "enter",
        "escape": "esc",
    }
    parts = [part.strip().lower() for part in str(keys or "").replace("+", " + ").split("+")]
    return "+".join(aliases.get(part, part) for part in parts if part)


def _element_params_from_state(state_id: str, element_index: int, window: dict[str, Any]) -> dict[str, Any] | dict[str, str]:
    state = get_state(state_id)
    if state is None:
        return {"status": "error", "reason_code": "stale_state", "error": "state_id is stale or unknown."}
    if int(state.window.get("hwnd") or 0) != int(window.get("hwnd") or 0):
        return {"status": "error", "reason_code": "window_mismatch", "error": "state_id belongs to a different window."}
    elements = state.uia.get("elements") if isinstance(state.uia, dict) else None
    if not isinstance(elements, list):
        return {"status": "error", "reason_code": "uia_state_missing", "error": "state_id has no UIA element list."}
    try:
        element = next(item for item in elements if int(item.get("index")) == int(element_index))
    except StopIteration:
        return {"status": "error", "reason_code": "element_not_found", "error": f"No element index {element_index} in state."}
    return {
        "element": element.get("name") or "",
        "automation_id": element.get("automation_id") or "",
        "control_type": element.get("control_type") or "",
        "window_title": window.get("title") or "",
    }


def _permission_for_batch(
    actions: list[dict[str, Any]],
    window: dict[str, Any] | None = None,
    *,
    target_app: str = "",
) -> dict[str, Any] | None:
    window = dict(window or {})
    target_app = str(target_app or window.get("process_name") or window.get("app") or window.get("title") or "")
    if _is_blocked_app(target_app):
        return {"status": "blocked", "reason_code": "blocked_app", "error": f"Blocked app target: {target_app}"}

    needed: list[ActionType] = []
    for action in actions:
        kind = str(action.get("type") or "").strip().lower()
        if kind in {"click", "scroll", "drag", "set_value", "select_option", "invoke", "secondary_action"}:
            needed.append(ActionType.CLICK)
        elif kind in {"type_text", "press_key"}:
            needed.append(ActionType.TYPE)
        elif kind == "launch_app":
            needed.append(ActionType.LAUNCH_APP)

    for action_type in dict.fromkeys(needed):
        decision = resolve_permission(action_type, target_app=target_app)
        if decision.blocked:
            return {
                "status": "blocked",
                "reason": decision.reason,
                "reason_code": decision.reason_code,
                "policy_source": decision.policy_source,
            }
        if decision.requires_access_grant:
            ticket = create_grant_ticket(
                target_type="app",
                target_identifier=target_app,
                display_name=target_app,
                action_context="Computer functions action batch",
            )
            return {
                "status": "pending_access_grant",
                "ticket_id": ticket.id,
                "target_type": ticket.target_type,
                "target_identifier": ticket.target_identifier,
                "display_name": ticket.display_name,
                "action_context": ticket.action_context,
            }
        if decision.requires_confirmation:
            args = {"window": window, "actions": actions}
            ticket = create_ticket(
                action_type="computer_functions_act",
                tool_name="computer_functions_act",
                target_app=target_app,
                risk_level="medium",
                reason=decision.reason,
                action_description=f"Computer action batch on {target_app}",
                payload={"input_str": json.dumps(args, ensure_ascii=False, sort_keys=True), "args": args},
            )
            return {
                "status": "pending_approval",
                "ticket_id": ticket.id,
                "action": f"Computer action batch on {target_app}",
                "reason": decision.reason,
                "reason_code": decision.reason_code,
                "policy_source": decision.policy_source,
            }
    return None


def _computer_functions_act(
    actions: list[dict[str, Any]],
    window: dict | None = None,
    state_id: str = "",
    hwnd: int = 0,
    title: str = "",
    app: str = "",
    _bypass_gate: bool = False,
) -> str:
    if not isinstance(actions, list) or not actions:
        return json.dumps({"status": "error", "error": "actions must be a non-empty array."})

    launch_only = all(str(action.get("type") or "").lower() == "launch_app" for action in actions)
    selected: dict[str, Any] = {}
    if state_id:
        state = get_state(state_id)
        if state is None:
            return json.dumps({"status": "error", "reason_code": "stale_state", "error": "state_id is stale or unknown."})
        selected = dict(state.window)
    elif not launch_only:
        target = _resolve_window(window=window, hwnd=hwnd, title=title, app=app)
        if target.get("status") != "ok":
            return json.dumps(target, ensure_ascii=False)
        selected = target["window"]

    if not _bypass_gate:
        launch_targets = [
            str(action.get("app") or action.get("alias") or "")
            for action in actions
            if str(action.get("type") or "").strip().lower() == "launch_app"
        ]
        batch_target = selected or {}
        target_app = ""
        if launch_only:
            target_app = launch_targets[0] if len(set(launch_targets)) == 1 else "multiple_apps"
        pending = _permission_for_batch(actions, batch_target, target_app=target_app)
        if pending is not None:
            return json.dumps(pending, ensure_ascii=False)

    if selected:
        focus_result = _json_loads(
            desktop_runtime.focus_window(hwnd=int(selected.get("hwnd") or 0), _bypass_gate=True),
            {},
        )
        if focus_result.get("status") not in {"focused", "ok"}:
            return json.dumps(focus_result or {"status": "error", "error": "Could not focus target window."}, ensure_ascii=False)

    results: list[dict[str, Any]] = []
    for index, action in enumerate(actions):
        kind = str(action.get("type") or "").strip().lower()
        if kind == "launch_app":
            alias = str(action.get("app") or action.get("alias") or "")
            if _is_blocked_app(alias):
                result = {"status": "blocked", "reason_code": "blocked_app", "error": f"Blocked app target: {alias}"}
            else:
                result = _json_loads(
                    desktop_runtime.launch_app(
                        alias,
                        str(action.get("url") or ""),
                        str(action.get("args") or ""),
                        _bypass_gate=_bypass_gate,
                    ),
                    {},
                )
        elif kind == "click":
            mode = str(action.get("coordinate_mode") or "window")
            result = _json_loads(
                window_ops._precision_click(
                    float(action.get("x", 0)),
                    float(action.get("y", 0)),
                    action.get("button", "left"),
                    int(action.get("clicks", 1)),
                    mode,
                    int(action.get("origin_x", 0)),
                    int(action.get("origin_y", 0)),
                    int(action.get("width", 0)),
                    int(action.get("height", 0)),
                    int(action.get("monitor", 0)),
                    str(selected.get("title") or ""),
                    int(selected.get("hwnd") or 0),
                    bool(action.get("verify_bounds", True)),
                    _bypass_gate=True,
                ),
                {},
            )
        elif kind == "type_text":
            if action.get("clear"):
                window_ops._ctrl_hotkey("ctrl+a", target_hwnd=int(selected.get("hwnd") or 0), _bypass_gate=True)
                window_ops._ctrl_hotkey("backspace", target_hwnd=int(selected.get("hwnd") or 0), _bypass_gate=True)
            result = _json_loads(
                window_ops._screen_type(
                    str(action.get("text") or ""),
                    target_hwnd=int(selected.get("hwnd") or 0),
                    target_title=str(selected.get("title") or ""),
                    target_process_name=str(selected.get("process_name") or ""),
                    _bypass_gate=True,
                ),
                {},
            )
        elif kind == "press_key":
            result = _json_loads(
                window_ops._ctrl_hotkey(
                    _normalize_key_chord(str(action.get("key") or action.get("keys") or "")),
                    target_hwnd=int(selected.get("hwnd") or 0),
                    target_title=str(selected.get("title") or ""),
                    target_process_name=str(selected.get("process_name") or ""),
                    _bypass_gate=True,
                ),
                {},
            )
        elif kind == "scroll":
            abs_x, abs_y = _map_window_point(selected, float(action.get("x", 0)), float(action.get("y", 0)))
            scroll_y = int(action.get("scrollY", action.get("scroll_y", 0)) or 0)
            direction = str(action.get("direction") or ("up" if scroll_y < 0 else "down"))
            amount = int(action.get("amount") or max(1, abs(scroll_y) // 120) or 3)
            result = _json_loads(window_ops._ctrl_scroll(direction, amount, abs_x, abs_y, _bypass_gate=True), {})
        elif kind == "drag":
            from_x, from_y = _map_window_point(selected, float(action.get("from_x", 0)), float(action.get("from_y", 0)))
            to_x, to_y = _map_window_point(selected, float(action.get("to_x", 0)), float(action.get("to_y", 0)))
            result = _json_loads(window_ops._ctrl_drag(from_x, from_y, to_x, to_y, action.get("button", "left"), _bypass_gate=True), {})
        elif kind in {"set_value", "select_option", "invoke", "secondary_action"}:
            params = _element_params_from_state(state_id, int(action.get("element_index", -1)), selected)
            if params.get("status") == "error":
                result = params
            else:
                if kind == "set_value":
                    params["text"] = str(action.get("value") or action.get("text") or "")
                    uia_action = "set_value"
                elif kind == "select_option":
                    params["option"] = str(action.get("option") or "")
                    uia_action = "select_option"
                else:
                    uia_action = "invoke_element"
                result = _json_loads(
                    window_ops._uia_interact_app(
                        str(selected.get("process_name") or selected.get("app") or selected.get("title") or ""),
                        uia_action,
                        params,
                        _bypass_gate=True,
                    ),
                    {},
                )
        else:
            result = {"status": "error", "reason_code": "unsupported_action", "error": f"Unsupported action type: {kind}"}

        result.setdefault("action_index", index)
        result.setdefault("action_type", kind)
        results.append(result)
        if result.get("status") in {"error", "blocked", "pending_approval", "pending_access_grant"}:
            return json.dumps({"status": result.get("status"), "window": selected, "results": results}, ensure_ascii=False)

    _audit.log("computer_functions.act", data={"count": len(actions), "hwnd": selected.get("hwnd")})
    return json.dumps({"status": "ok", "window": selected, "results": results}, ensure_ascii=False)


def _computer_functions_clipboard(action: str = "read", text: str = "") -> str:
    return window_ops._ctrl_clipboard(action=action, text=text)


def _computer_functions_list_processes(sort_by: str = "memory", limit: int = 20) -> str:
    data = _json_loads(window_ops._ctrl_list_processes(""), {})
    if not isinstance(data, dict) or "processes" not in data:
        return json.dumps(data, ensure_ascii=False)
    reverse = sort_by != "name"
    key = "memory_mb" if sort_by == "memory" else "name"
    data["processes"] = sorted(data.get("processes", []), key=lambda item: item.get(key, 0), reverse=reverse)[
        : max(1, min(int(limit or 20), 100))
    ]
    data["count"] = len(data["processes"])
    return json.dumps(data, ensure_ascii=False)


def _terminate_process(name_or_pid: str, force: bool = False) -> str:
    target = str(name_or_pid or "").strip()
    if not target:
        return json.dumps({"status": "error", "error": "name_or_pid is required"})
    if _is_blocked_app(target):
        return json.dumps({"status": "blocked", "reason_code": "blocked_app", "error": f"Blocked process target: {target}"})
    try:
        import psutil

        target_pid = int(target) if target.isdigit() else None
        killed: list[dict[str, Any]] = []
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
        _audit.log("computer_functions.kill_process", data={"target": target, "force": force, "count": len(killed)})
        return json.dumps({"status": "ok", "killed": killed}, ensure_ascii=False)
    except ImportError:
        return json.dumps({"status": "error", "error": "psutil not installed. Run: pip install psutil"})
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)})


def _computer_functions_kill_process(name_or_pid: str, force: bool = False) -> str:
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
        args = {"name_or_pid": name_or_pid, "force": bool(force)}
        ticket = create_ticket(
            action_type="process_kill",
            tool_name="computer_functions_kill_process",
            target_app=name_or_pid,
            risk_level="high",
            reason=decision.reason,
            action_description=f"Kill process: {name_or_pid}",
            payload={"input_str": json.dumps(args, ensure_ascii=False, sort_keys=True), "args": args},
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
    return _terminate_process(name_or_pid, bool(force))


def _computer_functions_diagnose(app: str = "") -> str:
    payload = _json_loads(desktop_runtime.diagnose_app_launch(app), {})
    payload["registered_tools"] = [
        "computer_functions_list_apps",
        "computer_functions_get_window",
        "computer_functions_activate_window",
        "computer_functions_get_window_state",
        "computer_functions_act",
        "computer_functions_clipboard",
        "computer_functions_list_processes",
        "computer_functions_kill_process",
        "computer_functions_diagnose",
    ]
    return json.dumps(payload, ensure_ascii=False)


def _resume_computer_functions_act(input_str: str) -> str:
    args = _json_loads(input_str, {})
    return _computer_functions_act(
        actions=args.get("actions", []),
        window=args.get("window"),
        state_id=str(args.get("state_id", "")),
        hwnd=int(args.get("hwnd") or 0),
        title=str(args.get("title", "")),
        app=str(args.get("app", "")),
        _bypass_gate=True,
    )


def _resume_computer_functions_kill_process(input_str: str) -> str:
    args = _json_loads(input_str, {})
    return _terminate_process(str(args.get("name_or_pid", args.get("pid", ""))), bool(args.get("force", False)))


register_executor("computer_functions_act", _resume_computer_functions_act)
register_executor("computer_functions_kill_process", _resume_computer_functions_kill_process)


def register_tools(registry, _settings=None) -> None:
    registry.extend(
        [
            {
                "name": "computer_functions_list_apps",
                "description": "List configured and currently running Windows apps with targetable windows.",
                "parameters": {
                    "type": "object",
                    "properties": {"include_windows": {"type": "boolean", "default": True}},
                    "required": [],
                },
                "callable": _computer_functions_list_apps,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _OBSERVATION_METADATA,
            },
            {
                "name": "computer_functions_get_window",
                "description": "Resolve one visible Windows app window by hwnd, title, or app/process name.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "hwnd": {"type": "integer", "default": 0},
                        "title": {"type": "string", "default": ""},
                        "app": {"type": "string", "default": ""},
                    },
                    "required": [],
                },
                "callable": _computer_functions_get_window,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _OBSERVATION_METADATA,
            },
            {
                "name": "computer_functions_activate_window",
                "description": "Restore and focus a target Windows app window before interaction.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "window": {"type": "object", "default": {}},
                        "hwnd": {"type": "integer", "default": 0},
                        "title": {"type": "string", "default": ""},
                    },
                    "required": [],
                },
                "callable": _computer_functions_activate_window,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _ACTION_METADATA,
            },
            {
                "name": "computer_functions_get_window_state",
                "description": "Capture canonical window state, screenshot metadata, monitor geometry, active window, and optional UIA elements.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "window": {"type": "object", "default": {}},
                        "hwnd": {"type": "integer", "default": 0},
                        "title": {"type": "string", "default": ""},
                        "app": {"type": "string", "default": ""},
                        "include_screenshot": {"type": "boolean", "default": True},
                        "include_text": {"type": "boolean", "default": False},
                        "limit": {"type": "integer", "default": 100},
                    },
                    "required": [],
                },
                "callable": _computer_functions_get_window_state,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _OBSERVATION_METADATA,
            },
            {
                "name": "computer_functions_act",
                "description": "Run an ordered batch of Windows computer actions against one target window, or launch an app.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {"type": "array"},
                        "window": {"type": "object", "default": {}},
                        "state_id": {"type": "string", "default": ""},
                        "hwnd": {"type": "integer", "default": 0},
                        "title": {"type": "string", "default": ""},
                        "app": {"type": "string", "default": ""},
                    },
                    "required": ["actions"],
                },
                "callable": _computer_functions_act,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _ACTION_METADATA,
            },
            {
                "name": "computer_functions_clipboard",
                "description": "Read, write, or clear the Windows clipboard.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["read", "write", "clear"], "default": "read"},
                        "text": {"type": "string", "default": ""},
                    },
                    "required": ["action"],
                },
                "callable": _computer_functions_clipboard,
                "domain": "interaction",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _ACTION_METADATA,
            },
            {
                "name": "computer_functions_list_processes",
                "description": "List running Windows processes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sort_by": {"type": "string", "enum": ["memory", "name"], "default": "memory"},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": [],
                },
                "callable": _computer_functions_list_processes,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _OBSERVATION_METADATA,
            },
            {
                "name": "computer_functions_kill_process",
                "description": "Terminate a Windows process by name or PID after policy approval.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name_or_pid": {"type": "string"},
                        "force": {"type": "boolean", "default": False},
                    },
                    "required": ["name_or_pid"],
                },
                "callable": _computer_functions_kill_process,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {**_ACTION_METADATA, "risk_level": "high"},
            },
            {
                "name": "computer_functions_diagnose",
                "description": "Diagnose app launch policy, app resolution, and the registered computer function surface.",
                "parameters": {
                    "type": "object",
                    "properties": {"app": {"type": "string", "default": ""}},
                    "required": [],
                },
                "callable": _computer_functions_diagnose,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": _OBSERVATION_METADATA,
            },
        ]
    )
