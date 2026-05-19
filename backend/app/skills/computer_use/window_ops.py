"""Windows controller – cmd-first, UIA fallback, coordinate-click last resort.

Execution hierarchy:
  1. Command path (PowerShell/subprocess) – fastest, most reliable
  2. Explorer/UIA path – element-based automation via pywinauto
  3. Coordinate-click fallback – screen-click via SendInput (last resort)

All actions are policy-gated and audit-logged.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent.output_workspace import configured_screenshots_dir
from app.agent.controller_policy import (
    ActionType,
    PermissionDecision,
    PermissionMode,
    canonical,
    get_effective_app_rule,
    is_app_allowed,
    is_path_permitted,
    is_screen_fallback_allowed,
    load_policy,
    resolve_permission,
)
from app.agent.audit import AuditLogger
from app.agent.access_grant_broker import create_grant_ticket
from app.agent.approval_broker import create_ticket as _create_approval_ticket
from app.agent.execution_resume import register_executor
from app.skills.computer_use.screen_geometry import get_window_rect, map_precision_coordinates

_audit = AuditLogger()

_CTRL_V_KEY_DELAY_SECONDS = 0.05

# ── Helpers ───────────────────────────────────────────────────────────────────


def _safe_json(s: str) -> dict[str, Any]:
    try:
        if s.strip().startswith("{"):
            return json.loads(s)
    except json.JSONDecodeError:
        pass
    return {}


def _json_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _paste_text_via_clipboard(text: str) -> dict[str, Any]:
    """Paste text into the currently focused control and restore prior text clipboard content."""
    import win32api
    import win32clipboard
    import win32con

    had_clipboard_text = False
    previous_text = ""
    win32clipboard.OpenClipboard()
    try:
        formats: list[int] = []
        enum_format = 0
        while hasattr(win32clipboard, "EnumClipboardFormats"):
            enum_format = int(win32clipboard.EnumClipboardFormats(enum_format) or 0)
            if not enum_format:
                break
            formats.append(enum_format)
        text_formats = {
            getattr(win32con, "CF_TEXT", 1),
            getattr(win32con, "CF_OEMTEXT", 7),
            getattr(win32con, "CF_UNICODETEXT", 13),
            getattr(win32con, "CF_LOCALE", 16),
        }
        non_text_formats = [fmt for fmt in formats if fmt not in text_formats]
        if non_text_formats:
            return {
                "status": "error",
                "reason_code": "clipboard_non_text_present",
                "error": "Clipboard contains non-text data; refusing to replace it for paste typing.",
                "clipboard_formats": formats,
                "clipboard_restored": False,
            }
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            previous_text = str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or "")
            had_clipboard_text = True
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()

    ctrl_vk = getattr(win32con, "VK_CONTROL", 0x11)
    v_vk = win32api.VkKeyScan("v") & 0xFF
    win32api.keybd_event(ctrl_vk, 0, 0, 0)
    win32api.keybd_event(v_vk, 0, 0, 0)
    win32api.keybd_event(v_vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(ctrl_vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(_CTRL_V_KEY_DELAY_SECONDS)

    clipboard_restored = False
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        if had_clipboard_text:
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, previous_text)
        clipboard_restored = True
    finally:
        win32clipboard.CloseClipboard()

    return {
        "status": "ok",
        "typed_chars": len(text),
        "method": "paste",
        "clipboard_restored": clipboard_restored,
    }


def _type_text_via_keyboard(text: str) -> dict[str, Any]:
    """Type text through Win32 key events for controls that do not accept paste."""
    import win32api
    import win32con

    keyup = getattr(win32con, "KEYEVENTF_KEYUP", KEYEVENTF_KEYUP)
    modifier_keys = [
        (1, getattr(win32con, "VK_SHIFT", 0x10)),
        (2, getattr(win32con, "VK_CONTROL", 0x11)),
        (4, getattr(win32con, "VK_MENU", 0x12)),
    ]
    special_chars = {
        "\n": getattr(win32con, "VK_RETURN", 0x0D),
        "\r": getattr(win32con, "VK_RETURN", 0x0D),
        "\t": getattr(win32con, "VK_TAB", 0x09),
    }

    for index, char in enumerate(text):
        modifiers: list[int] = []
        if char in special_chars:
            code = special_chars[char]
        else:
            vk = int(win32api.VkKeyScan(char))
            if vk == -1:
                return {
                    "status": "error",
                    "reason_code": "unsupported_keyboard_character",
                    "error": f"Character at index {index} cannot be typed with the current keyboard layout.",
                    "typed_chars": index,
                }
            code = vk & 0xFF
            shift_state = (vk >> 8) & 0xFF
            modifiers = [modifier for bit, modifier in modifier_keys if shift_state & bit]
        for modifier in modifiers:
            win32api.keybd_event(modifier, 0, 0, 0)
        win32api.keybd_event(code, 0, 0, 0)
        win32api.keybd_event(code, 0, keyup, 0)
        for modifier in reversed(modifiers):
            win32api.keybd_event(modifier, 0, keyup, 0)
        time.sleep(0.01)

    return {"status": "ok", "typed_chars": len(text), "method": "type"}


def _gate(
    action: ActionType,
    *,
    target_path: str = "",
    target_app: str = "",
) -> PermissionDecision:
    """Run permission check and return decision."""
    return resolve_permission(action, target_path=target_path, target_app=target_app)


def _blocked_result(decision: PermissionDecision) -> str:
    return json.dumps({
        "status": "blocked",
        "reason": decision.reason,
        "reason_code": decision.reason_code,
        "policy_source": decision.policy_source,
    })


def _pending_approval_result(
    decision: PermissionDecision,
    action_desc: str,
    *,
    tool_name: str,
    action_type: str,
    target_path: str = "",
    target_app: str = "",
    input_str: str = "",
    payload_args: dict[str, Any] | None = None,
) -> str:
    """Create an approval ticket and return a pending_approval response."""
    ticket = _create_approval_ticket(
        action_type=action_type,
        tool_name=tool_name,
        target_path=target_path,
        target_app=target_app,
        risk_level="high" if action_type == "delete" else "medium",
        reason=decision.reason,
        action_description=action_desc,
        payload={"input_str": input_str, "args": payload_args or {}},
    )
    return json.dumps({
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
    })


def _pending_access_grant_result(
    decision: PermissionDecision,
    *,
    target_type: str,
    target_identifier: str,
    display_name: str,
    action_context: str,
) -> str:
    """Create an access-grant ticket and return pending_access_grant JSON.

    Policy gate order: blocked → requires_access_grant → requires_confirmation → execute.
    """
    ticket = create_grant_ticket(
        target_type=target_type,
        target_identifier=target_identifier,
        display_name=display_name or target_identifier,
        action_context=action_context or decision.reason,
    )
    return json.dumps({
        "status": "pending_access_grant",
        "ticket_id": ticket.id,
        "target_type": ticket.target_type,
        "target_identifier": ticket.target_identifier,
        "display_name": ticket.display_name,
        "action_context": ticket.action_context,
    })


def _permission_result(
    action: ActionType,
    action_desc: str,
    *,
    tool_name: str,
    action_type: str,
    target_path: str = "",
    target_app: str = "",
    input_str: str = "",
    payload_args: dict[str, Any] | None = None,
) -> str | None:
    decision = _gate(action, target_path=target_path, target_app=target_app)
    if decision.blocked:
        return _blocked_result(decision)
    if decision.requires_access_grant and target_app:
        return _pending_access_grant_result(
            decision,
            target_type="app",
            target_identifier=target_app,
            display_name=target_app,
            action_context=action_desc,
        )
    if decision.requires_access_grant and target_path:
        return _pending_access_grant_result(
            decision,
            target_type="path",
            target_identifier=target_path,
            display_name=target_path,
            action_context=action_desc,
        )
    if decision.requires_confirmation:
        return _pending_approval_result(
            decision,
            action_desc,
            tool_name=tool_name,
            action_type=action_type,
            target_path=target_path,
            target_app=target_app,
            input_str=input_str,
            payload_args=payload_args,
        )
    return None


# ── Strategy 1: Command path ─────────────────────────────────────────────────


def _cmd_open_folder(folder: str) -> str:
    """Open a folder in Explorer via subprocess."""
    dec = _gate(ActionType.READ, target_path=folder)
    if dec.blocked:
        return _blocked_result(dec)
    if dec.requires_access_grant:
        return _pending_access_grant_result(
            dec,
            target_type="path",
            target_identifier=folder,
            display_name=folder,
            action_context=f"Open folder: {folder}",
        )
    if dec.requires_confirmation:
        return _pending_approval_result(
            dec, f"Open folder: {folder}",
            tool_name="ctrl_open_folder", action_type="read",
            target_path=folder,
            input_str=json.dumps({"path": folder}),
        )

    canon = canonical(folder)
    if not os.path.isdir(canon):
        return json.dumps({"status": "error", "error": f"Not a directory: {canon}"})

    subprocess.Popen(
        ["explorer.exe", canon],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _audit.log("controller.cmd_open_folder", data={"path": canon})
    return json.dumps({"status": "ok", "path": canon, "method": "cmd"})


def _cmd_create_folder(folder: str, *, _bypass_gate: bool = False) -> str:
    """Create a folder via os.makedirs."""
    if not _bypass_gate:
        dec = _gate(ActionType.MUTATE, target_path=folder)
        if dec.blocked:
            return _blocked_result(dec)
        if dec.requires_access_grant:
            return _pending_access_grant_result(
                dec,
                target_type="path",
                target_identifier=folder,
                display_name=folder,
                action_context=f"Create folder: {folder}",
            )
        if dec.requires_confirmation:
            return _pending_approval_result(
                dec, f"Create folder: {folder}",
                tool_name="ctrl_create_folder", action_type="mutate",
                target_path=folder, input_str=json.dumps({"path": folder}),
            )

    canon = canonical(os.path.dirname(folder)) if not os.path.isdir(folder) else canonical(folder)
    target = canonical(folder) if os.path.isdir(folder) else folder
    # Re-validate the resolved target
    if not is_path_permitted(folder):
        return json.dumps({"status": "blocked", "reason": "Path is inside a blocked root."})

    os.makedirs(folder, exist_ok=True)
    _audit.log("controller.cmd_create_folder", data={"path": folder})
    return json.dumps({"status": "ok", "path": folder, "method": "cmd"})


def _cmd_move_item(source: str, destination: str, *, _bypass_gate: bool = False) -> str:
    """Move a file or folder via shutil."""
    if not _bypass_gate:
        dec = _gate(ActionType.MUTATE, target_path=source)
        if dec.blocked:
            return _blocked_result(dec)
        dec2 = _gate(ActionType.MUTATE, target_path=destination)
        if dec2.blocked:
            return _blocked_result(dec2)
        if dec.requires_access_grant:
            return _pending_access_grant_result(
                dec,
                target_type="path",
                target_identifier=source,
                display_name=source,
                action_context=f"Move (source): {source} → {destination}",
            )
        if dec2.requires_access_grant:
            return _pending_access_grant_result(
                dec2,
                target_type="path",
                target_identifier=destination,
                display_name=destination,
                action_context=f"Move (destination): {source} → {destination}",
            )
        if dec.requires_confirmation or dec2.requires_confirmation:
            return _pending_approval_result(
                dec, f"Move {source} -> {destination}",
                tool_name="ctrl_move", action_type="mutate",
                target_path=source,
                input_str=json.dumps({"source": source, "destination": destination}),
            )

    if not os.path.exists(source):
        return json.dumps({"status": "error", "error": f"Source not found: {source}"})

    dest_parent = os.path.dirname(destination)
    if dest_parent:
        os.makedirs(dest_parent, exist_ok=True)

    shutil.move(source, destination)
    _audit.log("controller.cmd_move", data={"source": source, "destination": destination})
    return json.dumps({"status": "ok", "source": source, "destination": destination, "method": "cmd"})


def _cmd_copy_item(source: str, destination: str, *, _bypass_gate: bool = False) -> str:
    """Copy a file or folder via shutil."""
    if not _bypass_gate:
        dec = _gate(ActionType.READ, target_path=source)
        if dec.blocked:
            return _blocked_result(dec)
        dec2 = _gate(ActionType.MUTATE, target_path=destination)
        if dec2.blocked:
            return _blocked_result(dec2)
        if dec.requires_access_grant:
            return _pending_access_grant_result(
                dec,
                target_type="path",
                target_identifier=source,
                display_name=source,
                action_context=f"Copy (source): {source} → {destination}",
            )
        if dec2.requires_access_grant:
            return _pending_access_grant_result(
                dec2,
                target_type="path",
                target_identifier=destination,
                display_name=destination,
                action_context=f"Copy (destination): {source} → {destination}",
            )
        if dec2.requires_confirmation:
            return _pending_approval_result(
                dec2, f"Copy {source} -> {destination}",
                tool_name="ctrl_copy", action_type="mutate",
                target_path=destination,
                input_str=json.dumps({"source": source, "destination": destination}),
            )

    if not os.path.exists(source):
        return json.dumps({"status": "error", "error": f"Source not found: {source}"})

    if os.path.isdir(source):
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination)

    _audit.log("controller.cmd_copy", data={"source": source, "destination": destination})
    return json.dumps({"status": "ok", "source": source, "destination": destination, "method": "cmd"})


def _cmd_rename_item(source: str, new_name: str, *, _bypass_gate: bool = False) -> str:
    """Rename a file or folder."""
    if not _bypass_gate:
        dec = _gate(ActionType.MUTATE, target_path=source)
        if dec.blocked:
            return _blocked_result(dec)
        if dec.requires_access_grant:
            return _pending_access_grant_result(
                dec,
                target_type="path",
                target_identifier=source,
                display_name=source,
                action_context=f"Rename: {source} → {new_name}",
            )
        if dec.requires_confirmation:
            return _pending_approval_result(
                dec, f"Rename {source} -> {new_name}",
                tool_name="ctrl_rename", action_type="mutate",
                target_path=source,
                input_str=json.dumps({"path": source, "new_name": new_name}),
            )

    if not os.path.exists(source):
        return json.dumps({"status": "error", "error": f"Not found: {source}"})

    parent = os.path.dirname(source)
    dest = os.path.join(parent, new_name)
    dec2 = _gate(ActionType.MUTATE, target_path=dest)
    if dec2.blocked:
        return _blocked_result(dec2)
    if dec2.requires_access_grant:
        return _pending_access_grant_result(
            dec2,
            target_type="path",
            target_identifier=dest,
            display_name=dest,
            action_context=f"Rename (target path): {source} → {new_name}",
        )

    os.rename(source, dest)
    _audit.log("controller.cmd_rename", data={"source": source, "destination": dest})
    return json.dumps({"status": "ok", "source": source, "new_path": dest, "method": "cmd"})


def _cmd_delete_item(target: str, *, _bypass_gate: bool = False) -> str:
    """Delete a file or folder. Unknown paths require access grant; permitted paths use approval flow."""
    if not _bypass_gate:
        dec = _gate(ActionType.DELETE, target_path=target)
        if dec.blocked:
            return _blocked_result(dec)
        if dec.requires_access_grant:
            return _pending_access_grant_result(
                dec,
                target_type="path",
                target_identifier=target,
                display_name=target,
                action_context=f"Delete: {target}",
            )
        if dec.requires_confirmation:
            return _pending_approval_result(
                dec, f"Delete: {target}",
                tool_name="ctrl_delete", action_type="delete",
                target_path=target,
                input_str=json.dumps({"path": target}),
            )

    if not os.path.exists(target):
        return json.dumps({"status": "error", "error": f"Not found: {target}"})

    if os.path.isdir(target):
        shutil.rmtree(target)
    else:
        os.remove(target)

    _audit.log("controller.cmd_delete", data={"path": target})
    return json.dumps({"status": "ok", "deleted": target, "method": "cmd"})


# ── Strategy 2: Explorer/UIA ─────────────────────────────────────────────────


def _uia_available() -> bool:
    """Check if pywinauto is importable."""
    try:
        import pywinauto  # noqa: F401
        return True
    except ImportError:
        return False


def _element_rect(element) -> dict[str, int] | None:
    try:
        rect = element.rectangle()
    except Exception:
        return None
    left = int(getattr(rect, "left", 0))
    top = int(getattr(rect, "top", 0))
    right = int(getattr(rect, "right", left))
    bottom = int(getattr(rect, "bottom", top))
    width = max(0, right - left)
    height = max(0, bottom - top)
    if width <= 0 or height <= 0:
        return None
    return {
        "x": left,
        "y": top,
        "width": width,
        "height": height,
        "right": right,
        "bottom": bottom,
        "center_x": left + width // 2,
        "center_y": top + height // 2,
    }


def _element_summary(element, index: int) -> dict[str, Any]:
    info = getattr(element, "element_info", None)
    try:
        name = element.window_text()
    except Exception:
        name = getattr(info, "name", "") if info else ""
    try:
        friendly_type = element.friendly_class_name()
    except Exception:
        friendly_type = ""
    automation_id = getattr(info, "automation_id", "") if info else ""
    control_type = getattr(info, "control_type", "") if info else ""
    class_name = getattr(info, "class_name", "") if info else ""
    payload: dict[str, Any] = {
        "index": index,
        "name": (name or "")[:120],
        "type": friendly_type or control_type,
        "control_type": control_type,
        "automation_id": automation_id,
        "class_name": class_name,
    }
    rect = _element_rect(element)
    if rect:
        payload["bounds"] = {key: rect[key] for key in ("x", "y", "width", "height", "right", "bottom")}
        payload["center"] = {"x": rect["center_x"], "y": rect["center_y"]}
    return payload


def _element_matches(element, *, name_query: str, automation_id: str, control_type: str) -> bool:
    summary = _element_summary(element, 0)
    if name_query and name_query.lower() not in str(summary.get("name", "")).lower():
        return False
    if automation_id and automation_id.lower() != str(summary.get("automation_id", "")).lower():
        return False
    if control_type:
        type_values = {
            str(summary.get("type", "")).lower(),
            str(summary.get("control_type", "")).lower(),
            str(summary.get("class_name", "")).lower(),
        }
        if control_type.lower() not in type_values:
            return False
    return True


def _uia_open_and_select(folder: str, filename: str) -> str:
    """Open Explorer at folder and select a file via UIA."""
    full_sel = os.path.join(folder, filename)
    dec = _gate(ActionType.READ, target_path=full_sel)
    if dec.blocked:
        return _blocked_result(dec)
    if dec.requires_access_grant:
        return _pending_access_grant_result(
            dec,
            target_type="path",
            target_identifier=full_sel,
            display_name=full_sel,
            action_context=f"Select in Explorer: {full_sel}",
        )

    if not _uia_available():
        return json.dumps({"status": "fallback_needed", "reason": "pywinauto not installed"})

    full_path = os.path.join(folder, filename)
    if not os.path.exists(full_path):
        return json.dumps({"status": "error", "error": f"Not found: {full_path}"})

    try:
        subprocess.Popen(
            ["explorer.exe", "/select,", full_path],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        _audit.log("controller.uia_select", data={"path": full_path})
        return json.dumps({"status": "ok", "selected": full_path, "method": "uia"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _uia_interact_app(alias: str, action: str, params: dict[str, Any], *, _bypass_gate: bool = False) -> str:
    """Interact with an allowlisted application via UIA."""
    if not _bypass_gate:
        dec = _gate(ActionType.CLICK, target_app=alias)
        if dec.blocked:
            return _blocked_result(dec)
        if dec.requires_access_grant:
            return _pending_access_grant_result(
                dec,
                target_type="app",
                target_identifier=alias,
                display_name=alias,
                action_context=f"UIA interact with {alias}: {action}",
            )
        if dec.requires_confirmation:
            return _pending_approval_result(
                dec, f"UIA interact with {alias}: {action}",
                tool_name="ctrl_interact_app", action_type="click",
                target_app=alias,
                input_str=json.dumps({"app": alias, "action": action, "params": params}),
            )

    if not _uia_available():
        return json.dumps({
            "status": "fallback_needed",
            "reason": "pywinauto not installed",
            "reason_code": "uia_unavailable",
            "policy_source": "settings.json",
        })

    app_rule = get_effective_app_rule(alias)
    if app_rule is not None and not app_rule.uia_allowed:
        return json.dumps({
            "status": "blocked",
            "reason": f"UI automation is disabled for '{alias}'.",
            "reason_code": "uia_not_allowed",
            "policy_source": "settings.json",
        })

    try:
        from pywinauto import Desktop

        desktop = Desktop(backend="uia")
        windows = desktop.windows()
        query_title = str(params.get("window_title") or params.get("title") or alias).strip().lower()
        alias_query = alias.lower()
        matches = []
        for w in windows:
            title = w.window_text().lower()
            if query_title in title or alias_query in title:
                matches.append(w)

        if not matches:
            return json.dumps({
                "status": "error",
                "error": f"No window found for '{alias}'.",
                "reason_code": "window_not_found",
                "policy_source": "settings.json",
                "matches": [],
            })

        def _window_meta(window) -> dict[str, Any]:
            handle = getattr(window, "handle", None)
            meta = {
                "title": window.window_text(),
                "handle": int(handle) if handle is not None else None,
            }
            rect = _element_rect(window)
            if rect:
                meta["bounds"] = {key: rect[key] for key in ("x", "y", "width", "height", "right", "bottom")}
                meta["center"] = {"x": rect["center_x"], "y": rect["center_y"]}
            return meta

        if len(matches) > 1:
            return json.dumps({
                "status": "error",
                "error": f"Multiple windows matched '{alias}'.",
                "reason_code": "window_ambiguous",
                "policy_source": "settings.json",
                "matches": [_window_meta(w) for w in matches[:10]],
            })

        target = matches[0]
        matched_window = _window_meta(target)

        def _matched_elements() -> tuple[list[Any], int]:
            element_name = str(params.get("element", params.get("name", ""))).strip()
            automation_id = str(params.get("automation_id", "")).strip()
            control_type = str(params.get("control_type", "")).strip()
            occurrence = int(params.get("occurrence", 0) or 0)
            if not element_name and not automation_id and action not in {"get_elements", "wait_element"}:
                raise ValueError("Provide element name or automation_id.")
            candidates = [
                child
                for child in target.descendants()
                if _element_matches(
                    child,
                    name_query=element_name,
                    automation_id=automation_id,
                    control_type=control_type,
                )
            ]
            return candidates, occurrence

        def _element_read_value(element) -> str:
            try:
                return str(element.get_value())
            except Exception:
                pass
            try:
                value_pattern = element.iface_value
                return str(value_pattern.CurrentValue)
            except Exception:
                return ""

        if action == "click_element":
            element_name = str(params.get("element", params.get("name", ""))).strip()
            automation_id = str(params.get("automation_id", "")).strip()
            control_type = str(params.get("control_type", "")).strip()
            occurrence = int(params.get("occurrence", 0) or 0)
            if not element_name and not automation_id:
                return json.dumps({
                    "status": "error",
                    "error": "Provide element name or automation_id.",
                    "reason_code": "invalid_input",
                    "policy_source": "settings.json",
                    "matched_window": matched_window,
                })
            try:
                candidates = [
                    child
                    for child in target.descendants()
                    if _element_matches(
                        child,
                        name_query=element_name,
                        automation_id=automation_id,
                        control_type=control_type,
                    )
                ]
                if not candidates:
                    return json.dumps({
                        "status": "error",
                        "error": "Element not found.",
                        "reason_code": "element_not_found",
                        "policy_source": "settings.json",
                        "matched_window": matched_window,
                    })
                if occurrence < 0 or occurrence >= len(candidates):
                    return json.dumps({
                        "status": "error",
                        "error": f"occurrence {occurrence} is outside {len(candidates)} matched element(s).",
                        "reason_code": "invalid_occurrence",
                        "matches": [_element_summary(child, index) for index, child in enumerate(candidates[:20])],
                        "policy_source": "settings.json",
                        "matched_window": matched_window,
                    })
                if len(candidates) > 1 and "occurrence" not in params:
                    return json.dumps({
                        "status": "error",
                        "error": f"Multiple elements matched '{element_name or automation_id}'.",
                        "reason_code": "element_ambiguous",
                        "matches": [_element_summary(child, index) for index, child in enumerate(candidates[:20])],
                        "policy_source": "settings.json",
                        "matched_window": matched_window,
                    })

                child = candidates[occurrence]
                summary = _element_summary(child, occurrence)
                try:
                    child.click_input()
                    clicked_by = "uia"
                except Exception:
                    center = summary.get("center")
                    if not center:
                        raise
                    click_result = json.loads(
                        _screen_click(
                            int(center["x"]),
                            int(center["y"]),
                            "left",
                            _bypass_gate=True,
                        )
                    )
                    if click_result.get("status") != "ok":
                        return json.dumps(click_result)
                    clicked_by = "bounds_center"
                _audit.log("controller.uia_click", data={"app": alias, "element": element_name})
                return json.dumps({
                    "status": "ok",
                    "app": alias,
                    "action": action,
                    "element": element_name,
                    "method": "uia",
                    "clicked_by": clicked_by,
                    "target": summary,
                    "matched_window": matched_window,
                    "policy_source": "settings.json",
                })
            except Exception as e:
                return json.dumps({
                    "status": "error",
                    "error": f"Element not found: {e}",
                    "reason_code": "element_not_found",
                    "policy_source": "settings.json",
                    "matched_window": matched_window,
                })

        elif action in {"read_element", "type_element", "set_value", "invoke_element", "select_option", "wait_element"}:
            import time as _time

            timeout = max(0.1, min(float(params.get("timeout_seconds", 5) or 5), 60.0))
            deadline = _time.monotonic() + timeout
            candidates: list[Any] = []
            occurrence = int(params.get("occurrence", 0) or 0)
            while _time.monotonic() <= deadline:
                try:
                    candidates, occurrence = _matched_elements()
                except ValueError as exc:
                    return json.dumps({
                        "status": "error",
                        "error": str(exc),
                        "reason_code": "invalid_input",
                        "policy_source": "settings.json",
                        "matched_window": matched_window,
                    })
                if candidates or action != "wait_element":
                    break
                _time.sleep(0.2)

            if not candidates:
                return json.dumps({
                    "status": "error",
                    "error": "Element not found.",
                    "reason_code": "element_not_found",
                    "policy_source": "settings.json",
                    "matched_window": matched_window,
                })
            if occurrence < 0 or occurrence >= len(candidates):
                return json.dumps({
                    "status": "error",
                    "error": f"occurrence {occurrence} is outside {len(candidates)} matched element(s).",
                    "reason_code": "invalid_occurrence",
                    "matches": [_element_summary(child, index) for index, child in enumerate(candidates[:20])],
                    "policy_source": "settings.json",
                    "matched_window": matched_window,
                })
            if len(candidates) > 1 and "occurrence" not in params:
                return json.dumps({
                    "status": "error",
                    "error": "Multiple elements matched.",
                    "reason_code": "element_ambiguous",
                    "matches": [_element_summary(child, index) for index, child in enumerate(candidates[:20])],
                    "policy_source": "settings.json",
                    "matched_window": matched_window,
                })

            child = candidates[occurrence]
            summary = _element_summary(child, occurrence)
            value_before = _element_read_value(child)
            if action in {"type_element", "set_value"}:
                text = str(params.get("text", ""))
                clear = bool(params.get("clear", True))
                paste_meta: dict[str, Any] = {}
                try:
                    if clear:
                        child.set_edit_text(text)
                        method = "uia_value"
                    else:
                        child.click_input()
                        paste_meta = _paste_text_via_clipboard(text)
                        method = "uia_paste" if paste_meta.get("status") == "ok" else "uia_keys"
                        if paste_meta.get("status") != "ok":
                            child.type_keys(text, with_spaces=True)
                except Exception:
                    child.click_input()
                    if clear:
                        _ctrl_hotkey("ctrl+a", _bypass_gate=True)
                        _ctrl_hotkey("backspace", _bypass_gate=True)
                    paste_meta = _paste_text_via_clipboard(text)
                    method = "uia_paste" if paste_meta.get("status") == "ok" else "uia_keys"
                    if paste_meta.get("status") != "ok":
                        child.type_keys(text, with_spaces=True)
                value_after = _element_read_value(child)
                if (
                    method == "uia_paste"
                    and text
                    and value_before
                    and value_after == value_before
                ):
                    child.type_keys(text, with_spaces=True)
                    method = "uia_keys"
                    value_after = _element_read_value(child)
                payload = {
                    "status": "ok",
                    "app": alias,
                    "action": action,
                    "method": method,
                    "target": summary,
                    "value_before": value_before,
                    "value_after": value_after,
                    "matched_window": matched_window,
                    "policy_source": "settings.json",
                }
                if paste_meta:
                    payload["paste"] = paste_meta
                return json.dumps(payload)
            if action == "invoke_element":
                try:
                    child.invoke()
                    method = "uia_invoke"
                except Exception:
                    child.click_input()
                    method = "uia_click"
                return json.dumps({
                    "status": "ok",
                    "app": alias,
                    "action": action,
                    "method": method,
                    "target": summary,
                    "matched_window": matched_window,
                    "policy_source": "settings.json",
                })
            if action == "select_option":
                option = str(params.get("option", ""))
                try:
                    child.select(option)
                    method = "uia_select"
                except Exception:
                    child.click_input()
                    method = "uia_click"
                return json.dumps({
                    "status": "ok",
                    "app": alias,
                    "action": action,
                    "method": method,
                    "option": option,
                    "target": summary,
                    "matched_window": matched_window,
                    "policy_source": "settings.json",
                })
            return json.dumps({
                "status": "ok",
                "app": alias,
                "action": action,
                "target": summary,
                "value": value_before,
                "matched_window": matched_window,
                "policy_source": "settings.json",
            })

        elif action == "type_text":
            text = params.get("text", "")
            target.click_input()
            type_meta = _type_text_via_keyboard(str(text))
            _audit.log("controller.uia_type", data={"app": alias, "text_len": len(text), "method": type_meta.get("method", "type")})
            if type_meta.get("status") != "ok":
                return json.dumps(type_meta)
            payload = {
                "status": "ok",
                "app": alias,
                "action": action,
                "method": type_meta.get("method", "type"),
                "matched_window": matched_window,
                "policy_source": "settings.json",
            }
            payload.update(type_meta)
            return json.dumps(payload)

        elif action == "get_elements":
            elements = []
            limit = max(1, min(int(params.get("limit", 80) or 80), 200))
            name_filter = str(params.get("contains", params.get("element", ""))).strip().lower()
            control_type_filter = str(params.get("control_type", "")).strip().lower()
            for child in target.descendants():
                try:
                    summary = _element_summary(child, len(elements))
                    if name_filter and name_filter not in str(summary.get("name", "")).lower():
                        continue
                    if control_type_filter:
                        type_values = {
                            str(summary.get("type", "")).lower(),
                            str(summary.get("control_type", "")).lower(),
                            str(summary.get("class_name", "")).lower(),
                        }
                        if control_type_filter not in type_values:
                            continue
                    if (
                        str(summary.get("name", "")).strip()
                        or str(summary.get("automation_id", "")).strip()
                        or summary.get("bounds")
                    ):
                        elements.append(summary)
                except Exception:
                    pass
                if len(elements) >= limit:
                    break
            return json.dumps({
                "status": "ok",
                "app": alias,
                "elements": elements,
                "count": len(elements),
                "method": "uia",
                "matched_window": matched_window,
                "coordinate_hint": "Use element.center.x and element.center.y with click, or prefer click_ui_element.",
                "policy_source": "settings.json",
            })

        return json.dumps({
            "status": "error",
            "error": f"Unknown UIA action: {action}",
            "reason_code": "unknown_action",
            "policy_source": "settings.json",
            "matched_window": matched_window,
        })

    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "reason_code": "uia_error",
            "policy_source": "settings.json",
        })


# ── Strategy 3: Coordinate click fallback ─────────────────────────────────────


def _screen_click(x: int, y: int, button: str = "left", *, _bypass_gate: bool = False) -> str:
    """Click at screen coordinates using Win32 SendInput."""
    if not _bypass_gate and not is_screen_fallback_allowed():
        return json.dumps({
            "status": "blocked",
            "reason": "Screen fallback is disabled in settings.",
            "reason_code": "screen_fallback_disabled",
            "policy_source": "settings.json",
        })
    if not _bypass_gate:
        pending = _permission_result(
            ActionType.CLICK,
            f"Screen click at ({x}, {y})",
            tool_name="ctrl_screen_click",
            action_type="click",
            input_str=json.dumps({"x": x, "y": y, "button": button}),
            payload_args={"x": x, "y": y, "button": button},
        )
        if pending is not None:
            return pending

    try:
        ctypes.windll.user32.SetCursorPos(x, y)
        time.sleep(0.05)

        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        MOUSEEVENTF_RIGHTDOWN = 0x0008
        MOUSEEVENTF_RIGHTUP = 0x0010

        if button == "right":
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            time.sleep(0.02)
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
        else:
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.02)
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

        _audit.log("controller.screen_click", data={"x": x, "y": y, "button": button})
        return json.dumps({"status": "ok", "x": x, "y": y, "button": button, "method": "click"})

    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


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
    *,
    _bypass_gate: bool = False,
) -> str:
    """Map screenshot/window/normalized coordinates to screen coordinates and click."""
    if not _bypass_gate and not is_screen_fallback_allowed():
        return json.dumps({
            "status": "blocked",
            "reason": "Screen fallback is disabled in settings.",
            "reason_code": "screen_fallback_disabled",
            "policy_source": "settings.json",
        })

    mapping = map_precision_coordinates(
        coordinate_mode=coordinate_mode,
        x=float(x),
        y=float(y),
        origin_x=int(origin_x),
        origin_y=int(origin_y),
        width=int(width),
        height=int(height),
        monitor=int(monitor),
        title=title,
        hwnd=int(hwnd or 0),
        verify_bounds=bool(verify_bounds),
    )
    if mapping.get("status") != "ok":
        return json.dumps(mapping, ensure_ascii=False)

    mapped = mapping["mapped"]
    payload_args = {
        "x": x,
        "y": y,
        "button": button,
        "clicks": clicks,
        "coordinate_mode": coordinate_mode,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "width": width,
        "height": height,
        "monitor": monitor,
        "title": title,
        "hwnd": hwnd,
        "verify_bounds": verify_bounds,
    }
    if not _bypass_gate:
        pending = _permission_result(
            ActionType.CLICK,
            f"Precision click at ({mapped['x']}, {mapped['y']}) from {mapping['coordinate_mode']} coordinates",
            tool_name="precision_click",
            action_type="click",
            input_str=json.dumps(payload_args),
            payload_args=payload_args,
        )
        if pending is not None:
            return pending

    click_count = max(1, int(clicks or 1))
    if click_count >= 2:
        click_result = json.loads(
            _ctrl_double_click(int(mapped["x"]), int(mapped["y"]), button, _bypass_gate=True)
        )
    else:
        click_result = json.loads(_screen_click(int(mapped["x"]), int(mapped["y"]), button, _bypass_gate=True))
    if click_result.get("status") != "ok":
        return json.dumps(click_result, ensure_ascii=False)

    click_result.update(
        {
            "method": "precision_click",
            "coordinate_mode": mapping["coordinate_mode"],
            "mapped": mapping["mapped"],
            "bounds": mapping.get("bounds", {}),
            "source_point": mapping.get("source_point", {}),
        }
    )
    if "window" in mapping:
        click_result["window"] = mapping["window"]
    _audit.log(
        "controller.precision_click",
        data={"mode": mapping["coordinate_mode"], "mapped": mapping["mapped"], "clicks": click_count},
    )
    return json.dumps(click_result, ensure_ascii=False)


def _screen_type(
    text: str,
    *,
    target_hwnd: int = 0,
    target_title: str = "",
    target_process_name: str = "",
    _bypass_gate: bool = False,
) -> str:
    """Type text into the focused control."""
    if not _bypass_gate and not is_screen_fallback_allowed():
        return json.dumps({
            "status": "blocked",
            "reason": "Screen fallback is disabled in settings.",
            "reason_code": "screen_fallback_disabled",
            "policy_source": "settings.json",
        })
    if not _bypass_gate:
        payload_args = {"text": text}
        payload_args.update(_foreground_window_context())
        if target_hwnd:
            payload_args["target_hwnd"] = int(target_hwnd)
        if target_title:
            payload_args["target_title"] = target_title
        if target_process_name:
            payload_args["target_process_name"] = target_process_name
        pending = _permission_result(
            ActionType.TYPE,
            f"Type text: {text[:40]}...",
            tool_name="ctrl_screen_type",
            action_type="type",
            input_str=json.dumps(payload_args, ensure_ascii=False, sort_keys=True),
            payload_args=payload_args,
        )
        if pending is not None:
            return pending

    try:
        focus_error = _focus_target_window(
            target_hwnd=target_hwnd,
            target_title=target_title,
            target_process_name=target_process_name,
        )
        if focus_error:
            return json.dumps(
                {
                    "status": "error",
                    "reason_code": "target_window_not_focused",
                    "error": focus_error,
                    "target_hwnd": int(target_hwnd or 0),
                    "target_title": target_title,
                },
                ensure_ascii=False,
            )

        type_meta = _type_text_via_keyboard(text)
        if type_meta.get("status") == "error" and type_meta.get("reason_code") == "unsupported_keyboard_character":
            type_meta = _paste_text_via_clipboard(text)
        if type_meta.get("status") != "ok":
            return json.dumps(type_meta, ensure_ascii=False)
        _audit.log("controller.screen_type", data={"text_len": len(text), "method": type_meta.get("method", "type")})
        return json.dumps(type_meta, ensure_ascii=False)

    except ImportError:
        return json.dumps({"status": "error", "error": "pywin32 not installed"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


# ── Typed tool implementations (StructuredTool-compatible) ────────────────────


def _ctrl_open_folder(path: str) -> str:
    """Open a folder in Explorer."""
    if not path:
        return json.dumps({"status": "error", "error": "Provide a folder path."})
    return _cmd_open_folder(path)


def _ctrl_create_folder(path: str) -> str:
    """Create a folder."""
    if not path:
        return json.dumps({"status": "error", "error": "Provide a folder path."})
    return _cmd_create_folder(path)


def _ctrl_move(source: str, destination: str) -> str:
    """Move a file or folder."""
    if not source or not destination:
        return json.dumps({"status": "error", "error": "Provide 'source' and 'destination'."})
    return _cmd_move_item(source, destination)


def _ctrl_copy(source: str, destination: str) -> str:
    """Copy a file or folder."""
    if not source or not destination:
        return json.dumps({"status": "error", "error": "Provide 'source' and 'destination'."})
    return _cmd_copy_item(source, destination)


def _ctrl_rename(path: str, new_name: str) -> str:
    """Rename a file or folder."""
    if not path or not new_name:
        return json.dumps({"status": "error", "error": "Provide 'path' and 'new_name'."})
    return _cmd_rename_item(path, new_name)


def _ctrl_delete(path: str) -> str:
    """Delete a file or folder (access grant for unknown paths, else approval if required)."""
    if not path:
        return json.dumps({"status": "error", "error": "Provide a path to delete."})
    return _cmd_delete_item(path)


def _ctrl_select_in_explorer(folder: str, filename: str) -> str:
    """Open Explorer and select a specific file."""
    if not folder or not filename:
        return json.dumps({"status": "error", "error": "Provide 'folder' and 'filename'."})
    return _uia_open_and_select(folder, filename)


def _ctrl_interact_app(app: str, action: str, params: dict | None = None) -> str:
    """Interact with an allowlisted app via UIA."""
    if not app or not action:
        return json.dumps({"status": "error", "error": "Provide 'app' and 'action'."})
    return _uia_interact_app(app, action, params or {})


def _ctrl_click(x: int, y: int, button: str = "left") -> str:
    """Click at screen coordinates (last-resort fallback)."""
    return _screen_click(int(x), int(y), button)


def _ctrl_type(text: str) -> str:
    """Type text into the focused control (last-resort fallback)."""
    if not text:
        return json.dumps({"status": "error", "error": "Provide text to type."})
    return _screen_type(text)


def _ctrl_get_policy() -> str:
    """Return the current controller policy as JSON."""
    state = load_policy()
    return json.dumps({
        "mode": state.mode.value,
        "permitted_roots": state.permitted_roots,
        "allowlisted_apps": [a.alias for a in state.allowlisted_apps],
        "dangerous_actions_require_confirm": state.dangerous_actions_require_confirm,
        "policy_source": "settings.json",
    })


# ── New tools: hotkey, window_action, scroll, double_click, clipboard, drag ──

# Key name → Win32 Virtual Key code mapping
def _foreground_window_context() -> dict[str, Any]:
    """Capture the current foreground window so approved keyboard replay can retarget it."""
    try:
        import win32gui
        import win32process

        hwnd = int(win32gui.GetForegroundWindow())
        if not hwnd:
            return {}
        if hasattr(win32gui, "IsWindow") and not win32gui.IsWindow(hwnd):
            return {}
        try:
            title = str(win32gui.GetWindowText(hwnd) or "")
        except Exception:
            title = ""
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = 0
        process_name = ""
        if pid:
            try:
                import psutil

                process_name = psutil.Process(pid).name()
            except Exception:
                process_name = ""
        return {
            "target_hwnd": hwnd,
            "target_title": title,
            "target_pid": int(pid or 0),
            "target_process_name": process_name,
        }
    except Exception:
        return {}


def _focus_target_window(
    *,
    target_hwnd: int = 0,
    target_title: str = "",
    target_process_name: str = "",
) -> str | None:
    """Restore and verify a target window before sending global keyboard input."""
    hwnd = int(target_hwnd or 0)
    if not hwnd:
        return None
    try:
        import win32api
        import win32con
        import win32gui
        import win32process

        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return f"Target window is no longer visible: hwnd={hwnd}."

        actual_title = str(win32gui.GetWindowText(hwnd) or "")
        if target_title and actual_title and target_title != actual_title:
            return (
                f"Target window changed before keyboard replay: expected {target_title!r}, "
                f"got {actual_title!r}."
            )

        if target_process_name:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            actual_process = ""
            try:
                import psutil

                actual_process = psutil.Process(pid).name()
            except Exception:
                actual_process = ""
            if actual_process and actual_process.lower() != target_process_name.lower():
                return (
                    f"Target process changed before keyboard replay: expected {target_process_name!r}, "
                    f"got {actual_process!r}."
                )

        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception:
            pass
        try:
            win32gui.BringWindowToTop(hwnd)
        except Exception:
            pass

        focused = False
        try:
            win32gui.SetForegroundWindow(hwnd)
            focused = True
        except Exception:
            focused = False

        if not focused:
            try:
                foreground = int(win32gui.GetForegroundWindow())
                foreground_thread, _ = win32process.GetWindowThreadProcessId(foreground)
                target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
                current_thread = win32api.GetCurrentThreadId()
                for thread_id in {foreground_thread, target_thread}:
                    if thread_id:
                        win32process.AttachThreadInput(current_thread, thread_id, True)
                try:
                    win32gui.BringWindowToTop(hwnd)
                    win32gui.SetForegroundWindow(hwnd)
                    focused = True
                finally:
                    for thread_id in {foreground_thread, target_thread}:
                        if thread_id:
                            win32process.AttachThreadInput(current_thread, thread_id, False)
            except Exception:
                focused = False

        time.sleep(0.1)
        foreground = int(win32gui.GetForegroundWindow())
        if foreground != hwnd:
            return (
                f"Target window was not focused for keyboard replay. "
                f"expected_hwnd={hwnd}, foreground_hwnd={foreground}."
            )
        return None
    except ImportError:
        return "pywin32 not installed"
    except Exception as exc:
        return str(exc)


_VK_MAP: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B, "windows": 0x5B,
    "tab": 0x09,
    "enter": 0x0D, "return": 0x0D,
    "escape": 0x1B, "esc": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "ins": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "printscreen": 0x2C, "prtsc": 0x2C,
    "pause": 0x13,
    "numlock": 0x90,
    "capslock": 0x14,
    "apps": 0x5D,  # context menu key
}

KEYEVENTF_KEYUP = 0x0002


def _vk_for_key(key: str) -> int:
    """Return the VK code for a key name. Handles single chars and mapped names."""
    k = key.lower().strip()
    if k in _VK_MAP:
        return _VK_MAP[k]
    if len(k) == 1:
        import win32api
        vk = win32api.VkKeyScan(k) & 0xFF
        return vk
    raise ValueError(f"Unknown key: {key!r}")


def _ctrl_hotkey(
    keys: str,
    *,
    target_hwnd: int = 0,
    target_title: str = "",
    target_process_name: str = "",
    _bypass_gate: bool = False,
) -> str:
    """Send a keyboard shortcut. keys is '+' separated, e.g. 'ctrl+c', 'alt+f4', 'win+e'."""
    if not _bypass_gate:
        payload_args = {"keys": keys}
        payload_args.update(_foreground_window_context())
        if target_hwnd:
            payload_args["target_hwnd"] = int(target_hwnd)
        if target_title:
            payload_args["target_title"] = target_title
        if target_process_name:
            payload_args["target_process_name"] = target_process_name
        pending = _permission_result(
            ActionType.TYPE,
            f"Send hotkey: {keys}",
            tool_name="hotkey",
            action_type="type",
            input_str=json.dumps(payload_args, ensure_ascii=False, sort_keys=True),
            payload_args=payload_args,
        )
        if pending is not None:
            return pending

    try:
        import win32api

        focus_error = _focus_target_window(
            target_hwnd=target_hwnd,
            target_title=target_title,
            target_process_name=target_process_name,
        )
        if focus_error:
            return json.dumps(
                {
                    "status": "error",
                    "reason_code": "target_window_not_focused",
                    "error": focus_error,
                    "keys": keys,
                    "target_hwnd": int(target_hwnd or 0),
                    "target_title": target_title,
                },
                ensure_ascii=False,
            )

        parts = [p.strip() for p in keys.split("+") if p.strip()]
        if not parts:
            return json.dumps({"status": "error", "error": "No keys provided."})

        vk_codes = [_vk_for_key(p) for p in parts]

        # Press all keys down in order
        for vk in vk_codes:
            win32api.keybd_event(vk, 0, 0, 0)
            time.sleep(0.02)

        # Release all keys in reverse order
        for vk in reversed(vk_codes):
            win32api.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.02)

        _audit.log("controller.hotkey", data={"keys": keys})
        return json.dumps({"status": "ok", "keys": keys, "method": "hotkey"})

    except ImportError:
        return json.dumps({"status": "error", "error": "pywin32 not installed"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _win32_window_meta(win32gui, win32process, hwnd: int) -> dict[str, Any]:
    title = win32gui.GetWindowText(hwnd)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    process_name = ""
    try:
        import psutil

        process_name = psutil.Process(pid).name()
    except Exception:
        process_name = ""
    return {"title": title, "handle": int(hwnd), "hwnd": int(hwnd), "pid": pid, "process_name": process_name}


def _select_win32_window(win32gui, win32process, *, title: str = "", hwnd: int = 0) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
    if hwnd:
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return None, f"No visible window with hwnd {hwnd}.", []
        return _win32_window_meta(win32gui, win32process, hwnd), "", []

    query = title.strip().lower()
    if not query:
        return None, "Provide a window title or hwnd.", []

    matches: list[dict[str, Any]] = []

    def _enum(candidate_hwnd, _):
        current_title = win32gui.GetWindowText(candidate_hwnd)
        if current_title.strip() and query in current_title.lower() and win32gui.IsWindowVisible(candidate_hwnd):
            matches.append(_win32_window_meta(win32gui, win32process, candidate_hwnd))

    win32gui.EnumWindows(_enum, None)
    if not matches:
        return None, f"No visible window matching '{title}'.", []

    exact = [item for item in matches if item["title"].lower() == query]
    if len(exact) == 1:
        return exact[0], "", matches
    if len(matches) == 1:
        return matches[0], "", matches
    return None, f"Multiple visible windows matched '{title}'.", matches[:10]


def _ctrl_window_action(
    title: str,
    action: str,
    width: int = 0,
    height: int = 0,
    x: int = 0,
    y: int = 0,
    hwnd: int = 0,
    *,
    _bypass_gate: bool = False,
) -> str:
    """Manage window state. action: minimize|maximize|restore|close|snap_left|snap_right|resize|move."""
    valid_actions = {"minimize", "maximize", "restore", "close", "snap_left", "snap_right", "resize", "move"}
    if action not in valid_actions:
        return json.dumps({"status": "error", "error": f"Unknown action: {action!r}. Use minimize|maximize|restore|close|snap_left|snap_right|resize|move."})

    try:
        import win32gui
        import win32con
        import win32process

        matched_window, error, matches = _select_win32_window(
            win32gui,
            win32process,
            title=title,
            hwnd=int(hwnd or 0),
        )
        if matched_window is None:
            payload: dict[str, Any] = {"status": "error", "error": error}
            if matches:
                payload["reason_code"] = "window_ambiguous"
                payload["matches"] = matches
            return json.dumps(payload, ensure_ascii=False)

        hwnd = int(matched_window["hwnd"])
        window_title = matched_window["title"]
        target_app = matched_window.get("process_name") or title

        if not _bypass_gate:
            pending = _permission_result(
                ActionType.CLICK,
                f"Window action '{action}' on {window_title}",
                tool_name="window_action",
                action_type="click",
                target_app=target_app,
                input_str=json.dumps(
                    {
                        "title": title,
                        "action": action,
                        "width": width,
                        "height": height,
                        "x": x,
                        "y": y,
                        "hwnd": hwnd,
                    }
                ),
                payload_args={
                    "title": title,
                    "action": action,
                    "width": width,
                    "height": height,
                    "x": x,
                    "y": y,
                    "hwnd": hwnd,
                },
            )
            if pending is not None:
                return pending

        if action == "minimize":
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        elif action == "maximize":
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        elif action == "restore":
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        elif action == "close":
            import win32api as _w32api
            _w32api.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        elif action in ("snap_left", "snap_right"):
            import ctypes
            sw = ctypes.windll.user32.GetSystemMetrics(0)  # SM_CXSCREEN
            sh = ctypes.windll.user32.GetSystemMetrics(1)  # SM_CYSCREEN
            if action == "snap_left":
                win32gui.MoveWindow(hwnd, 0, 0, sw // 2, sh, True)
            else:
                win32gui.MoveWindow(hwnd, sw // 2, 0, sw // 2, sh, True)
        elif action == "resize":
            if width <= 0 or height <= 0:
                return json.dumps({"status": "error", "error": "Provide width and height > 0 for resize."})
            rect = win32gui.GetWindowRect(hwnd)
            win32gui.MoveWindow(hwnd, rect[0], rect[1], width, height, True)
        elif action == "move":
            rect = win32gui.GetWindowRect(hwnd)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            win32gui.MoveWindow(hwnd, x, y, w, h, True)
        _audit.log("controller.window_action", data={"title": title, "action": action, "hwnd": hwnd})
        return json.dumps({"status": "ok", "action": action, "window": window_title, "hwnd": hwnd, "matched_window": matched_window}, ensure_ascii=False)

    except ImportError:
        return json.dumps({"status": "error", "error": "pywin32 not installed"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _ctrl_scroll(direction: str = "down", clicks: int = 3, x: int = -1, y: int = -1, *, _bypass_gate: bool = False) -> str:
    """Scroll the mouse wheel. direction: up|down. clicks: scroll steps (default 3)."""
    if not _bypass_gate and not is_screen_fallback_allowed():
        return json.dumps({
            "status": "blocked",
            "reason": "Screen fallback is disabled in settings.",
            "reason_code": "screen_fallback_disabled",
        })
    if not _bypass_gate:
        pending = _permission_result(
            ActionType.CLICK,
            f"Scroll {direction} by {clicks}",
            tool_name="scroll",
            action_type="click",
            input_str=json.dumps({"direction": direction, "clicks": clicks, "x": x, "y": y}),
            payload_args={"direction": direction, "clicks": clicks, "x": x, "y": y},
        )
        if pending is not None:
            return pending

    try:
        MOUSEEVENTF_WHEEL = 0x0800
        WHEEL_DELTA = 120

        if x >= 0 and y >= 0:
            ctypes.windll.user32.SetCursorPos(x, y)
            time.sleep(0.05)

        delta = WHEEL_DELTA * clicks if direction == "up" else -(WHEEL_DELTA * clicks)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta & 0xFFFFFFFF, 0)

        _audit.log("controller.scroll", data={"direction": direction, "clicks": clicks})
        return json.dumps({"status": "ok", "direction": direction, "clicks": clicks, "method": "scroll"})

    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _ctrl_double_click(x: int, y: int, button: str = "left", *, _bypass_gate: bool = False) -> str:
    """Double-click at screen coordinates."""
    if not _bypass_gate and not is_screen_fallback_allowed():
        return json.dumps({
            "status": "blocked",
            "reason": "Screen fallback is disabled in settings.",
            "reason_code": "screen_fallback_disabled",
        })
    if not _bypass_gate:
        pending = _permission_result(
            ActionType.CLICK,
            f"Double-click at ({x}, {y})",
            tool_name="ctrl_screen_click",
            action_type="click",
            input_str=json.dumps({"x": x, "y": y, "button": button, "clicks": 2}),
            payload_args={"x": x, "y": y, "button": button, "clicks": 2},
        )
        if pending is not None:
            return pending

    try:
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        MOUSEEVENTF_RIGHTDOWN = 0x0008
        MOUSEEVENTF_RIGHTUP = 0x0010
        down = MOUSEEVENTF_RIGHTDOWN if button == "right" else MOUSEEVENTF_LEFTDOWN
        up = MOUSEEVENTF_RIGHTUP if button == "right" else MOUSEEVENTF_LEFTUP

        ctypes.windll.user32.SetCursorPos(x, y)
        time.sleep(0.05)
        ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.02)
        ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.05)
        ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.02)
        ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)

        _audit.log("controller.double_click", data={"x": x, "y": y, "button": button})
        return json.dumps({"status": "ok", "x": x, "y": y, "button": button, "method": "double_click"})

    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _ctrl_clipboard(action: str = "read", text: str = "", *, _bypass_gate: bool = False) -> str:
    """Read or write the system clipboard. action: read|write|clear."""
    if action == "read":
        permission_action = ActionType.READ
    else:
        permission_action = ActionType.MUTATE
    if not _bypass_gate:
        pending = _permission_result(
            permission_action,
            f"Clipboard {action}",
            tool_name="ctrl_clipboard",
            action_type=permission_action.value,
            input_str=json.dumps({"action": action, "text": text}),
            payload_args={"action": action, "text": text},
        )
        if pending is not None:
            return pending

    try:
        import win32clipboard
        import win32con

        if action == "read":
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                elif win32clipboard.IsClipboardFormatAvailable(win32con.CF_TEXT):
                    data = win32clipboard.GetClipboardData(win32con.CF_TEXT)
                    if isinstance(data, bytes):
                        data = data.decode("utf-8", errors="replace")
                else:
                    data = ""
            finally:
                win32clipboard.CloseClipboard()
            _audit.log("controller.clipboard_read", data={})
            return json.dumps({"status": "ok", "action": "read", "text": data})

        elif action == "write":
            if not text:
                return json.dumps({"status": "error", "error": "Provide 'text' to write to clipboard."})
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()
            _audit.log("controller.clipboard_write", data={"text_len": len(text)})
            return json.dumps({"status": "ok", "action": "write", "chars_written": len(text)})

        elif action == "clear":
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
            finally:
                win32clipboard.CloseClipboard()
            _audit.log("controller.clipboard_clear", data={})
            return json.dumps({"status": "ok", "action": "clear"})

        else:
            return json.dumps({"status": "error", "error": f"Unknown action: {action!r}. Use read|write|clear."})

    except ImportError:
        return json.dumps({"status": "error", "error": "pywin32 not installed"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _ctrl_drag(start_x: int, start_y: int, end_x: int, end_y: int, button: str = "left", *, _bypass_gate: bool = False) -> str:
    """Drag from (start_x, start_y) to (end_x, end_y)."""
    if not _bypass_gate and not is_screen_fallback_allowed():
        return json.dumps({
            "status": "blocked",
            "reason": "Screen fallback is disabled in settings.",
            "reason_code": "screen_fallback_disabled",
        })
    if not _bypass_gate:
        pending = _permission_result(
            ActionType.CLICK,
            f"Drag from ({start_x}, {start_y}) to ({end_x}, {end_y})",
            tool_name="drag",
            action_type="click",
            input_str=json.dumps(
                {
                    "from_x": start_x,
                    "from_y": start_y,
                    "to_x": end_x,
                    "to_y": end_y,
                    "button": button,
                }
            ),
            payload_args={
                "from_x": start_x,
                "from_y": start_y,
                "to_x": end_x,
                "to_y": end_y,
                "button": button,
            },
        )
        if pending is not None:
            return pending

    try:
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        MOUSEEVENTF_RIGHTDOWN = 0x0008
        MOUSEEVENTF_RIGHTUP = 0x0010
        MOUSEEVENTF_MOVE = 0x0001

        btn_down = MOUSEEVENTF_LEFTDOWN if button != "right" else MOUSEEVENTF_RIGHTDOWN
        btn_up = MOUSEEVENTF_LEFTUP if button != "right" else MOUSEEVENTF_RIGHTUP

        ctypes.windll.user32.SetCursorPos(start_x, start_y)
        time.sleep(0.05)
        ctypes.windll.user32.mouse_event(btn_down, 0, 0, 0, 0)
        time.sleep(0.05)

        # Smooth movement via 15 interpolated steps
        steps = 15
        for i in range(1, steps + 1):
            ix = int(start_x + (end_x - start_x) * i / steps)
            iy = int(start_y + (end_y - start_y) * i / steps)
            ctypes.windll.user32.SetCursorPos(ix, iy)
            time.sleep(0.02)

        ctypes.windll.user32.mouse_event(btn_up, 0, 0, 0, 0)

        _audit.log("controller.drag", data={"from": [start_x, start_y], "to": [end_x, end_y], "button": button})
        return json.dumps({
            "status": "ok",
            "from": [start_x, start_y],
            "to": [end_x, end_y],
            "button": button,
            "method": "drag",
        })

    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _ctrl_list_processes(filter_name: str = "") -> str:
    """List running processes, optionally filtered by name substring."""
    dec = _gate(ActionType.READ)
    if dec.blocked:
        return _blocked_result(dec)

    try:
        import psutil

        results = []
        for proc in psutil.process_iter(["pid", "name", "memory_info", "status"]):
            try:
                info = proc.info
                name = info.get("name") or ""
                if filter_name and filter_name.lower() not in name.lower():
                    continue
                mem = info.get("memory_info")
                mem_mb = round(mem.rss / (1024 * 1024), 1) if mem else 0
                results.append({
                    "pid": info["pid"],
                    "name": name,
                    "memory_mb": mem_mb,
                    "status": info.get("status", ""),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        results.sort(key=lambda p: p["memory_mb"], reverse=True)
        _audit.log("controller.list_processes", data={"filter": filter_name, "count": len(results)})
        return json.dumps({"status": "ok", "count": len(results), "processes": results[:50]})

    except ImportError:
        return json.dumps({"status": "error", "error": "psutil not installed. Run: pip install psutil"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _ctrl_screenshot_region(x: int, y: int, width: int, height: int) -> str:
    """Capture a specific region of the screen."""
    dec = _gate(ActionType.READ)
    if dec.blocked:
        return _blocked_result(dec)

    if width <= 0 or height <= 0:
        return json.dumps({"status": "error", "error": "width and height must be > 0."})

    try:
        import mss
        import mss.tools

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        filepath = configured_screenshots_dir() / f"region_{timestamp}.png"

        monitor = {"left": x, "top": y, "width": width, "height": height}
        with mss.mss() as sct:
            img = sct.grab(monitor)
            mss.tools.to_png(img.rgb, img.size, output=str(filepath))

        size_kb = round(filepath.stat().st_size / 1024, 1)
        _audit.log("controller.screenshot_region", data={"region": monitor, "path": str(filepath)})
        return json.dumps({
            "status": "ok",
            "path": str(filepath),
            "region": {"x": x, "y": y, "width": width, "height": height},
            "size_kb": size_kb,
        })

    except ImportError:
        return json.dumps({"status": "error", "error": "mss not installed. Run: pip install mss"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _ctrl_screenshot_window(title: str = "", hwnd: int = 0) -> str:
    """Capture a visible window by exact hwnd or title match."""
    dec = _gate(ActionType.READ)
    if dec.blocked:
        return _blocked_result(dec)

    window_result = get_window_rect(title=title, hwnd=int(hwnd or 0))
    if window_result.get("status") != "ok":
        return json.dumps(window_result, ensure_ascii=False)

    window = window_result["window"]
    rect = dict(window.get("rect") or {})
    width = int(rect.get("width", 0))
    height = int(rect.get("height", 0))
    if width <= 0 or height <= 0:
        return json.dumps(
            {
                "status": "error",
                "error": "Matched window has no capturable area.",
                "window": window,
            },
            ensure_ascii=False,
        )

    raw = _ctrl_screenshot_region(
        int(rect.get("x", 0)),
        int(rect.get("y", 0)),
        width,
        height,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(payload, dict):
        payload["mode"] = "window"
        payload["window"] = window
        payload["coordinate_origin"] = {
            "x": int(rect.get("x", 0)),
            "y": int(rect.get("y", 0)),
        }
        payload["coordinate_hint"] = (
            "For window screenshot pixels, absolute_x = coordinate_origin.x + image_x "
            "and absolute_y = coordinate_origin.y + image_y."
        )
    return json.dumps(payload, ensure_ascii=False)


# ── Build tools ───────────────────────────────────────────────────────────────


# ── Resume executors (bypass gate – approval already granted) ──────────────────

def _resume_create_folder(input_str: str) -> str:
    args = _safe_json(input_str)
    return _cmd_create_folder(args.get("path", input_str.strip()), _bypass_gate=True)

def _resume_move(input_str: str) -> str:
    args = _safe_json(input_str)
    return _cmd_move_item(args["source"], args["destination"], _bypass_gate=True)

def _resume_copy(input_str: str) -> str:
    args = _safe_json(input_str)
    return _cmd_copy_item(args["source"], args["destination"], _bypass_gate=True)

def _resume_rename(input_str: str) -> str:
    args = _safe_json(input_str)
    return _cmd_rename_item(args["path"], args["new_name"], _bypass_gate=True)

def _resume_delete(input_str: str) -> str:
    args = _safe_json(input_str)
    return _cmd_delete_item(args.get("path", input_str.strip()), _bypass_gate=True)

def _resume_interact_app(input_str: str) -> str:
    args = _safe_json(input_str)
    return _uia_interact_app(args["app"], args["action"], args.get("params", {}), _bypass_gate=True)

def _resume_screen_click(input_str: str) -> str:
    args = _safe_json(input_str)
    if int(args.get("clicks", 1)) >= 2:
        return _ctrl_double_click(
            int(args["x"]),
            int(args["y"]),
            args.get("button", "left"),
            _bypass_gate=True,
        )
    return _screen_click(int(args["x"]), int(args["y"]), args.get("button", "left"), _bypass_gate=True)

def _resume_precision_click(input_str: str) -> str:
    args = _safe_json(input_str)
    return _precision_click(
        float(args["x"]),
        float(args["y"]),
        args.get("button", "left"),
        int(args.get("clicks", 1)),
        args.get("coordinate_mode", "absolute"),
        int(args.get("origin_x", 0)),
        int(args.get("origin_y", 0)),
        int(args.get("width", 0)),
        int(args.get("height", 0)),
        int(args.get("monitor", 0)),
        args.get("title", ""),
        int(args.get("hwnd", 0)),
        _json_bool(args.get("verify_bounds", True), True),
        _bypass_gate=True,
    )

def _resume_screen_type(input_str: str) -> str:
    args = _safe_json(input_str)
    target_hwnd = int(args.get("target_hwnd") or 0)
    if not target_hwnd:
        return json.dumps(
            {
                "status": "error",
                "reason_code": "target_window_missing",
                "error": (
                    "Approved keyboard text input did not include a target window. "
                    "Refusing to replay global keyboard input after approval."
                ),
                "typed_chars": len(args.get("text", input_str.strip())),
            }
        )
    return _screen_type(
        args.get("text", input_str.strip()),
        target_hwnd=target_hwnd,
        target_title=args.get("target_title", ""),
        target_process_name=args.get("target_process_name", ""),
        _bypass_gate=True,
    )

def _resume_hotkey(input_str: str) -> str:
    args = _safe_json(input_str)
    target_hwnd = int(args.get("target_hwnd") or 0)
    if not target_hwnd:
        return json.dumps(
            {
                "status": "error",
                "reason_code": "target_window_missing",
                "error": (
                    "Approved keyboard shortcut did not include a target window. "
                    "Refusing to replay global keyboard input after approval."
                ),
                "keys": args.get("keys", input_str.strip()),
            }
        )
    return _ctrl_hotkey(
        args.get("keys", input_str.strip()),
        target_hwnd=target_hwnd,
        target_title=args.get("target_title", ""),
        target_process_name=args.get("target_process_name", ""),
        _bypass_gate=True,
    )

def _resume_window_action(input_str: str) -> str:
    args = _safe_json(input_str)
    return _ctrl_window_action(
        args.get("title", ""),
        args["action"],
        int(args.get("width", 0)),
        int(args.get("height", 0)),
        int(args.get("x", 0)),
        int(args.get("y", 0)),
        int(args.get("hwnd", 0)),
        _bypass_gate=True,
    )

def _resume_scroll(input_str: str) -> str:
    args = _safe_json(input_str)
    return _ctrl_scroll(
        args.get("direction", "down"),
        int(args.get("clicks", args.get("amount", 3))),
        int(args.get("x", -1)),
        int(args.get("y", -1)),
        _bypass_gate=True,
    )

def _resume_drag(input_str: str) -> str:
    args = _safe_json(input_str)
    return _ctrl_drag(
        int(args["from_x"]),
        int(args["from_y"]),
        int(args["to_x"]),
        int(args["to_y"]),
        args.get("button", "left"),
        _bypass_gate=True,
    )

register_executor("ctrl_create_folder", _resume_create_folder)
register_executor("ctrl_move", _resume_move)
register_executor("ctrl_copy", _resume_copy)
register_executor("ctrl_rename", _resume_rename)
register_executor("ctrl_delete", _resume_delete)
register_executor("ctrl_interact_app", _resume_interact_app)
register_executor("ctrl_screen_click", _resume_screen_click)
register_executor("precision_click", _resume_precision_click)
register_executor("ctrl_screen_type", _resume_screen_type)
register_executor("hotkey", _resume_hotkey)
register_executor("window_action", _resume_window_action)
register_executor("scroll", _resume_scroll)
register_executor("drag", _resume_drag)


def _resume_clipboard_write(input_str: str) -> str:
    args = _safe_json(input_str)
    return _ctrl_clipboard(
        action=args.get("action", "write"),
        text=args.get("text", input_str.strip()),
        _bypass_gate=True,
    )


register_executor("ctrl_clipboard", _resume_clipboard_write)


# ── Build tools ───────────────────────────────────────────────────────────────

