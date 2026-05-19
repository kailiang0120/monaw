from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from typing import Any

from app.agent.access_grant_broker import create_grant_ticket
from app.agent.audit import AuditLogger
from app.agent.approval_broker import create_ticket
from app.agent.execution_resume import register_executor
from app.agent.output_workspace import configured_screenshots_dir
from app.skills.computer_use.screen_geometry import get_screen_layout

_UNSAFE_ARG_CHARS = {"&", "|", ";", "\n", "\r", "`", "$", "(", ")"}
_audit = AuditLogger()


def _blocked_result(decision) -> str:
    return json.dumps(
        {
            "status": "blocked",
            "reason": decision.reason,
            "reason_code": decision.reason_code,
            "policy_source": decision.policy_source,
        }
    )


def _pending_approval_result(
    decision,
    action_desc: str,
    *,
    tool_name: str,
    action_type: str,
    target_app: str = "",
    payload_args: dict[str, Any] | None = None,
) -> str:
    args = payload_args or {}
    ticket = create_ticket(
        action_type=action_type,
        tool_name=tool_name,
        target_app=target_app,
        risk_level="medium",
        reason=decision.reason,
        action_description=action_desc,
        payload={"input_str": json.dumps(args, ensure_ascii=False), "args": args},
    )
    return json.dumps(
        {
            "status": "pending_approval",
            "ticket_id": ticket.id,
            "action": action_desc,
            "reason": decision.reason,
            "reason_code": decision.reason_code,
            "policy_source": decision.policy_source,
            "instruction": (
                "This action requires user approval. "
                "The loop will pause until the user decides; do not attempt further steps."
            ),
        }
    )


def _pending_access_grant_result(decision, *, alias: str, action_desc: str) -> str:
    ticket = create_grant_ticket(
        target_type="app",
        target_identifier=alias,
        display_name=alias,
        action_context=action_desc or decision.reason,
    )
    return json.dumps(
        {
            "status": "pending_access_grant",
            "ticket_id": ticket.id,
            "target_type": ticket.target_type,
            "target_identifier": ticket.target_identifier,
            "display_name": ticket.display_name,
            "action_context": ticket.action_context,
        }
    )


def _resolve_glob_path(pattern: str) -> str | None:
    import glob

    matches = glob.glob(pattern)
    return matches[0] if matches else None


def _validated_launch_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if any(char in value for char in ("\n", "\r", "\x00")):
        raise ValueError("url contains unsafe control characters.")

    from urllib.parse import urlparse

    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an absolute http(s) URL.")
    return value


def _settings_app_allowlist(*, launch_only: bool = False) -> dict[str, list[str]]:
    from app.agent.controller_policy import normalize_app_alias
    from app.agent.settings_store import load_agent_settings

    settings_data = load_agent_settings()
    allowlist: dict[str, list[str]] = {}
    for rule in settings_data.permissions.app_rules:
        if not rule.enabled:
            continue
        if launch_only and not rule.launch_allowed:
            continue
        alias = normalize_app_alias(rule.alias)
        if not alias:
            continue
        allowlist[alias] = list(rule.exe_paths or [])
    return allowlist


def _available_app_aliases(*, launch_only: bool = False) -> list[str]:
    return sorted(_settings_app_allowlist(launch_only=launch_only).keys())


def _start_menu_shortcuts_for_alias(alias: str, *, limit: int = 5) -> list[dict[str, str]]:
    import glob

    alias_lower = alias.strip().lower()
    if not alias_lower:
        return []

    search_dirs = [
        os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
    ]
    matches: list[dict[str, str]] = []
    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        for lnk_path in glob.glob(os.path.join(search_dir, "**", "*.lnk"), recursive=True):
            shortcut_name = os.path.splitext(os.path.basename(lnk_path))[0]
            shortcut_lower = shortcut_name.lower()
            if alias_lower not in shortcut_lower and shortcut_lower not in alias_lower:
                continue
            matches.append(
                {
                    "kind": "shortcut",
                    "path": lnk_path,
                    "display_name": shortcut_name,
                    "source": "start_menu",
                }
            )
            if len(matches) >= limit:
                return matches
    return matches


def _resolve_launch_target(alias: str) -> dict[str, Any]:
    import shutil

    from app.agent.access_grant_broker import _discover_exe_for_alias
    from app.agent.controller_policy import normalize_app_alias

    normalized_alias = normalize_app_alias(alias)
    allowlist = _settings_app_allowlist(launch_only=True)
    configured_candidates = allowlist.get(normalized_alias, [])
    attempts: list[dict[str, str]] = []

    for candidate in configured_candidates:
        resolved = _resolve_glob_path(candidate) if "*" in candidate else candidate
        if resolved and os.path.isfile(resolved):
            kind = "shortcut" if resolved.lower().endswith(".lnk") else "executable"
            return {
                "found": True,
                "kind": kind,
                "path": resolved,
                "source": "settings",
                "configured_candidates": configured_candidates,
                "attempts": attempts,
            }
        attempts.append({"source": "settings", "candidate": candidate, "status": "missing"})

    for candidate in configured_candidates:
        basename = os.path.basename(candidate)
        if os.path.sep not in candidate or candidate == basename:
            return {
                "found": True,
                "kind": "path_command",
                "path": basename,
                "source": "settings",
                "configured_candidates": configured_candidates,
                "attempts": attempts,
            }

    found = shutil.which(normalized_alias) or shutil.which(f"{normalized_alias}.exe")
    if found:
        return {
            "found": True,
            "kind": "executable",
            "path": found,
            "source": "path",
            "configured_candidates": configured_candidates,
            "attempts": attempts,
        }

    discovered = _discover_exe_for_alias(alias)
    if discovered:
        return {
            "found": True,
            "kind": "executable",
            "path": discovered,
            "source": "discovery",
            "configured_candidates": configured_candidates,
            "attempts": attempts,
        }

    shortcuts = _start_menu_shortcuts_for_alias(alias)
    if shortcuts:
        target = shortcuts[0]
        return {
            "found": True,
            "kind": target["kind"],
            "path": target["path"],
            "source": target["source"],
            "display_name": target["display_name"],
            "configured_candidates": configured_candidates,
            "shortcut_matches": shortcuts,
            "attempts": attempts,
        }

    return {
        "found": False,
        "kind": "",
        "path": "",
        "source": "",
        "configured_candidates": configured_candidates,
        "attempts": attempts,
        "shortcut_matches": [],
    }


def _find_exe(alias: str) -> str | None:
    resolved = _resolve_launch_target(alias)
    return str(resolved.get("path") or "") if resolved.get("found") else None


def diagnose_app_launch(alias: str) -> str:
    from app.agent.controller_policy import (
        ActionType,
        is_blocked_process_alias,
        normalize_app_alias,
        resolve_permission,
    )

    normalized_alias = normalize_app_alias(alias)
    if not normalized_alias:
        return json.dumps({"status": "error", "error": "Provide an application alias."})

    decision = resolve_permission(ActionType.LAUNCH_APP, target_app=normalized_alias)
    target = _resolve_launch_target(normalized_alias)
    target_path = str(target.get("path") or "")
    blocked_target = bool(target_path and is_blocked_process_alias(target_path))
    available = _available_app_aliases(launch_only=True)
    return json.dumps(
        {
            "status": "ok",
            "alias": normalized_alias,
            "policy": {
                "blocked": decision.blocked,
                "requires_access_grant": decision.requires_access_grant,
                "requires_confirmation": decision.requires_confirmation,
                "reason": decision.reason,
                "reason_code": decision.reason_code,
                "policy_source": decision.policy_source,
            },
            "target": target,
            "blocked_target": blocked_target,
            "configured_app_rules": available,
            "next_step": (
                "Grant access to this app before launching."
                if decision.requires_access_grant
                else "Fix or add an app path in Settings."
                if not target.get("found")
                else "Launch should be possible."
            ),
        },
        ensure_ascii=False,
    )


def _wait_for_launch_window(alias: str, pid: int = 0, *, timeout_seconds: float = 2.5) -> dict[str, Any]:
    try:
        import win32gui
        import win32process
    except ImportError:
        return {"status": "skipped", "reason": "pywin32_unavailable"}

    alias_lower = alias.lower()
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    last_windows: list[dict[str, Any]] = []
    while time.monotonic() <= deadline:
        windows: list[dict[str, Any]] = []

        def _enum_cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            meta = _window_meta(win32gui, win32process, hwnd)
            if not meta["title"].strip():
                return
            windows.append(meta)

        try:
            win32gui.EnumWindows(_enum_cb, None)
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        last_windows = windows

        if pid:
            for window in windows:
                if int(window.get("pid") or 0) == pid:
                    return {"status": "found", "window": window}
        for window in windows:
            title = str(window.get("title") or "").lower()
            process_name = str(window.get("process_name") or "").lower()
            if alias_lower in title or alias_lower in process_name:
                return {"status": "found", "window": window}
        time.sleep(0.25)

    return {
        "status": "not_found",
        "reason": "No matching visible window appeared before timeout.",
        "visible_window_count": len(last_windows),
    }


def _launch_resolved_target(target: dict[str, Any], extra_args: list[str], launch_url: str):
    target_path = str(target.get("path") or "")
    target_kind = str(target.get("kind") or "")
    if target_kind == "shortcut":
        if extra_args or launch_url:
            raise ValueError("Start Menu shortcut launch does not support url or extra args. Configure the executable path in Settings for that app.")
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise RuntimeError("Shortcut launching is only available on Windows.")
        startfile(target_path)
        return None, "startfile"

    command = [target_path, *extra_args]
    if launch_url:
        command.append(launch_url)
    proc = subprocess.Popen(
        command,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, "popen"


def launch_app(alias: str, url: str = "", args: str = "", *, _bypass_gate: bool = False) -> str:
    from app.agent.controller_policy import (
        ActionType,
        is_blocked_process_alias,
        normalize_app_alias,
        resolve_permission,
    )

    extra_args = [part.strip() for part in args.split(",") if part.strip()]
    alias = normalize_app_alias(alias)
    if not alias:
        return json.dumps(
            {"status": "error", "error": "Provide an application alias, e.g. 'outlook' or 'notepad'."}
        )

    if is_blocked_process_alias(alias):
        return json.dumps(
            {"status": "error", "error": f"'{alias}' is blocked for security reasons."}
        )

    try:
        launch_url = _validated_launch_url(url)
    except ValueError as exc:
        return json.dumps({"status": "error", "error": str(exc)})

    for token in extra_args:
        if any(char in token for char in _UNSAFE_ARG_CHARS):
            return json.dumps(
                {"status": "error", "error": f"argument '{token}' contains unsafe characters."}
            )

    if not _bypass_gate:
        decision = resolve_permission(ActionType.LAUNCH_APP, target_app=alias)
        if decision.blocked:
            return _blocked_result(decision)
        if decision.requires_access_grant:
            return _pending_access_grant_result(
                decision,
                alias=alias,
                action_desc=f"Launch app: {alias}",
            )
        if decision.requires_confirmation:
            return _pending_approval_result(
                decision,
                f"Launch app: {alias}",
                tool_name="launch_app",
                action_type="launch_app",
                target_app=alias,
                payload_args={"alias": alias, "url": url, "args": args},
            )

    target = _resolve_launch_target(alias)
    if not target.get("found"):
        available = ", ".join(_available_app_aliases(launch_only=True)) or "(none configured in Settings)"
        return json.dumps(
            {
                "status": "error",
                "reason_code": "app_not_found",
                "error": (
                    f"Could not locate an installed executable for '{alias}'. "
                    f"Configured app rules: {available}"
                ),
                "diagnostics": target,
            }
        )

    target_path = str(target.get("path") or "")
    if is_blocked_process_alias(target_path):
        return json.dumps(
            {"status": "error", "error": f"'{alias}' resolves to a blocked process."}
        )

    try:
        proc, launch_method = _launch_resolved_target(target, extra_args, launch_url)
        pid = int(getattr(proc, "pid", 0) or 0)
        window_result = _wait_for_launch_window(alias, pid)
        _audit.log(
            "desktop.launch_app",
            data={
                "alias": alias,
                "pid": pid,
                "url": bool(launch_url),
                "source": target.get("source"),
                "kind": target.get("kind"),
                "window_status": window_result.get("status"),
            },
        )
        return json.dumps(
            {
                "status": "launched",
                "alias": alias,
                "pid": pid,
                "url": launch_url,
                "launch_method": launch_method,
                "launch_target": {
                    "kind": target.get("kind"),
                    "path": target.get("path"),
                    "source": target.get("source"),
                    "display_name": target.get("display_name", ""),
                },
                "window": window_result,
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "reason_code": "launch_failed",
                "error": f"Error launching '{alias}': {exc}",
                "launch_target": {
                    "kind": target.get("kind"),
                    "path": target.get("path"),
                    "source": target.get("source"),
                    "display_name": target.get("display_name", ""),
                },
            },
            ensure_ascii=False,
        )


def _resume_launch_app(input_str: str) -> str:
    try:
        payload = json.loads(input_str) if input_str else {}
    except json.JSONDecodeError:
        payload = {"alias": input_str}
    if not isinstance(payload, dict):
        payload = {"alias": str(payload)}
    return launch_app(
        str(payload.get("alias", "")),
        str(payload.get("url", "")),
        str(payload.get("args", "")),
        _bypass_gate=True,
    )


register_executor("launch_app", _resume_launch_app)


def screen_info() -> str:
    return json.dumps(get_screen_layout(), ensure_ascii=False)


def capture_full_screenshot(monitor: int = 0) -> str:
    try:
        import mss
        import mss.tools
    except ImportError:
        return json.dumps({"status": "error", "error": "'mss' package not installed. Run: pip install mss"})

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = configured_screenshots_dir() / f"screenshot_{timestamp}.png"
    try:
        with mss.mss() as sct:
            selected_monitor = sct.monitors[monitor]
            image = sct.grab(selected_monitor)
            mss.tools.to_png(image.rgb, image.size, output=str(filepath))
        size_kb = filepath.stat().st_size / 1024
        monitor_info = {
            "index": monitor,
            "x": int(selected_monitor["left"]),
            "y": int(selected_monitor["top"]),
            "width": int(selected_monitor["width"]),
            "height": int(selected_monitor["height"]),
        }
        _audit.log("desktop.screenshot", data={"path": str(filepath), "monitor": monitor_info})
        return json.dumps(
            {
                "status": "captured",
                "path": str(filepath),
                "size_kb": round(size_kb, 1),
                "resolution": f"{image.width}x{image.height}",
                "monitor": monitor_info,
                "region": {
                    "x": monitor_info["x"],
                    "y": monitor_info["y"],
                    "width": image.width,
                    "height": image.height,
                },
                "coordinate_origin": {"x": monitor_info["x"], "y": monitor_info["y"]},
                "coordinate_hint": (
                    "For screenshot pixels, absolute_x = coordinate_origin.x + image_x "
                    "and absolute_y = coordinate_origin.y + image_y."
                ),
            }
        )
    except Exception as exc:
        return json.dumps({"status": "error", "error": f"Error taking screenshot: {exc}"})


def _window_meta(win32gui, win32process, hwnd: int) -> dict[str, Any]:
    title = win32gui.GetWindowText(hwnd)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    process_name = ""
    try:
        import psutil

        process_name = psutil.Process(pid).name()
    except Exception:
        process_name = ""
    return {"hwnd": hwnd, "title": title, "pid": pid, "process_name": process_name}


def _select_visible_window(win32gui, win32process, *, title: str = "", hwnd: int = 0) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
    if hwnd:
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return None, f"No visible window with hwnd {hwnd}.", []
        return _window_meta(win32gui, win32process, hwnd), "", []

    query = title.strip().lower()
    if not query:
        return None, "Provide a window title or hwnd.", []

    matches: list[dict[str, Any]] = []

    def _enum_cb(candidate_hwnd, _):
        if not win32gui.IsWindowVisible(candidate_hwnd):
            return
        current_title = win32gui.GetWindowText(candidate_hwnd)
        if current_title.strip() and query in current_title.lower():
            matches.append(_window_meta(win32gui, win32process, candidate_hwnd))

    win32gui.EnumWindows(_enum_cb, None)
    if not matches:
        return None, f"No visible window matching '{title}'.", []

    exact = [item for item in matches if item["title"].lower() == query]
    if len(exact) == 1:
        return exact[0], "", matches
    if len(matches) == 1:
        return matches[0], "", matches
    return None, f"Multiple visible windows matched '{title}'.", matches[:10]


def list_windows() -> str:
    try:
        import win32gui
        import win32process
    except ImportError:
        return json.dumps({"status": "error", "error": "'pywin32' package not installed."})

    windows: list[dict[str, Any]] = []

    def _enum_cb(hwnd, _results):
        if win32gui.IsWindowVisible(hwnd):
            meta = _window_meta(win32gui, win32process, hwnd)
            if meta["title"].strip():
                _results.append(meta)

    win32gui.EnumWindows(_enum_cb, windows)
    return json.dumps(windows[:30], ensure_ascii=False)


def focus_window(title: str = "", hwnd: int = 0, *, _bypass_gate: bool = False) -> str:
    try:
        import win32con
        import win32gui
        import win32process
    except ImportError:
        return json.dumps({"status": "error", "error": "'pywin32' package not installed."})

    selected, error, matches = _select_visible_window(win32gui, win32process, title=title, hwnd=int(hwnd or 0))
    if selected is None:
        payload: dict[str, Any] = {"status": "error", "error": error}
        if matches:
            payload["reason_code"] = "window_ambiguous"
            payload["matches"] = matches
        return json.dumps(payload, ensure_ascii=False)

    target_hwnd = int(selected["hwnd"])
    target_app = selected.get("process_name") or title
    if not _bypass_gate:
        from app.agent.controller_policy import ActionType, resolve_permission

        decision = resolve_permission(ActionType.CLICK, target_app=target_app)
        if decision.blocked:
            return _blocked_result(decision)
        if decision.requires_confirmation:
            return _pending_approval_result(
                decision,
                f"Focus window: {selected['title']}",
                tool_name="focus_window",
                action_type="click",
                target_app=target_app,
                payload_args={"title": title, "hwnd": target_hwnd},
            )

    try:
        win32gui.ShowWindow(target_hwnd, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(target_hwnd)
        except Exception:
            _force_foreground_window(win32gui, win32process, target_hwnd)
        actual_title = win32gui.GetWindowText(target_hwnd)
        _audit.log("desktop.focus_window", data={"title": actual_title, "hwnd": target_hwnd})
        selected["title"] = actual_title
        return json.dumps({"status": "focused", "title": actual_title, "hwnd": target_hwnd, "matched_window": selected}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"status": "error", "error": f"Error focusing window: {exc}"})


def _force_foreground_window(win32gui, win32process, hwnd: int) -> None:
    import win32api

    foreground = win32gui.GetForegroundWindow()
    current_thread_id = win32api.GetCurrentThreadId()
    target_thread_id, _ = win32process.GetWindowThreadProcessId(hwnd)
    foreground_thread_id, _ = win32process.GetWindowThreadProcessId(foreground) if foreground else (0, 0)

    attached_target = False
    attached_foreground = False
    try:
        if target_thread_id and target_thread_id != current_thread_id:
            attached_target = bool(win32process.AttachThreadInput(current_thread_id, target_thread_id, True))
        if foreground_thread_id and foreground_thread_id not in {current_thread_id, target_thread_id}:
            attached_foreground = bool(win32process.AttachThreadInput(current_thread_id, foreground_thread_id, True))
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    finally:
        if attached_foreground:
            win32process.AttachThreadInput(current_thread_id, foreground_thread_id, False)
        if attached_target:
            win32process.AttachThreadInput(current_thread_id, target_thread_id, False)


def _resume_focus_window(input_str: str) -> str:
    try:
        payload = json.loads(input_str) if input_str else {}
    except json.JSONDecodeError:
        payload = {"title": input_str}
    if not isinstance(payload, dict):
        payload = {"title": str(payload)}
    return focus_window(
        str(payload.get("title", "")),
        int(payload.get("hwnd") or 0),
        _bypass_gate=True,
    )


register_executor("focus_window", _resume_focus_window)
