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
from functools import wraps

from app.agent.output_workspace import configured_screenshots_dir
from app.agent.controller_policy import (
    ActionType,
    PermissionDecision,
    PermissionMode,
    canonical,
    get_effective_app_rule,
    is_app_allowed,
    is_blocked_process_alias,
    is_path_permitted,
    is_screen_fallback_allowed,
    load_policy,
    resolve_permission,
)
from app.agent.audit import AuditLogger
from app.agent.access_grant_broker import create_grant_ticket
from app.agent.approval_broker import create_ticket as _create_approval_ticket
from app.agent.execution_resume import register_executor
from app.skills.computer_use.screen_geometry import get_window_rect, map_precision_coordinates, physical_pixels
from app.skills.computer_use.screen_redaction import redact_capture
from app.skills.computer_use.input_safety import text_entry_error
from app.agent.tool_cancellation import check_cancelled, cancellable_sleep, ToolCancelled

_audit = AuditLogger()

_CTRL_V_KEY_DELAY_SECONDS = 0.05


def _uia_worker(fn):
    """UI Automation needs COM initialized on each native worker thread."""
    @wraps(fn)
    def run(*args, **kwargs):
        try:
            import pythoncom
        except ImportError:
            return fn(*args, **kwargs)
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        try:
            return fn(*args, **kwargs)
        finally:
            pythoncom.CoUninitialize()
    return run


def _cancelled_result(exc: ToolCancelled, **progress: Any) -> dict:
    return {"status": "cancelled", "reason_code": exc.reason_code, "error": str(exc), **progress}


def _read_control_value(element) -> str | None:
    """None means unreadable; an empty string is a readable empty field."""
    for read in (lambda: element.get_value(), lambda: element.iface_value.CurrentValue,
                 lambda: element.iface_text.DocumentRange.GetText(-1)):
        try:
            value = read()
            return str(value) if value is not None else None
        except Exception:
            pass
    return None


def _expected_typed_value(element, before: str | None, text: str) -> str | None:
    """Typing replaces the current selection; do not assume the caret is at end."""
    if before is None:
        return None
    if not before:
        return text
    try:
        pattern = element.iface_text
        selections = pattern.GetSelection()
        if selections.Length != 1:
            return None
        selection = selections.GetElement(0)
        prefix, suffix = pattern.DocumentRange.Clone(), pattern.DocumentRange.Clone()
        prefix.MoveEndpointByRange(1, selection, 0)  # end -> selection start
        suffix.MoveEndpointByRange(0, selection, 1)  # start -> selection end
        left, selected, right = prefix.GetText(-1), selection.GetText(-1), suffix.GetText(-1)
        normalize = lambda value: value.replace("\r\n", "\n").replace("\r", "\n")
        if normalize(left + selected + right) == normalize(before):
            return left + text + right
    except Exception:
        pass
    return None


@_uia_worker
def _focused_control():
    try:
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo
        from pywinauto.controls.uiawrapper import UIAWrapper
        raw = IUIA().iuia.GetFocusedElement()
        return UIAWrapper(UIAElementInfo(raw)) if raw else None
    except Exception:
        return None


@_uia_worker
def _focused_text_entry_error(text: str) -> dict | None:
    # Keep the COM apartment alive while inspecting returned native properties.
    control = _focused_control()
    if control is None:
        return {"status": "blocked", "reason_code": "input_target_unavailable",
                "error": "The focused control could not be inspected before text entry."}
    return text_entry_error(text, _element_summary(control, 0))


def _input_target_error(target_hwnd: int = 0, control=None) -> str | None:
    context = _foreground_window_context()
    if target_hwnd and int(context.get("target_hwnd") or 0) != int(target_hwnd):
        return "Target window focus changed during input."
    if control is not None:
        current = _focused_control()
        if current is None:
            return "The focused text control could no longer be verified."
        before = _element_summary(control, 0).get("runtime_id")
        after = _element_summary(current, 0).get("runtime_id")
        if not before or before != after:
            return "The focused text control changed during input."
    return _keyboard_target_error()


def _mouse_target_error(target_hwnd: int, x: int | None = None, y: int | None = None) -> str | None:
    if not target_hwnd:
        return None
    try:
        import win32gui
        if int(win32gui.GetForegroundWindow()) != target_hwnd:
            return "Target window focus changed before pointer input."
        if x is not None and y is not None:
            hit = win32gui.WindowFromPoint((x, y))
            root = int(win32gui.GetAncestor(hit, 2))  # GA_ROOT
            if root != target_hwnd and not _is_target_popup(win32gui, root, target_hwnd):
                return "The intended point is covered by another window."
    except Exception:
        return "The pointer target could not be verified."
    return None


def _window_pid(hwnd: int) -> int:
    try:
        import win32process
        return int(win32process.GetWindowThreadProcessId(hwnd)[1] or 0)
    except Exception:
        return 0


def _is_target_popup(win32gui, root: int, target_hwnd: int) -> bool:
    """Menus, dropdowns and other popups the target opened are not occluders."""
    get_window = getattr(win32gui, "GetWindow", None)
    owner = root
    for _ in range(8):
        owner = int(get_window(owner, 4) or 0) if get_window else 0  # GW_OWNER
        if not owner:
            break
        if owner == target_hwnd:
            return True
    # Unowned popups such as #32768 context menus: same process and WS_POPUP,
    # never another top-level window of the same app.
    get_style = getattr(win32gui, "GetWindowLong", None)
    style = int(get_style(root, -16) or 0) if get_style else 0  # GWL_STYLE
    target_pid = _window_pid(target_hwnd)
    return bool(style & 0x80000000) and bool(target_pid) and _window_pid(root) == target_pid

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


def _paste_text_via_clipboard(text: str, *, target_hwnd: int = 0, control=None) -> dict[str, Any]:
    """Paste text into the currently focused control and restore prior text clipboard content."""
    import win32api
    import win32clipboard
    import win32con

    check_cancelled()
    had_clipboard_text = False
    previous_text = ""
    temporary_sequence = None
    win32clipboard.OpenClipboard()
    try:
        sensitive = _sensitive_clipboard_reason(win32clipboard)
        if sensitive:
            return {"status": "blocked", "reason_code": "sensitive_clipboard", "error": sensitive}
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
        if hasattr(win32clipboard, "GetClipboardSequenceNumber"):
            temporary_sequence = win32clipboard.GetClipboardSequenceNumber()
    finally:
        win32clipboard.CloseClipboard()

    ctrl_vk = getattr(win32con, "VK_CONTROL", 0x11)
    v_vk = win32api.VkKeyScan("v") & 0xFF
    clipboard_restored = False
    pressed = []
    try:
        check_cancelled()
        target_error = _input_target_error(target_hwnd, control)
        if target_error:
            return {"status": "blocked", "reason_code": "target_focus_changed", "error": target_error}
        for vk in (ctrl_vk, v_vk):
            check_cancelled()
            win32api.keybd_event(vk, 0, 0, 0)
            pressed.append(vk)
        for vk in reversed(pressed):
            win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        pressed.clear()
        cancellable_sleep(_CTRL_V_KEY_DELAY_SECONDS)
    finally:
        for vk in reversed(pressed):
            win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        try:
            win32clipboard.OpenClipboard()
            try:
                # Never overwrite something the user copied while the paste ran.
                if temporary_sequence is None or win32clipboard.GetClipboardSequenceNumber() == temporary_sequence:
                    win32clipboard.EmptyClipboard()
                    if had_clipboard_text:
                        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, previous_text)
                    clipboard_restored = True
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            # Cleanup failure must not mask Stop or repeat an already sent paste.
            clipboard_restored = False

    return {
        "status": "ok",
        "typed_chars": len(text),
        "method": "paste",
        "clipboard_restored": clipboard_restored,
    }


def _type_text_via_keyboard(text: str, *, target_hwnd: int = 0, control=None) -> dict[str, Any]:
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

    # Validate the entire layout before typing. A late unsupported character
    # must not paste the whole string over an already typed prefix.
    prepared = []
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
                    "typed_chars": 0,
                }
            code = vk & 0xFF
            shift_state = (vk >> 8) & 0xFF
            modifiers = [modifier for bit, modifier in modifier_keys if shift_state & bit]
        prepared.append((code, modifiers))

    typed_chars = 0
    try:
        for code, modifiers in prepared:
            check_cancelled()
            target_error = _input_target_error(target_hwnd, control)
            if target_error:
                return {"status": "blocked", "reason_code": "target_focus_changed", "error": target_error, "typed_chars": typed_chars}
            pressed = []
            try:
                for vk in [*modifiers, code]:
                    check_cancelled()
                    win32api.keybd_event(vk, 0, 0, 0)
                    pressed.append(vk)
            finally:
                for vk in reversed(pressed):
                    win32api.keybd_event(vk, 0, keyup, 0)
            typed_chars += 1
            cancellable_sleep(0.01)
    except ToolCancelled as exc:
        return _cancelled_result(exc, typed_chars=typed_chars)

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
    requested_access: str = "read",
) -> str:
    """Create an access-grant ticket and return pending_access_grant JSON.

    Policy gate order: blocked → requires_access_grant → requires_confirmation → execute.
    """
    ticket = create_grant_ticket(
        target_type=target_type,
        target_identifier=target_identifier,
        display_name=display_name or target_identifier,
        action_context=action_context or decision.reason,
        requested_access=requested_access,
    )
    return json.dumps({
        "status": "pending_access_grant",
        "ticket_id": ticket.id,
        "target_type": ticket.target_type,
        "target_identifier": ticket.target_identifier,
        "display_name": ticket.display_name,
        "action_context": ticket.action_context,
        "requested_access": ticket.requested_access,
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
            requested_access="launch" if action == ActionType.LAUNCH_APP else "read",
        )
    if decision.requires_access_grant and target_path:
        return _pending_access_grant_result(
            decision,
            target_type="path",
            target_identifier=target_path,
            display_name=target_path,
            action_context=action_desc,
            requested_access=(
                "delete" if action == ActionType.DELETE
                else "write" if action in {ActionType.MUTATE, ActionType.EXEC, ActionType.CLICK, ActionType.TYPE}
                else "read"
            ),
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
            requested_access="read",
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
                requested_access="write",
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
                requested_access="read",
            )
        if dec2.requires_access_grant:
            return _pending_access_grant_result(
                dec2,
                target_type="path",
                target_identifier=destination,
                display_name=destination,
                action_context=f"Move (destination): {source} → {destination}",
                requested_access="write",
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
                requested_access="read",
            )
        if dec2.requires_access_grant:
            return _pending_access_grant_result(
                dec2,
                target_type="path",
                target_identifier=destination,
                display_name=destination,
                action_context=f"Copy (destination): {source} → {destination}",
                requested_access="write",
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
                requested_access="write",
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
            requested_access="write",
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
                requested_access="delete",
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
    try:
        payload["runtime_id"] = list(info.runtime_id)
    except Exception:
        try:
            payload["runtime_id"] = list(info.element.GetRuntimeId())
        except Exception:
            pass
    try:
        payload["is_password"] = bool(info.element.CurrentIsPassword)
    except Exception:
        payload["is_password"] = bool(getattr(info, "is_password", False))
    try:
        payload["enabled"] = bool(element.is_enabled())
    except Exception:
        pass
    if not text_entry_error("", payload):
        value = _read_control_value(element)
        if value is not None:
            payload["value"] = value[:4000]
    rect = _element_rect(element)
    if rect:
        payload["bounds"] = {key: rect[key] for key in ("x", "y", "width", "height", "right", "bottom")}
        payload["center"] = {"x": rect["center_x"], "y": rect["center_y"]}
    return payload


def _verify_control_value(element, expected: str, *, timeout: float = 1.0) -> dict:
    deadline = time.monotonic() + timeout
    actual = None
    normalize = lambda value: value.replace("\r\n", "\n").replace("\r", "\n")
    while True:
        check_cancelled()
        actual = _read_control_value(element)
        if actual is not None and normalize(actual) == normalize(expected):
            return {"verified": True, "expected": expected, "actual": actual}
        if actual is None or time.monotonic() >= deadline:
            return {"verified": False, "reason_code": "verification_unavailable" if actual is None else "verification_failed",
                    "expected": expected, "actual": actual,
                    "instruction": "Inspect the current state before retrying; the input may already have been applied."}
        cancellable_sleep(0.05)


def _selected_control_text(element) -> str | None:
    try:
        return str(element.selected_text())
    except Exception:
        try:
            selected = element.get_selection()
            if isinstance(selected, list) and len(selected) == 1:
                return str(selected[0].window_text())
        except Exception:
            pass
    return _read_control_value(element)


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
            requested_access="read",
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


def _uia_window_process_name(window: Any) -> str:
    try:
        import psutil

        return str(psutil.Process(int(window.process_id())).name() or "")
    except Exception:
        return ""


@physical_pixels
@_uia_worker
def _uia_interact_app(alias: str, action: str, params: dict[str, Any], *, _bypass_gate: bool = False) -> str:
    """Interact with an allowlisted application via UIA."""
    if not _bypass_gate:
        permission_action = (ActionType.READ if action in {"get_elements", "read_element", "wait_element"}
                             else ActionType.TYPE if action in {"type_element", "set_value", "type_text"} else ActionType.CLICK)
        dec = _gate(permission_action, target_app=alias)
        if dec.blocked:
            return _blocked_result(dec)
        if dec.requires_access_grant:
            return _pending_access_grant_result(
                dec,
                target_type="app",
                target_identifier=alias,
                display_name=alias,
                action_context=f"UIA interact with {alias}: {action}",
                requested_access="read",
            )
        if dec.requires_confirmation:
            return _pending_approval_result(
                dec, f"UIA interact with {alias}: {action}",
                tool_name="ctrl_interact_app", action_type=permission_action.value,
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
        check_cancelled()
        from pywinauto import Desktop

        desktop = Desktop(backend="uia")
        windows = desktop.windows()
        query_title = str(params.get("window_title") or params.get("title") or alias).strip().lower()
        alias_query = alias.lower()
        matches = []
        exact_hwnd = int(params.get("hwnd") or 0)
        for w in windows:
            title = w.window_text().lower()
            matched = int(w.handle or 0) == exact_hwnd if exact_hwnd else query_title in title or alias_query in title
            if matched:
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
        # The gate checked the alias, but the window was matched by a
        # caller-controlled title substring; verify the real owning process.
        target_process = _uia_window_process_name(target)
        matched_window["process_name"] = target_process
        if target_process and is_blocked_process_alias(target_process):
            return json.dumps({
                "status": "blocked",
                "reason": f"Window '{matched_window['title']}' belongs to blocked process '{target_process}'.",
                "reason_code": "blocked_process",
                "policy_source": "settings.json",
                "matched_window": matched_window,
            })
        if (
            action in {"type_element", "set_value", "type_text"}
            and target_process.lower() in _SHELL_INPUT_PROCESSES
        ):
            return json.dumps({
                "status": "blocked",
                "reason": f"Text entry into the Windows shell ('{target_process}') can launch programs.",
                "reason_code": "keyboard_target_blocked",
                "policy_source": "settings.json",
                "matched_window": matched_window,
            })

        def _matched_elements() -> tuple[list[Any], int]:
            element_name = str(params.get("element", params.get("name", ""))).strip()
            automation_id = str(params.get("automation_id", "")).strip()
            control_type = str(params.get("control_type", "")).strip()
            occurrence = int(params.get("occurrence", 0) or 0)
            if not element_name and not automation_id and not params.get("runtime_id") and action not in {"get_elements", "wait_element"}:
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
                and (not params.get("runtime_id") or _element_summary(child, 0).get("runtime_id") == params["runtime_id"])
                and (not params.get("bounds") or _element_summary(child, 0).get("bounds") == params["bounds"])
            ]
            return candidates, occurrence

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

        elif action in {"read_element", "type_element", "set_value", "invoke_element", "secondary_action", "select_option", "wait_element"}:
            import time as _time

            timeout = max(0.1, min(float(params.get("timeout_seconds", 5) or 5), 60.0))
            deadline = _time.monotonic() + timeout
            candidates: list[Any] = []
            occurrence = int(params.get("occurrence", 0) or 0)
            while _time.monotonic() <= deadline:
                check_cancelled()
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
                cancellable_sleep(0.2)

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
            # Only text entry and value reads touch the secret itself; invoking
            # a "Forgot password" link or "Show password" toggle does not.
            if action in {"read_element", "wait_element", "type_element", "set_value"} and (
                    summary.get("is_password") or text_entry_error("", summary)):
                return json.dumps({"status": "blocked", "reason_code": "credential_entry_restricted", "error": "Sensitive controls require direct local user control."})
            check_cancelled()
            if action in {"type_element", "set_value", "invoke_element", "secondary_action", "select_option"}:
                target_error = _mouse_target_error(int(target.handle))
                if target_error:
                    return json.dumps({"status": "blocked", "reason_code": "target_focus_changed", "error": target_error})
            value_before = _read_control_value(child)
            if action in {"type_element", "set_value"}:
                text = str(params.get("text", ""))
                entry_error = text_entry_error(text, {**summary, **{key: params[key] for key in ("label", "field", "description") if key in params}})
                if entry_error:
                    return json.dumps(entry_error)
                clear = bool(params.get("clear", True))
                if not clear and value_before is None:
                    return json.dumps({"status": "error", "reason_code": "verification_unavailable", "error": "The existing text cannot be read; append could not be verified."})
                paste_meta: dict[str, Any] = {}
                try:
                    if clear:
                        check_cancelled()
                        child.set_edit_text(text)
                        method = "uia_value"
                    else:
                        check_cancelled()
                        child.click_input()
                        # The click leaves the caret at the click point; append
                        # means the end of the existing text.
                        moved = _safe_json(_ctrl_hotkey("ctrl+end", target_hwnd=int(target.handle), _bypass_gate=True))
                        if moved.get("status") != "ok":
                            return json.dumps(moved)
                        paste_meta = _paste_text_via_clipboard(text, target_hwnd=int(target.handle), control=child)
                        method = "uia_paste" if paste_meta.get("status") == "ok" else "uia_keys"
                        if paste_meta.get("status") != "ok":
                            if paste_meta.get("status") in {"blocked", "cancelled"}:
                                return json.dumps(paste_meta)
                            paste_meta = _type_text_via_keyboard(text, target_hwnd=int(target.handle), control=child)
                            if paste_meta.get("status") != "ok":
                                return json.dumps(paste_meta)
                except ToolCancelled:
                    raise
                except Exception as exc:
                    # A setter that changed the value before failing must not be
                    # retried through a second input method. A missing pattern
                    # means no write was attempted, even if the value is unreadable.
                    from pywinauto.uia_defines import NoPatternInterfaceError
                    if (not isinstance(exc, NoPatternInterfaceError) or not clear
                            or (value_before is not None and _read_control_value(child) != value_before)):
                        return json.dumps({"status": "error", "reason_code": "action_completion_unknown", "error": "Text setter failed after a possible change. Inspect before retrying."})
                    check_cancelled()
                    child.click_input()
                    if clear:
                        for chord in ("ctrl+a", "backspace"):
                            result = _safe_json(_ctrl_hotkey(chord, target_hwnd=int(target.handle), _bypass_gate=True))
                            if result.get("status") != "ok":
                                return json.dumps(result)
                    paste_meta = _paste_text_via_clipboard(text, target_hwnd=int(target.handle), control=child)
                    method = "uia_paste" if paste_meta.get("status") == "ok" else "uia_keys"
                    if paste_meta.get("status") != "ok":
                        if paste_meta.get("status") in {"blocked", "cancelled"}:
                            return json.dumps(paste_meta)
                        paste_meta = _type_text_via_keyboard(text, target_hwnd=int(target.handle), control=child)
                        if paste_meta.get("status") != "ok":
                            return json.dumps(paste_meta)
                verification = _verify_control_value(child, text if clear else value_before + text)
                mismatch = verification.get("reason_code") == "verification_failed"
                payload = {
                    "status": "error" if mismatch else "ok",
                    "verification": verification,
                    "app": alias,
                    "action": action,
                    "method": method,
                    "target": summary,
                    "value_before": value_before,
                    "value_after": verification["actual"],
                    "matched_window": matched_window,
                    "policy_source": "settings.json",
                }
                # An unreadable control is input sent but unverified, not a
                # failure; only a readable mismatch is an error.
                if mismatch:
                    payload.update({"reason_code": verification["reason_code"], "error": "Text entry could not be verified. Inspect before retrying."})
                if paste_meta:
                    payload["paste"] = paste_meta
                return json.dumps(payload)
            if action in {"invoke_element", "secondary_action"}:
                try:
                    check_cancelled()
                    if action == "secondary_action":
                        center = summary.get("center")
                        if not center:
                            return json.dumps({"status": "error", "reason_code": "element_bounds_missing"})
                        return _screen_click(center["x"], center["y"], "right", target_hwnd=int(target.handle), _bypass_gate=True)
                    child.invoke()
                    method = "uia_invoke"
                except ToolCancelled:
                    raise
                except Exception as exc:
                    return json.dumps({"status": "error", "reason_code": "action_completion_unknown", "error": str(exc), "instruction": "Inspect current state before retrying; invocation may have completed."})
                return json.dumps({
                    "status": "ok",
                    "app": alias,
                    "action": action,
                    "method": method,
                    "verification": {"verified": False, "status": "input_sent", "instruction": "Inspect the resulting window or supply an explicit expected outcome."},
                    "target": summary,
                    "matched_window": matched_window,
                    "policy_source": "settings.json",
                })
            if action == "select_option":
                option = str(params.get("option", ""))
                check_cancelled()
                try:
                    child.select(option)
                    method = "uia_select"
                except ToolCancelled:
                    raise
                except Exception as exc:
                    return json.dumps({"status": "error", "reason_code": "selection_failed", "error": str(exc), "instruction": "Inspect before retrying. Clicking the control alone does not select an option."})
                deadline = time.monotonic() + 1.0
                while True:
                    check_cancelled()
                    selected = _selected_control_text(child)
                    if selected is None or selected == option or time.monotonic() >= deadline:
                        break
                    cancellable_sleep(0.05)
                return json.dumps({
                    "status": "ok" if selected == option else "error",
                    "reason_code": "" if selected == option else "verification_unavailable" if selected is None else "verification_failed",
                    "verification": {"verified": selected == option, "expected": option, "actual": selected},
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
                "selected": _selected_control_text(child),
                "matched_window": matched_window,
                "policy_source": "settings.json",
            })

        elif action == "type_text":
            text = params.get("text", "")
            entry_error = text_entry_error(str(text), _element_summary(target, 0))
            if entry_error:
                return json.dumps(entry_error)
            check_cancelled()
            target.click_input()
            return _screen_type(str(text), target_hwnd=int(target.handle), _bypass_gate=True)

        elif action == "get_elements":
            elements = []
            limit = max(1, min(int(params.get("limit", 80) or 80), 200))
            name_filter = str(params.get("contains", params.get("element", ""))).strip().lower()
            control_type_filter = str(params.get("control_type", "")).strip().lower()
            for child in target.descendants():
                check_cancelled()
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
                "limit": limit,
                "method": "uia",
                "matched_window": matched_window,
                "coordinate_hint": "Use element.center.x and element.center.y with computer_functions_act, or prefer a fresh state_id UIA action.",
                "policy_source": "settings.json",
            })

        return json.dumps({
            "status": "error",
            "error": f"Unknown UIA action: {action}",
            "reason_code": "unknown_action",
            "policy_source": "settings.json",
            "matched_window": matched_window,
        })

    except ToolCancelled as exc:
        return json.dumps(_cancelled_result(exc))
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "reason_code": "uia_error",
            "policy_source": "settings.json",
        })


# ── Strategy 3: Coordinate click fallback ─────────────────────────────────────


@physical_pixels
def _screen_click(x: int, y: int, button: str = "left", *, target_hwnd: int = 0, _bypass_gate: bool = False) -> str:
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
            tool_name="computer_functions_screen_click",
            action_type="click",
            input_str=json.dumps({"x": x, "y": y, "button": button}),
            payload_args={"x": x, "y": y, "button": button},
        )
        if pending is not None:
            return pending

    try:
        check_cancelled()
        ctypes.windll.user32.SetCursorPos(x, y)
        cancellable_sleep(0.05)
        target_error = _mouse_target_error(target_hwnd, x, y)
        if target_error:
            return json.dumps({"status": "blocked", "reason_code": "target_focus_changed", "error": target_error})

        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        MOUSEEVENTF_RIGHTDOWN = 0x0008
        MOUSEEVENTF_RIGHTUP = 0x0010

        down = MOUSEEVENTF_RIGHTDOWN if button == "right" else MOUSEEVENTF_LEFTDOWN
        up = MOUSEEVENTF_RIGHTUP if button == "right" else MOUSEEVENTF_LEFTUP
        check_cancelled()
        ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
        try:
            cancellable_sleep(0.02)
        finally:
            ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)

        _audit.log("controller.screen_click", data={"x": x, "y": y, "button": button})
        return json.dumps({"status": "ok", "x": x, "y": y, "button": button, "method": "click"})

    except ToolCancelled as exc:
        return json.dumps(_cancelled_result(exc))
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
            tool_name="computer_functions_precision_click",
            action_type="click",
            input_str=json.dumps(payload_args),
            payload_args=payload_args,
        )
        if pending is not None:
            return pending

    click_count = max(1, int(clicks or 1))
    if click_count >= 2:
        click_result = json.loads(
            _ctrl_double_click(int(mapped["x"]), int(mapped["y"]), button, target_hwnd=hwnd, _bypass_gate=True)
        )
    else:
        click_result = json.loads(_screen_click(int(mapped["x"]), int(mapped["y"]), button, target_hwnd=hwnd, _bypass_gate=True))
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


@_uia_worker
def _screen_type(
    text: str,
    *,
    target_hwnd: int = 0,
    target_title: str = "",
    target_process_name: str = "",
    _restore_focus: bool = True,
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
            tool_name="computer_functions_screen_type",
            action_type="type",
            input_str=json.dumps(payload_args, ensure_ascii=False, sort_keys=True),
            payload_args=payload_args,
        )
        if pending is not None:
            return pending

    try:
        check_cancelled()
        focus_error = _focus_target_window(
            target_hwnd=target_hwnd,
            target_title=target_title,
            target_process_name=target_process_name,
        ) if _restore_focus else _input_target_error(target_hwnd)
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
        target_error = _keyboard_target_error()
        if target_error:
            return _keyboard_blocked_result(target_error)

        control = _focused_control()
        if control is None:
            return json.dumps({"status": "blocked", "reason_code": "input_target_unavailable", "error": "The focused control could not be inspected before text entry."})
        entry_error = text_entry_error(text, _element_summary(control, 0) if control is not None else {})
        if entry_error:
            return json.dumps(entry_error)
        before = _read_control_value(control) if control is not None else None
        expected = _expected_typed_value(control, before, text)
        type_meta = _type_text_via_keyboard(text, target_hwnd=target_hwnd, control=control)
        if type_meta.get("status") == "error" and type_meta.get("reason_code") == "unsupported_keyboard_character":
            type_meta = _paste_text_via_clipboard(text, target_hwnd=target_hwnd, control=control)
        if type_meta.get("status") != "ok":
            return json.dumps(type_meta, ensure_ascii=False)
        verification = (_verify_control_value(control, expected) if expected is not None
                        else {"verified": False, "reason_code": "verification_unavailable", "actual": None,
                              "instruction": "Input was sent; inspect the resulting state to confirm it."})
        type_meta["verification"] = verification
        # Terminals, canvases and fields without a readable value can't be
        # verified; that is input sent, not a failure. Only a mismatch errors.
        if verification.get("reason_code") == "verification_failed":
            type_meta.update({"status": "error", "reason_code": verification["reason_code"],
                              "error": "Input was sent but the resulting text could not be verified. Inspect before retrying."})
        _audit.log("controller.screen_type", data={"text_len": len(text), "method": type_meta.get("method", "type")})
        return json.dumps(type_meta, ensure_ascii=False)

    except ToolCancelled as exc:
        return json.dumps(_cancelled_result(exc, typed_chars=(locals().get("type_meta") or {}).get("typed_chars", 0)))
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


# Keyboard input to the Windows shell is a command line: the Explorer address
# bar, Run dialog, and Start/Search boxes launch whatever is typed, bypassing
# the exec gate and the blocked-process list. Clicks there remain allowed.
_SHELL_INPUT_PROCESSES = frozenset({
    "explorer.exe",
    "searchhost.exe",
    "searchapp.exe",
    "searchui.exe",
    "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
})
# Chords handled by the shell itself rather than the focused window.
_SHELL_HOTKEY_MODIFIERS = frozenset({"win", "windows", "lwin", "rwin"})
_FILE_DIALOG_CHILD_CLASSES = frozenset({"DUIViewWndClassName", "SHELLDLL_DefView", "DirectUIHWND"})


def _shell_hotkey_error(keys: str) -> str | None:
    parts = {part.strip().lower() for part in str(keys or "").split("+") if part.strip()}
    if parts & _SHELL_HOTKEY_MODIFIERS:
        return "Windows-key shortcuts are handled by the shell (Run, Start, admin menu) and are not allowed."
    if {"ctrl", "esc"} <= parts or {"control", "esc"} <= parts or {"ctrl", "escape"} <= parts:
        return "Ctrl+Esc opens the Start menu and is not allowed."
    return None


def _is_shell_file_dialog(win32gui, hwnd: int) -> bool:
    """Detect common Open/Save dialogs, where a typed path bypasses path policy."""
    try:
        if win32gui.GetClassName(hwnd) != "#32770":
            return False
        child_classes: set[str] = set()

        def _collect(child, _):
            try:
                child_classes.add(str(win32gui.GetClassName(child) or ""))
            except Exception:
                pass
            return True

        win32gui.EnumChildWindows(hwnd, _collect, None)
        return bool(child_classes & _FILE_DIALOG_CHILD_CLASSES)
    except Exception:
        return False


def _keyboard_target_error() -> str | None:
    """Re-check the window that will actually receive keystrokes, right before sending."""
    try:
        import win32gui
    except ImportError:
        return None
    context = _foreground_window_context()
    process_name = str(context.get("target_process_name") or "").lower()
    if process_name and is_blocked_process_alias(process_name):
        return f"Keyboard input to blocked process '{process_name}' is not allowed."
    if process_name in _SHELL_INPUT_PROCESSES:
        return (
            f"Keyboard input to the Windows shell ('{process_name}') can launch programs and is not allowed; "
            "use the filesystem or launch tools instead."
        )
    hwnd = int(context.get("target_hwnd") or 0)
    if hwnd and _is_shell_file_dialog(win32gui, hwnd):
        return "Typing into a file Open/Save dialog bypasses path permissions and is not allowed."
    return None


def _keyboard_blocked_result(error: str, **extra: Any) -> str:
    return json.dumps(
        {"status": "blocked", "reason_code": "keyboard_target_blocked", "error": error, **extra},
        ensure_ascii=False,
    )


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
        check_cancelled()
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

        if int(win32gui.GetForegroundWindow()) == hwnd:
            return None
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

        cancellable_sleep(0.1)
        foreground = int(win32gui.GetForegroundWindow())
        if foreground != hwnd:
            return (
                f"Target window was not focused for keyboard replay. "
                f"expected_hwnd={hwnd}, foreground_hwnd={foreground}."
            )
        return None
    except ImportError:
        return "pywin32 not installed"
    except ToolCancelled:
        raise
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
    _restore_focus: bool = True,
    _bypass_gate: bool = False,
) -> str:
    """Send a keyboard shortcut. keys is '+' separated, e.g. 'ctrl+c', 'alt+f4'."""
    shell_error = _shell_hotkey_error(keys)
    if shell_error:
        return _keyboard_blocked_result(shell_error, keys=keys)
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
            tool_name="computer_functions_hotkey",
            action_type="type",
            input_str=json.dumps(payload_args, ensure_ascii=False, sort_keys=True),
            payload_args=payload_args,
        )
        if pending is not None:
            return pending

    try:
        check_cancelled()
        import win32api

        focus_error = _focus_target_window(
            target_hwnd=target_hwnd,
            target_title=target_title,
            target_process_name=target_process_name,
        ) if _restore_focus else _input_target_error(target_hwnd)
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
        target_error = _keyboard_target_error()
        if target_error:
            return _keyboard_blocked_result(target_error, keys=keys)

        parts = [p.strip() for p in keys.split("+") if p.strip()]
        if not parts:
            return json.dumps({"status": "error", "error": "No keys provided."})

        vk_codes = [_vk_for_key(p) for p in parts]

        pressed = []
        try:
            for vk in vk_codes:
                check_cancelled()
                target_error = _input_target_error(target_hwnd)
                if target_error:
                    return _keyboard_blocked_result(target_error, keys=keys)
                win32api.keybd_event(vk, 0, 0, 0)
                pressed.append(vk)
                cancellable_sleep(0.02)
        finally:
            for vk in reversed(pressed):
                win32api.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

        _audit.log("controller.hotkey", data={"keys": keys})
        return json.dumps({"status": "ok", "keys": keys, "method": "hotkey"})

    except ToolCancelled as exc:
        return json.dumps(_cancelled_result(exc, keys=keys))
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
                tool_name="computer_functions_window_action",
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


@physical_pixels
def _ctrl_scroll(direction: str = "down", clicks: int = 3, x: int = -1, y: int = -1, *, target_hwnd: int = 0, _bypass_gate: bool = False) -> str:
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
            tool_name="computer_functions_scroll",
            action_type="click",
            input_str=json.dumps({"direction": direction, "clicks": clicks, "x": x, "y": y}),
            payload_args={"direction": direction, "clicks": clicks, "x": x, "y": y},
        )
        if pending is not None:
            return pending

    try:
        check_cancelled()
        MOUSEEVENTF_WHEEL = 0x0800
        WHEEL_DELTA = 120

        if (x, y) != (-1, -1):
            ctypes.windll.user32.SetCursorPos(x, y)
            cancellable_sleep(0.05)
        target_error = _mouse_target_error(target_hwnd, None if (x, y) == (-1, -1) else x, None if (x, y) == (-1, -1) else y)
        if target_error:
            return json.dumps({"status": "blocked", "reason_code": "target_focus_changed", "error": target_error})
        check_cancelled()

        delta = WHEEL_DELTA * clicks if direction == "up" else -(WHEEL_DELTA * clicks)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta & 0xFFFFFFFF, 0)

        _audit.log("controller.scroll", data={"direction": direction, "clicks": clicks})
        return json.dumps({"status": "ok", "direction": direction, "clicks": clicks, "method": "scroll"})

    except ToolCancelled as exc:
        return json.dumps(_cancelled_result(exc))
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


@physical_pixels
def _ctrl_double_click(x: int, y: int, button: str = "left", *, target_hwnd: int = 0, _bypass_gate: bool = False) -> str:
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
            tool_name="computer_functions_screen_click",
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

        completed_clicks = 0
        for _ in range(2):
            check_cancelled()
            ctypes.windll.user32.SetCursorPos(x, y)
            cancellable_sleep(0.05)
            target_error = _mouse_target_error(target_hwnd, x, y)
            if target_error:
                return json.dumps({"status": "blocked", "reason_code": "target_focus_changed", "error": target_error, "completed_clicks": completed_clicks})
            check_cancelled()
            ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
            try:
                cancellable_sleep(0.02)
            finally:
                ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)
            completed_clicks += 1

        _audit.log("controller.double_click", data={"x": x, "y": y, "button": button})
        return json.dumps({"status": "ok", "x": x, "y": y, "button": button, "method": "double_click"})

    except ToolCancelled as exc:
        return json.dumps(_cancelled_result(exc, completed_clicks=completed_clicks))
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


_SENSITIVE_CLIPBOARD_FORMATS = ("ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore")


def _sensitive_clipboard_reason(win32clipboard) -> str:
    """Refuse clipboard content that password managers or blocked apps put there.

    Password managers mark secrets with these registered formats so clipboard
    monitors skip them; the clipboard owner catches blocked apps that do not.
    Caller must have the clipboard open.
    """
    for format_name in _SENSITIVE_CLIPBOARD_FORMATS:
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.RegisterClipboardFormat(format_name)):
                return "Clipboard content is marked sensitive by the app that copied it."
        except Exception:
            continue
    try:
        import psutil
        import win32process

        owner = win32clipboard.GetClipboardOwner()
        if owner:
            _, pid = win32process.GetWindowThreadProcessId(owner)
            owner_process = psutil.Process(pid).name() if pid else ""
            if owner_process and is_blocked_process_alias(owner_process):
                return f"Clipboard content was copied from blocked process '{owner_process}'."
    except Exception:
        pass
    return ""


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
            tool_name="computer_functions_clipboard",
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
                sensitive_reason = _sensitive_clipboard_reason(win32clipboard)
                if sensitive_reason:
                    return json.dumps({
                        "status": "blocked",
                        "reason_code": "sensitive_clipboard_restricted",
                        "error": sensitive_reason,
                    })
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


@physical_pixels
def _ctrl_drag(start_x: int, start_y: int, end_x: int, end_y: int, button: str = "left", *, target_hwnd: int = 0, _bypass_gate: bool = False) -> str:
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
            tool_name="computer_functions_drag",
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

        check_cancelled()
        ctypes.windll.user32.SetCursorPos(start_x, start_y)
        cancellable_sleep(0.05)
        target_error = _mouse_target_error(target_hwnd, start_x, start_y)
        if target_error:
            return json.dumps({"status": "blocked", "reason_code": "target_focus_changed", "error": target_error})
        check_cancelled()
        ctypes.windll.user32.mouse_event(btn_down, 0, 0, 0, 0)
        try:
            cancellable_sleep(0.05)
            for i in range(1, 16):
                check_cancelled()
                target_error = _mouse_target_error(target_hwnd)
                if target_error:
                    return json.dumps({"status": "blocked", "reason_code": "target_focus_changed", "error": target_error})
                ix = int(start_x + (end_x - start_x) * i / 15)
                iy = int(start_y + (end_y - start_y) * i / 15)
                ctypes.windll.user32.SetCursorPos(ix, iy)
                cancellable_sleep(0.02)
        finally:
            ctypes.windll.user32.mouse_event(btn_up, 0, 0, 0, 0)

        _audit.log("controller.drag", data={"from": [start_x, start_y], "to": [end_x, end_y], "button": button})
        return json.dumps({
            "status": "ok",
            "from": [start_x, start_y],
            "to": [end_x, end_y],
            "button": button,
            "method": "drag",
        })

    except ToolCancelled as exc:
        return json.dumps(_cancelled_result(exc))
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
        return json.dumps({
            "status": "error",
            "error": "psutil not installed. Run `uv sync --locked` from the backend directory.",
        })
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _ctrl_screenshot_region(x: int, y: int, width: int, height: int, *, _above_hwnd: int = 0) -> str:
    """Capture a specific region of the screen, with blocked windows blacked out."""
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
        try:
            redacted = redact_capture(filepath, origin_x=x, origin_y=y, above_hwnd=_above_hwnd)
        except Exception as exc:
            # Never hand back a capture that may still show a blocked window.
            filepath.unlink(missing_ok=True)
            return json.dumps({"status": "error", "error": f"Could not redact screenshot: {exc}"})

        size_kb = round(filepath.stat().st_size / 1024, 1)
        _audit.log("controller.screenshot_region", data={"region": monitor, "path": str(filepath)})
        return json.dumps({
            "status": "ok",
            "path": str(filepath),
            "region": {"x": x, "y": y, "width": width, "height": height},
            "size_kb": size_kb,
            "redacted_windows": redacted,
        })

    except ImportError:
        return json.dumps({
            "status": "error",
            "error": "mss not installed. Run `uv sync --locked` from the backend directory.",
        })
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
        _above_hwnd=int(window.get("hwnd") or window.get("handle") or 0),
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
register_executor("computer_functions_screen_click", _resume_screen_click)
register_executor("computer_functions_precision_click", _resume_precision_click)
register_executor("computer_functions_screen_type", _resume_screen_type)
register_executor("computer_functions_hotkey", _resume_hotkey)
register_executor("computer_functions_window_action", _resume_window_action)
register_executor("computer_functions_scroll", _resume_scroll)
register_executor("computer_functions_drag", _resume_drag)


def _resume_clipboard_write(input_str: str) -> str:
    args = _safe_json(input_str)
    return _ctrl_clipboard(
        action=args.get("action", "write"),
        text=args.get("text", input_str.strip()),
        _bypass_gate=True,
    )


register_executor("computer_functions_clipboard", _resume_clipboard_write)


# ── Build tools ───────────────────────────────────────────────────────────────
