from __future__ import annotations

import json
import os
import math
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.agent.access_grant_broker import create_grant_ticket
from app.agent.approval_broker import create_ticket
from app.agent.audit import AuditLogger
from app.agent.controller_policy import (
    ActionType,
    is_blocked_process_alias,
    is_screen_fallback_allowed,
    resolve_permission,
)
from app.agent.execution_resume import register_executor
from app.agent.tool_cancellation import check_cancelled, cancellation_requested, cancellable_sleep, ToolCancelled
from app.agent.conversation_workspace import workspace_path
from app.agent.run_context import current_interactive
from app.agent.settings_store import load_agent_settings
from app.skills.computer_use import desktop_runtime, window_ops
from app.skills.computer_use.screen_geometry import get_window_rect
from app.skills.computer_use.session import get_state, store_state, consume_state, invalidate_window, ComputerWindowState
from app.skills.computer_use.input_safety import text_entry_error, MAX_TYPED_TEXT_CHARS
from app.skills.computer_use.observation import window_changed, uia_fingerprint, image_sample, image_changed

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

# Action kinds that _computer_functions_act dispatches through the screen-coordinate
# primitives (precision_click/screen_type/ctrl_hotkey/ctrl_scroll/ctrl_drag). These
# are gated by the screen-fallback policy. Secondary action targets a UIA
# element but sends a right click, so it needs both UIA and screen permission.
_SCREEN_FALLBACK_ACTIONS = {
    "click",
    "type_text",
    "press_key",
    "scroll",
    "drag",
    "secondary_action",
}
_MAX_ACTIONS_PER_BATCH = 20
_MAX_TYPED_TEXT_CHARS = MAX_TYPED_TEXT_CHARS


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
    # controller_policy.BLOCKED_PROCESSES (via is_blocked_process_alias) is the single
    # source of truth for blocked terminals/self processes; it handles basename and
    # .exe-suffix matching internally.
    normalized = _normalize_app_id(value)
    if not normalized:
        return False
    return is_blocked_process_alias(normalized)


# Title substrings that identify a blocked terminal/shell window when its owning
# process name is unavailable (e.g. elevated/admin windows psutil cannot introspect,
# which surface with process_name == "").
_BLOCKED_TITLE_PATTERNS = (
    "command prompt",
    "powershell",
    "windows terminal",
)


def _is_blocked_window(process_name: str, title: str = "") -> bool:
    """Block by process name, falling back to terminal title patterns when the
    process name could not be resolved, so an elevated shell window is not
    surfaced as a targetable app/window."""
    if _is_blocked_app(process_name):
        return True
    if not _normalize_app_id(process_name):
        lowered = str(title or "").lower()
        return any(pattern in lowered for pattern in _BLOCKED_TITLE_PATTERNS)
    return False


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
        if _is_blocked_window(str(window.get("process_name") or ""), str(window.get("title") or "")):
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
        if _is_blocked_window(process, str(window.get("title") or "")):
            return json.dumps(
                {
                    "status": "blocked",
                    "reason_code": "blocked_app",
                    "error": f"Blocked app target: {process or window.get('title') or ''}",
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
        if _is_blocked_window(process, str(window.get("title") or "")):
            continue
        matches.append(_window_from_rect_payload(window))

    if not matches:
        return json.dumps({"status": "error", "reason_code": "window_not_found", "error": "No matching visible window."})
    exact = [item for item in matches if query_title and str(item.get("title", "")).lower() == query_title]
    if len(exact) == 1:
        matches = exact
    if len(matches) > 1:
        return json.dumps({"status": "error", "reason_code": "window_ambiguous", "matches": matches[:10]}, ensure_ascii=False)
    # Discovery only returns titles/processes. Hydrate the exact HWND so all
    # callers receive canonical geometry, identity and display scaling.
    return _computer_functions_get_window(hwnd=int(matches[0]["hwnd"]))


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
                    "hwnd": int(selected.get("hwnd") or 0),
                    "limit": max(1, min(int(limit or 100), 200)),
                },
            ),
            {"status": "error", "error": "Invalid UIA response."},
        )

    for observation in (screenshot, uia):
        if observation.get("status") in {"pending_approval", "pending_access_grant", "blocked", "cancelled"}:
            return json.dumps(observation, ensure_ascii=False)
    if include_text:
        uia["limit"] = max(1, min(int(limit or 100), 200))
    if screenshot.get("status") == "ok" and image_sample(screenshot) is None:
        screenshot = {"status": "error", "reason_code": "invalid_capture", "error": "The captured image could not be read."}
    if screenshot.get("status") != "ok" and uia.get("status") != "ok":
        return json.dumps({"status": "error", "reason_code": "observation_failed", "error": "No usable window observation was captured.", "screenshot": screenshot, "uia": uia})
    current = _resolve_window(hwnd=int(selected.get("hwnd") or 0))
    if current.get("status") != "ok" or window_changed(selected, current.get("window") or {}):
        return json.dumps({"status": "error", "reason_code": "stale_state", "error": "The window changed during capture. Capture a fresh state."})
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


def _action_batch_budget_error(actions: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(actions) > _MAX_ACTIONS_PER_BATCH:
        return {
            "status": "error",
            "reason_code": "action_batch_limit_exceeded",
            "error": "Computer action batch exceeds the maximum allowed action count.",
            "max_actions": _MAX_ACTIONS_PER_BATCH,
        }
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            return {"status": "error", "reason_code": "invalid_action", "error": "Every action must be an object.", "action_index": index}
        kind = str(action.get("type") or "").strip().lower()
        if kind not in _SCREEN_FALLBACK_ACTIONS | {"launch_app", "set_value", "select_option", "invoke", "secondary_action"}:
            return {"status": "error", "reason_code": "unsupported_action", "error": f"Unsupported action type: {kind}", "action_index": index}
        coordinate_keys = {"click": ("x", "y"), "drag": ("from_x", "from_y", "to_x", "to_y"),
                           "scroll": tuple(key for key in ("x", "y") if key in action)}.get(kind, ())
        for key in coordinate_keys:
            if key not in action or isinstance(action[key], bool) or not isinstance(action[key], (int, float)) or not math.isfinite(action[key]):
                return {"status": "error", "reason_code": "invalid_action", "error": f"A finite numeric {key} is required.", "action_index": index}
        if kind == "press_key" and not str(action.get("key") or action.get("keys") or "").strip():
            return {"status": "error", "reason_code": "invalid_action", "error": "A key chord is required.", "action_index": index}
        if kind in {"set_value", "select_option", "invoke", "secondary_action"} and (type(action.get("element_index")) is not int or action["element_index"] < 0):
            return {"status": "error", "reason_code": "invalid_action", "error": "A nonnegative integer element_index is required.", "action_index": index}
        if kind == "select_option" and not isinstance(action.get("option"), str):
            return {"status": "error", "reason_code": "invalid_action", "error": "A string option is required.", "action_index": index}
        expectation = action.get("expect")
        if expectation is not None:
            if not isinstance(expectation, dict) or expectation.get("type") not in {"file_exists", "window_title", "element_value", "selected_option"}:
                return {"status": "error", "reason_code": "invalid_expectation", "error": "Unsupported outcome check.", "action_index": index}
            required = ("path",) if expectation["type"] == "file_exists" else ("equals",)
            if any(not isinstance(expectation.get(key), str) for key in required):
                return {"status": "error", "reason_code": "invalid_expectation", "error": "The expected path or value must be a string.", "action_index": index}
            if expectation["type"] in {"element_value", "selected_option"} and (type(expectation.get("element_index")) is not int or expectation["element_index"] < 0):
                return {"status": "error", "reason_code": "invalid_expectation", "error": "Outcome checks need a nonnegative integer element_index.", "action_index": index}
            if expectation["type"] == "file_exists" and not expectation["path"].strip():
                return {"status": "error", "reason_code": "invalid_expectation", "error": "An expected file path is required.", "action_index": index}
        if kind not in {"type_text", "set_value"}:
            continue
        text = action.get("value", action.get("text", "")) if kind == "set_value" else action.get("text", "")
        if not isinstance(text, str):
            return {"status": "error", "reason_code": "invalid_action", "error": "Text entry requires a string.", "action_index": index}
        error = text_entry_error(text, action)
        if error:
            return {**error, "action_index": index}
    return None


def _active_window_matches(window: dict[str, Any]) -> bool:
    expected_hwnd = int(window.get("hwnd") or 0)
    if not expected_hwnd:
        return True
    active = _active_window()
    active_hwnd = int(active.get("hwnd") or 0)
    return bool(active_hwnd and active_hwnd == expected_hwnd)


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


def _element_params_from_state(state_id: str, element_index: int, window: dict[str, Any], *, state: ComputerWindowState | None = None) -> dict[str, Any]:
    state = state or get_state(state_id)
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
        "hwnd": int(window.get("hwnd") or 0),
        **{key: element[key] for key in ("runtime_id", "bounds") if key in element},
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
    state_id: str = "",
    skip_confirmation: bool = False,
) -> dict[str, Any] | None:
    window = dict(window or {})
    target_app = str(target_app or window.get("process_name") or window.get("app") or window.get("title") or "")
    if _is_blocked_app(target_app) or _is_blocked_window(
        str(window.get("process_name") or window.get("app") or ""), str(window.get("title") or "")
    ):
        return {"status": "blocked", "reason_code": "blocked_app", "error": f"Blocked app target: {target_app}"}

    # The act loop dispatches the screen-coordinate primitives with _bypass_gate=True,
    # so the screen-fallback guard those primitives enforce never runs there. Re-check
    # it here for any action that uses the screen-coordinate path.
    uses_screen_fallback = any(
        str(action.get("type") or "").strip().lower() in _SCREEN_FALLBACK_ACTIONS
        for action in actions
    )
    if uses_screen_fallback and not is_screen_fallback_allowed(target_app or None):
        return {
            "status": "blocked",
            "reason": "Screen fallback is disabled in settings.",
            "reason_code": "screen_fallback_disabled",
            "policy_source": "settings.json",
        }

    # Launches are checked against the app being launched, not the window the
    # batch targets; otherwise approving "a batch on notepad" launches anything.
    needed: list[tuple[ActionType, str]] = []
    launch_aliases: list[str] = []
    for action in actions:
        kind = str(action.get("type") or "").strip().lower()
        if kind in {"click", "scroll", "drag", "select_option", "invoke", "secondary_action"}:
            needed.append((ActionType.CLICK, target_app))
        elif kind in {"type_text", "set_value", "press_key"}:
            needed.append((ActionType.TYPE, target_app))
        elif kind == "launch_app":
            alias = str(action.get("app") or action.get("alias") or "")
            launch_aliases.append(alias)
            needed.append((ActionType.LAUNCH_APP, alias))

    # Evaluate every action before asking for confirmation: approval replays
    # the batch with the gate bypassed, so a hard block or access grant on a
    # later action must not hide behind an earlier confirmation prompt.
    confirmation = None
    for action_type, app_target in dict.fromkeys(needed):
        decision = resolve_permission(action_type, target_app=app_target)
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
                target_identifier=app_target,
                display_name=app_target,
                action_context="Computer functions action batch",
                requested_access="launch" if action_type == ActionType.LAUNCH_APP else "read",
            )
            return {
                "status": "pending_access_grant",
                "ticket_id": ticket.id,
                "target_type": ticket.target_type,
                "target_identifier": ticket.target_identifier,
                "display_name": ticket.display_name,
                "action_context": ticket.action_context,
            }
        if decision.requires_confirmation and confirmation is None:
            confirmation = decision

    if confirmation is not None and not skip_confirmation:
        args = {"window": window, "actions": actions, "state_id": state_id}
        description = f"Computer action batch on {target_app}"
        if launch_aliases:
            description += f" (launches: {', '.join(dict.fromkeys(launch_aliases))})"
        ticket = create_ticket(
            action_type="computer_functions_act",
            tool_name="computer_functions_act",
            target_app=target_app,
            risk_level="medium",
            reason=confirmation.reason,
            action_description=description,
            payload={"input_str": json.dumps(args, ensure_ascii=False, sort_keys=True), "args": args},
        )
        return {
            "status": "pending_approval",
            "ticket_id": ticket.id,
            "action": description,
            "reason": confirmation.reason,
            "reason_code": confirmation.reason_code,
            "policy_source": confirmation.policy_source,
        }
    return None


def _live_window_error(selected: dict, *, check_title: bool = True) -> dict | None:
    current = _resolve_window(hwnd=int(selected.get("hwnd") or 0))
    if current.get("status") != "ok":
        return current
    if window_changed(selected, current["window"], check_title=check_title):
        return {"status": "error", "reason_code": "stale_state",
                "error": "Window identity, bounds or display scaling changed. Capture a fresh state."}
    return None


def _validate_observation(state: ComputerWindowState, selected: dict) -> dict | None:
    if state.uia.get("status") == "ok":
        current = _json_loads(window_ops._uia_interact_app(
            str(selected.get("process_name") or selected.get("app") or selected.get("title") or ""),
            "get_elements", {"hwnd": int(selected["hwnd"]), "limit": int(state.uia.get("limit", 100))},
            _bypass_gate=True), {})
        if current.get("status") != "ok" or uia_fingerprint(current) != uia_fingerprint(state.uia):
            return {"status": "error", "reason_code": "stale_state", "error": "The accessible UI changed. Capture a fresh state."}
    if state.visual_sample is not None:
        current = _json_loads(window_ops._ctrl_screenshot_window(hwnd=int(selected["hwnd"])), {})
        sample = image_sample(current)
        if sample is None or image_changed(state.visual_sample, sample):
            return {"status": "error", "reason_code": "stale_state", "error": "The visible UI changed. Capture a fresh state."}
    return None


def _file_expectation_permission(expectation: dict) -> dict | None:
    expectation["path"] = str(workspace_path(expectation["path"]).resolve())
    decision = resolve_permission(ActionType.READ, target_path=expectation["path"])
    if decision.blocked:
        return {"status": "blocked", "reason_code": decision.reason_code, "reason": decision.reason}
    if decision.requires_access_grant:
        ticket = create_grant_ticket(target_type="path", target_identifier=expectation["path"],
                                     display_name=expectation["path"], action_context="Verify a desktop action's file output", requested_access="read")
        return {"status": "pending_access_grant", "ticket_id": ticket.id, "target_type": ticket.target_type,
                "target_identifier": ticket.target_identifier, "display_name": ticket.display_name, "action_context": ticket.action_context}
    if decision.requires_confirmation or not decision.allowed:
        return {"status": "blocked", "reason_code": "verification_access_required", "error": "Allow read access to the expected file before acting."}
    return None


def _verify_expected(expectation: dict, selected: dict, state: ComputerWindowState | None, state_id: str) -> dict:
    deadline = time.monotonic() + 1.0
    actual = None
    while True:
        check_cancelled()
        kind = expectation["type"]
        if kind == "file_exists":
            path = Path(expectation["path"]).resolve()
            permission = resolve_permission(ActionType.READ, target_path=str(path))
            if permission.blocked or permission.requires_access_grant or permission.requires_confirmation or not permission.allowed:
                return {"verified": False, "reason_code": "verification_access_required"}
            actual = path.is_file()
            matched = actual
        elif kind == "window_title":
            current = _resolve_window(hwnd=int(selected["hwnd"]))
            actual = (current.get("window") or {}).get("title")
            matched = current.get("status") == "ok" and actual == expectation["equals"]
        else:
            params = _element_params_from_state(state_id, int(expectation.get("element_index", -1)), selected, state=state)
            if params.get("status") in {"error", "blocked"}:
                return {"verified": False, "reason_code": params.get("reason_code", "verification_unavailable")}
            result = _json_loads(window_ops._uia_interact_app(
                str(selected.get("process_name") or selected.get("title") or ""), "read_element", params, _bypass_gate=True), {})
            if result.get("status") != "ok":
                return {"verified": False, "reason_code": result.get("reason_code", "verification_unavailable")}
            actual = result.get("selected") if kind == "selected_option" else result.get("value")
            matched = actual == expectation["equals"]
        if matched:
            return {"verified": True, "type": kind, "actual": actual}
        if actual is None:
            return {"verified": False, "reason_code": "verification_unavailable", "type": kind}
        if time.monotonic() >= deadline:
            return {"verified": False, "reason_code": "verification_failed", "type": kind, "actual": actual,
                    "instruction": "Inspect the resulting state before retrying; the action may have completed."}
        cancellable_sleep(0.05)


def _dispatch_batch_action(kind: str, action: dict, selected: dict, state_id: str,
                           state: ComputerWindowState | None, _bypass_gate: bool) -> dict:
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
        entry_error = window_ops._focused_text_entry_error(str(action.get("text", "")))
        if entry_error:
            return entry_error
        if action.get("clear"):
            for chord in ("ctrl+a", "backspace"):
                check_cancelled()
                cleared = _json_loads(window_ops._ctrl_hotkey(chord, target_hwnd=int(selected.get("hwnd") or 0), _restore_focus=False, _bypass_gate=True), {})
                if cleared.get("status") != "ok":
                    return cleared
        result = _json_loads(
            window_ops._screen_type(
                str(action.get("text") or ""),
                target_hwnd=int(selected.get("hwnd") or 0),
                target_title=str(selected.get("title") or ""),
                target_process_name=str(selected.get("process_name") or ""),
                _restore_focus=False,
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
                _restore_focus=False,
                _bypass_gate=True,
            ),
            {},
        )
    elif kind == "scroll":
        abs_x, abs_y = _map_window_point(selected, float(action.get("x", 0)), float(action.get("y", 0)))
        scroll_y = int(action.get("scrollY", action.get("scroll_y", 0)) or 0)
        direction = str(action.get("direction") or ("up" if scroll_y < 0 else "down"))
        amount = int(action.get("amount") or max(1, abs(scroll_y) // 120) or 3)
        result = _json_loads(window_ops._ctrl_scroll(direction, amount, abs_x, abs_y, target_hwnd=int(selected.get("hwnd") or 0), _bypass_gate=True), {})
    elif kind == "drag":
        from_x, from_y = _map_window_point(selected, float(action.get("from_x", 0)), float(action.get("from_y", 0)))
        to_x, to_y = _map_window_point(selected, float(action.get("to_x", 0)), float(action.get("to_y", 0)))
        result = _json_loads(window_ops._ctrl_drag(from_x, from_y, to_x, to_y, action.get("button", "left"), target_hwnd=int(selected.get("hwnd") or 0), _bypass_gate=True), {})
    elif kind in {"set_value", "select_option", "invoke", "secondary_action"}:
        params = _element_params_from_state(state_id, int(action.get("element_index", -1)), selected, state=state)
        if params.get("status") in {"error", "blocked"}:
            result = params
        else:
            if kind == "set_value":
                params["text"] = str(action.get("value", action.get("text", "")))
                uia_action = "set_value"
            elif kind == "select_option":
                params["option"] = str(action.get("option") or "")
                uia_action = "select_option"
            else:
                uia_action = "secondary_action" if kind == "secondary_action" else "invoke_element"
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


    return result if isinstance(result, dict) and result.get("status") else {
        "status": "error", "reason_code": "invalid_action_result", "error": "The desktop action returned no valid outcome."}


def _computer_functions_act(
    actions: list[dict[str, Any]], window: dict | None = None, state_id: str = "",
    hwnd: int = 0, title: str = "", app: str = "", _bypass_gate: bool = False,
) -> str:
    if not isinstance(actions, list) or not actions:
        return json.dumps({"status": "error", "error": "actions must be a non-empty array."})
    budget_error = _action_batch_budget_error(actions)
    if budget_error is not None:
        return json.dumps(budget_error, ensure_ascii=False)
    actions = deepcopy(actions)
    for action in actions:
        action["type"] = action["type"].strip().lower()
    results: list[dict] = []
    selected: dict = {}
    state = None
    started_input = False

    def finish(status: str, **details) -> str:
        payload = {"status": status, "window": selected, "results": results,
                   "completed_actions": sum(result.get("input_completed", result.get("status") in {"ok", "launched"}) for result in results), **details}
        if started_input and selected and status != "cancelled" and not cancellation_requested():
            try:
                after = _json_loads(_computer_functions_get_window_state(
                    hwnd=int(selected["hwnd"]), include_screenshot=True, include_text=True), {})
            except ToolCancelled:
                after = {"status": "cancelled", "reason_code": "user_stopped"}
                payload["status"] = "cancelled"
            except Exception as exc:
                after = {"status": "error", "reason_code": "observation_failed", "error": str(exc)}
            payload["after_state"] = after
            if after.get("status") == "ok":
                payload["state_id"] = after.get("state_id", "")
        payload["verified"] = payload["status"] == "ok" and bool(results) and all(result.get("verification", {}).get("verified") for result in results)
        if payload["status"] != "ok":
            payload["retryable"] = False
            payload["instruction"] = "Earlier actions may have completed. Inspect current state before continuing; do not replay the batch."
        return json.dumps(payload, ensure_ascii=False)

    try:
        check_cancelled()
        launch_only = all(str(action["type"]).strip().lower() == "launch_app" for action in actions)
        if state_id:
            state = get_state(state_id)
            if state is None:
                return finish("error", reason_code="stale_state", error="state_id is expired, used, or belongs to another chat/control session.")
            selected = dict(state.window)
            requested_hwnd = int(hwnd or (window or {}).get("hwnd") or (window or {}).get("handle") or 0)
            if requested_hwnd and requested_hwnd != int(selected.get("hwnd") or 0):
                return finish("error", reason_code="window_mismatch", error="The requested window differs from the captured state.")
            if (window and window_changed(window, selected)) or (title and title != selected.get("title")) or (app and _normalize_app_id(app) not in {_normalize_app_id(selected.get("app")), _normalize_app_id(selected.get("process_name"))}):
                return finish("error", reason_code="window_mismatch", error="The requested target differs from the captured state.")
        elif not launch_only:
            target = _resolve_window(window=window, hwnd=hwnd, title=title, app=app)
            if target.get("status") != "ok":
                return json.dumps(target, ensure_ascii=False)
            selected = target["window"]
            if window and window_changed(window, selected):
                return finish("error", reason_code="stale_state", error="The supplied window changed. Capture a fresh state.")
        for action in actions:
            if (action.get("expect") or {}).get("type") == "file_exists":
                pending = _file_expectation_permission(action["expect"])
                if pending:
                    return json.dumps(pending, ensure_ascii=False)
            if str(action["type"]).lower() in {"set_value", "select_option", "invoke", "secondary_action"}:
                params = _element_params_from_state(state_id, int(action.get("element_index", -1)), selected, state=state)
                if params.get("status") in {"error", "blocked"}:
                    return json.dumps(params, ensure_ascii=False)
            if (action.get("expect") or {}).get("type") in {"element_value", "selected_option"}:
                params = _element_params_from_state(state_id, action["expect"]["element_index"], selected, state=state)
                if params.get("status") in {"error", "blocked"}:
                    return json.dumps(params, ensure_ascii=False)
        launch_targets = [str(action.get("app") or action.get("alias") or "") for action in actions if action["type"] == "launch_app"]
        target_app = launch_targets[0] if launch_only and len(set(launch_targets)) == 1 else "multiple_apps" if launch_only else ""
        pending = _permission_for_batch(actions, selected, target_app=target_app, state_id=state_id, skip_confirmation=_bypass_gate)
        if pending:
            return finish(pending["status"], **{key: value for key, value in pending.items() if key != "status"})
        if not state_id and any(action["type"] in {"click", "scroll", "drag"} for action in actions):
            return finish("error", reason_code="state_required", error="Capture a fresh window state before coordinate input and pass its state_id.")
        if selected:
            error = _live_window_error(selected)
            if error:
                invalidate_window(int(selected["hwnd"]))
                return json.dumps(error, ensure_ascii=False)
            if state is not None:
                error = _validate_observation(state, selected)
                if error:
                    invalidate_window(int(selected["hwnd"]))
                    return json.dumps(error, ensure_ascii=False)
                state = consume_state(state_id)
                if state is None:
                    return finish("error", reason_code="stale_state", error="The state was invalidated before input.")
            else:
                invalidate_window(int(selected["hwnd"]))
            check_cancelled()
            focused = _json_loads(desktop_runtime.focus_window(hwnd=int(selected["hwnd"]), _bypass_gate=True), {})
            if focused.get("status") == "cancelled":
                return finish("cancelled", reason_code=focused.get("reason_code", "user_stopped"))
            if focused.get("status") not in {"focused", "ok"}:
                return finish("error", reason_code="target_window_not_focused", error=focused.get("error", "Could not focus target window."))
            if state is not None and state.visual_sample is not None:
                current_capture = _json_loads(window_ops._ctrl_screenshot_window(hwnd=int(selected["hwnd"])), {})
                focused_sample = image_sample(current_capture)
                if focused_sample is None or image_changed(state.visual_sample, focused_sample):
                    return finish("error", reason_code="stale_state", error="The visible target changed on activation. Inspect a fresh state before input.")
        for index, action in enumerate(actions):
            check_cancelled()
            kind = str(action["type"]).strip().lower()
            if selected and kind != "launch_app":
                error = _live_window_error(selected, check_title=index == 0)
                if error:
                    results.append({**error, "action_index": index, "action_type": kind})
                    return finish(error["status"], reason_code=error.get("reason_code"))
                if not _active_window_matches(selected):
                    results.append({"status": "blocked", "reason_code": "target_focus_changed", "action_index": index, "action_type": kind})
                    return finish("blocked", reason_code="target_focus_changed")
                if index and kind in {"click", "scroll", "drag"} and state is not None:
                    error = _validate_observation(state, selected)
                    if error:
                        results.append({**error, "action_index": index, "action_type": kind})
                        return finish("error", reason_code=error.get("reason_code"))
            started_input = True
            result = _dispatch_batch_action(kind, action, selected, state_id, state, _bypass_gate)
            result.update({"action_index": index, "action_type": kind})
            if result.get("status") in {"ok", "launched", "focused"}:
                result["input_completed"] = True
            result.setdefault("verification", {"verified": False, "status": "input_sent" if result.get("input_completed") else "unconfirmed",
                                               "instruction": "Verify the resulting state or supply an explicit expected outcome."})
            results.append(result)
            if result.get("status") not in {"ok", "launched", "focused"}:
                if result.get("status") in {"pending_approval", "pending_access_grant"}:
                    # Native dispatch may already have applied earlier inputs.
                    # A gate must never automatically replay this batch.
                    return finish("error", reason_code="batch_interrupted", error="Desktop permissions changed during the batch. Inspect current state before continuing.")
                return finish(str(result.get("status") or "error"), reason_code=result.get("reason_code", "desktop_action_failed"))
            if action.get("expect"):
                verification = _verify_expected(action["expect"], selected, state, state_id)
                result["verification"] = verification
                if not verification["verified"]:
                    result.update({"status": "error", "reason_code": verification["reason_code"]})
                    return finish("error", reason_code=verification["reason_code"])
        check_cancelled()
        _audit.log("computer_functions.act", data={"count": len(results), "hwnd": selected.get("hwnd")})
        return finish("ok")
    except ToolCancelled as exc:
        return finish("cancelled", reason_code=exc.reason_code, error=str(exc))
    except Exception as exc:
        return finish("error", reason_code="desktop_action_exception", error=str(exc))


def _computer_functions_clipboard(action: str = "read", text: str = "") -> str:
    if not current_interactive():
        return json.dumps(
            {
                "status": "blocked",
                "reason_code": "clipboard_non_interactive_restricted",
                "error": "Clipboard access requires an interactive desktop session.",
            },
            ensure_ascii=False,
        )
    if action in {"write", "clear"}:
        lowered = str(text or "").lower()
        if any(hint in lowered for hint in _CREDENTIAL_TEXT_HINTS):
            return json.dumps(
                {
                    "status": "blocked",
                    "reason_code": "credential_clipboard_restricted",
                    "error": "Credential-like clipboard writes require direct local user control.",
                },
                ensure_ascii=False,
            )
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


def _process_name_key(name: str) -> str:
    return os.path.basename(str(name or "").strip().lower()).removesuffix(".exe")


def _terminate_process(name_or_pid: str, force: bool = False) -> str:
    target = str(name_or_pid or "").strip()
    if not target:
        return json.dumps({"status": "error", "error": "name_or_pid is required"})
    if _is_blocked_app(target):
        return json.dumps({"status": "blocked", "reason_code": "blocked_app", "error": f"Blocked process target: {target}"})
    try:
        import psutil

        target_pid = int(target) if target.isdigit() else None
        target_key = _process_name_key(target)
        protected_pids = {os.getpid(), os.getppid()}
        killed: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pid = proc.info["pid"]
                name = proc.info["name"] or ""
                if target_pid is not None and pid != target_pid:
                    continue
                # Exact name match only: a substring match would let "power" or
                # "e" reach blocked processes such as powershell.exe or monaw.exe.
                if target_pid is None and _process_name_key(name) != target_key:
                    continue
                # A PID says nothing about what it points at, so check the real
                # process name against the blocked list before terminating.
                if _is_blocked_app(name) or pid in protected_pids:
                    refused.append({"pid": pid, "name": name, "reason_code": "blocked_app"})
                    continue
                proc.kill() if force else proc.terminate()
                killed.append({"pid": pid, "name": name})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        _audit.log(
            "computer_functions.kill_process",
            data={"target": target, "force": force, "count": len(killed), "refused": len(refused)},
        )
        if refused and not killed:
            return json.dumps(
                {
                    "status": "blocked",
                    "reason_code": "blocked_app",
                    "error": f"Blocked process target: {target}",
                    "refused": refused,
                },
                ensure_ascii=False,
            )
        return json.dumps({"status": "ok", "killed": killed, "refused": refused}, ensure_ascii=False)
    except ImportError:
        return json.dumps({
            "status": "error",
            "error": "psutil not installed. Run `uv sync --locked` from the backend directory.",
        })
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
                "description": "Run up to 20 ordered Windows actions. Pass a fresh state_id for pointer/UIA input; it is consumed by one batch. Changed targets stop input. Results distinguish input sent from verified outcomes and include after_state when available. Inspect partial failures before continuing; never replay them automatically.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array", "minItems": 1, "maxItems": _MAX_ACTIONS_PER_BATCH,
                            "items": {
                                "type": "object", "required": ["type"],
                                "properties": {
                                    "type": {"type": "string", "enum": ["launch_app", "click", "type_text", "press_key", "scroll", "drag", "set_value", "select_option", "invoke", "secondary_action"]},
                                    "x": {"type": "number"}, "y": {"type": "number"},
                                    "from_x": {"type": "number"}, "from_y": {"type": "number"},
                                    "to_x": {"type": "number"}, "to_y": {"type": "number"},
                                    "text": {"type": "string", "maxLength": MAX_TYPED_TEXT_CHARS},
                                    "value": {"type": "string", "maxLength": MAX_TYPED_TEXT_CHARS},
                                    "clear": {"type": "boolean"}, "key": {"type": "string"},
                                    "element_index": {"type": "integer", "minimum": 0},
                                    "option": {"type": "string"}, "app": {"type": "string"},
                                    "url": {"type": "string"}, "args": {"type": "string"},
                                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                                    "clicks": {"type": "integer", "minimum": 1, "maximum": 2},
                                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                                    "amount": {"type": "integer", "minimum": 1},
                                    "coordinate_mode": {"type": "string", "description": "Default window. Coordinates use physical pixels."},
                                    "expect": {
                                        "type": "object", "required": ["type"],
                                        "properties": {
                                            "type": {"type": "string", "enum": ["file_exists", "window_title", "element_value", "selected_option"]},
                                            "path": {"type": "string", "description": "Expected file; requires read access before input. Relative paths use the chat workspace."},
                                            "equals": {"type": "string"}, "element_index": {"type": "integer", "minimum": 0},
                                        },
                                    },
                                },
                            },
                        },
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
