"""Tests for the Windows controller – cmd operations and policy gating."""

import json
import os
import sys
from types import SimpleNamespace

import pytest

from app.agent.controller_policy import (
    AppEntry,
    ControllerPolicyState,
    PermissionMode,
)
from app.agent.settings_store import AgentSettings, AppRule, PathRule, save_agent_settings
from app.skills.computer_use.window_ops import (
    _cmd_copy_item,
    _cmd_create_folder,
    _cmd_delete_item,
    _cmd_move_item,
    _cmd_rename_item,
    _precision_click,
    _screen_click,
)


def test_desktop_snapshot_accepts_list_windows_payload(monkeypatch):
    import app.skills.computer_use.tools as desktop_tools

    monkeypatch.setattr(
        desktop_tools.desktop_runtime,
        "screen_info",
        lambda: '{"monitors": [], "cursor": {}, "virtual_screen": {}}',
    )
    monkeypatch.setattr(
        desktop_tools.desktop_runtime,
        "list_windows",
        lambda: '[{"hwnd": 1, "title": "Notepad"}]',
    )
    monkeypatch.setattr(
        desktop_tools,
        "_screenshot",
        lambda region=None: '{"status": "captured", "path": "shot.png"}',
    )

    result = json.loads(
        desktop_tools._desktop_snapshot(include_screenshot=True, include_uia=False)
    )

    assert result["status"] == "ok"
    assert result["windows"] == [{"hwnd": 1, "title": "Notepad"}]
    assert "windows_error" not in result


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Set up isolated workspace with patched policy."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("hello")
    (root / "subdir").mkdir()

    import app.agent.controller_policy as cp

    state = ControllerPolicyState(
        mode=PermissionMode.FULL_ACCESS,
        permitted_roots=[str(root)],
        blocked_roots=[],
        allow_delete=False,
    )

    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    monkeypatch.setattr(cp, "_POLICY_DIR", policy_dir)
    monkeypatch.setattr(cp, "_POLICY_FILE", policy_dir / "controller_policy.md")
    monkeypatch.setattr(cp, "_ALLOWLIST_FILE", policy_dir / "allowlisted_apps.md")

    json_path = policy_dir / "controller_policy.json"
    json_path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    import app.agent.audit as audit_mod
    monkeypatch.setattr(audit_mod, "_LOG_DIR", log_dir)

    import app.skills.computer_use.window_ops as wc
    wc._audit = audit_mod.AuditLogger(log_dir=log_dir)

    settings_path = policy_dir.parent / "settings.json"

    def write_state() -> None:
        json_path.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        settings_data = AgentSettings()
        settings_data.permissions.mode = (
            "default"
            if state.mode == PermissionMode.DEFAULT
            else "full_access"
            if state.mode == PermissionMode.FULL_ACCESS
            else "custom"
        )
        settings_data.permissions.blocked_roots = list(state.blocked_roots)
        settings_data.permissions.path_rules = [
            PathRule(path=path, read=True, write=True, delete=True, launch=False, enabled=True)
            for path in state.permitted_roots
        ]
        settings_data.permissions.app_rules = [
            AppRule(
                alias=entry.alias,
                display_name=entry.display_name,
                exe_paths=list(entry.exe_paths),
                launch_allowed=True,
                uia_allowed=True,
                enabled=True,
            )
            for entry in state.allowlisted_apps
        ]
        settings_data.permissions.allow_delete = state.allow_delete
        settings_data.permissions.dangerous_actions_require_confirm = state.dangerous_actions_require_confirm
        save_agent_settings(settings_data, settings_path=settings_path)

    write_state()

    return {"root": root, "state": state, "policy_json": json_path, "settings_path": settings_path, "write_state": write_state}


class TestCmdOperations:
    def test_create_folder(self, workspace):
        root = workspace["root"]
        target = str(root / "newfolder")
        result = json.loads(_cmd_create_folder(target))
        assert result["status"] == "ok"
        assert os.path.isdir(target)

    def test_move_item(self, workspace):
        root = workspace["root"]
        src = str(root / "file.txt")
        dst = str(root / "subdir" / "file.txt")
        result = json.loads(_cmd_move_item(src, dst))
        assert result["status"] == "ok"
        assert os.path.exists(dst)
        assert not os.path.exists(src)

    def test_copy_item(self, workspace):
        root = workspace["root"]
        src = str(root / "file.txt")
        dst = str(root / "subdir" / "copy.txt")
        result = json.loads(_cmd_copy_item(src, dst))
        assert result["status"] == "ok"
        assert os.path.exists(dst)
        assert os.path.exists(src)

    def test_rename_item(self, workspace):
        root = workspace["root"]
        src = str(root / "file.txt")
        result = json.loads(_cmd_rename_item(src, "renamed.txt"))
        assert result["status"] == "ok"
        assert os.path.exists(str(root / "renamed.txt"))

    def test_delete_item(self, workspace):
        root = workspace["root"]
        target = str(root / "file.txt")
        result = json.loads(_cmd_delete_item(target))
        assert result["status"] == "blocked"
        assert result["reason_code"] == "delete_disabled"

    def test_delete_item_requires_approval_when_enabled(self, workspace):
        root = workspace["root"]
        workspace["state"].allow_delete = True
        workspace["write_state"]()
        target = str(root / "file.txt")
        result = json.loads(_cmd_delete_item(target))
        assert result["status"] == "pending_approval"
        assert "ticket_id" in result


class TestPolicyBlocking:
    def test_unknown_path_create_folder_allowed_in_full_access(self, workspace, tmp_path):
        outside = str(tmp_path / "outside_root" / "nope")
        result = json.loads(_cmd_create_folder(outside))
        assert result["status"] == "ok"
        assert os.path.isdir(outside)

    def test_unknown_destination_move_allowed_in_full_access(self, workspace, tmp_path):
        root = workspace["root"]
        src = str(root / "file.txt")
        outside = str(tmp_path / "outside" / "stolen.txt")
        result = json.loads(_cmd_move_item(src, outside))
        assert result["status"] == "ok"
        assert os.path.exists(outside)

    def test_nonexistent_source_error(self, workspace):
        root = workspace["root"]
        result = json.loads(_cmd_move_item(str(root / "nope.txt"), str(root / "dest.txt")))
        assert result["status"] == "error"


class TestDefaultModeConfirmation:
    def test_default_mode_requires_confirm(self, workspace, monkeypatch):
        """In default mode, mutations require confirmation."""
        workspace["state"].mode = PermissionMode.DEFAULT
        workspace["write_state"]()

        root = workspace["root"]
        result = json.loads(_cmd_create_folder(str(root / "confirm_test")))
        assert result["status"] == "pending_approval"
        assert "ticket_id" in result


class TestWindowControlResponses:
    def test_launch_app_allowlisted_alias_skips_default_confirmation(self, workspace, monkeypatch):
        import app.skills.computer_use.desktop_runtime as dr

        workspace["state"].mode = PermissionMode.DEFAULT
        workspace["state"].allowlisted_apps = [
            AppEntry(alias="notepad", display_name="Notepad", exe_paths=["notepad.exe"])
        ]
        workspace["write_state"]()

        monkeypatch.setattr(dr, "_find_exe", lambda alias: "notepad.exe")
        monkeypatch.setattr(
            dr.subprocess,
            "Popen",
            lambda *_args, **_kwargs: SimpleNamespace(pid=3210),
        )

        result = json.loads(dr.launch_app("notepad"))
        assert result["status"] == "launched"
        assert result["pid"] == 3210

    def test_launch_app_unknown_alias_requires_access_grant_in_default(self, workspace, monkeypatch):
        import app.skills.computer_use.desktop_runtime as dr

        workspace["state"].mode = PermissionMode.DEFAULT
        workspace["state"].allowlisted_apps = []
        workspace["write_state"]()

        monkeypatch.setattr(
            dr.subprocess,
            "Popen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unknown app should not launch before grant")),
        )

        result = json.loads(dr.launch_app("notepad"))
        assert result["status"] == "pending_access_grant"
        assert result["target_type"] == "app"
        assert result["target_identifier"] == "notepad"

    def test_launch_app_unknown_alias_auto_discovers_in_full_access(self, workspace, monkeypatch):
        import shutil

        import app.skills.computer_use.desktop_runtime as dr
        import app.agent.access_grant_broker as grants

        workspace["state"].mode = PermissionMode.FULL_ACCESS
        workspace["state"].allowlisted_apps = []
        workspace["write_state"]()
        monkeypatch.setattr(grants, "_discover_exe_for_alias", lambda _alias: "notepad.exe")
        monkeypatch.setattr(
            dr.subprocess,
            "Popen",
            lambda *_args, **_kwargs: SimpleNamespace(pid=6543),
        )
        monkeypatch.setattr(dr, "_settings_app_allowlist", lambda launch_only=False: {})
        monkeypatch.setattr(shutil, "which", lambda _candidate: None)

        result = json.loads(dr.launch_app("sampleapp"))
        assert result["status"] == "launched"
        assert result["pid"] == 6543
        assert result["launch_target"]["source"] == "discovery"

    def test_launch_app_falls_back_to_start_menu_shortcut(self, workspace, monkeypatch):
        import app.agent.access_grant_broker as grants
        import app.skills.computer_use.desktop_runtime as dr

        workspace["state"].mode = PermissionMode.FULL_ACCESS
        workspace["state"].allowlisted_apps = []
        workspace["write_state"]()

        launched = {}
        monkeypatch.setattr(grants, "_discover_exe_for_alias", lambda _alias: None)
        monkeypatch.setattr(
            dr,
            "_start_menu_shortcuts_for_alias",
            lambda _alias: [
                {
                    "kind": "shortcut",
                    "path": r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Example.lnk",
                    "display_name": "Example",
                    "source": "start_menu",
                }
            ],
        )
        monkeypatch.setattr(dr.os, "startfile", lambda path: launched.update({"path": path}), raising=False)

        result = json.loads(dr.launch_app("example"))

        assert result["status"] == "launched"
        assert result["launch_method"] == "startfile"
        assert result["pid"] == 0
        assert result["launch_target"]["kind"] == "shortcut"
        assert launched["path"].endswith("Example.lnk")

    def test_diagnose_app_launch_reports_missing_configured_candidate(self, workspace, monkeypatch):
        import app.agent.access_grant_broker as grants
        import app.skills.computer_use.desktop_runtime as dr

        workspace["state"].mode = PermissionMode.FULL_ACCESS
        workspace["state"].allowlisted_apps = [
            AppEntry(alias="demo", display_name="Demo", exe_paths=[r"C:\Missing\Demo.exe"])
        ]
        workspace["write_state"]()
        monkeypatch.setattr(grants, "_discover_exe_for_alias", lambda _alias: None)
        monkeypatch.setattr(
            dr,
            "_settings_app_allowlist",
            lambda launch_only=False: {"demo": [r"C:\Missing\Demo.exe"]},
        )
        monkeypatch.setattr(dr, "_start_menu_shortcuts_for_alias", lambda _alias: [])

        result = json.loads(dr.diagnose_app_launch("demo"))

        assert result["status"] == "ok"
        assert result["target"]["found"] is False
        assert result["target"]["attempts"][0]["candidate"] == r"C:\Missing\Demo.exe"
        assert result["next_step"] == "Fix or add an app path in Settings."

    def test_launch_app_reports_matching_visible_window(self, workspace, monkeypatch):
        import app.agent.access_grant_broker as grants
        import app.skills.computer_use.desktop_runtime as dr

        workspace["state"].mode = PermissionMode.FULL_ACCESS
        workspace["state"].allowlisted_apps = []
        workspace["write_state"]()

        monkeypatch.setattr(grants, "_discover_exe_for_alias", lambda _alias: "notepad.exe")
        monkeypatch.setattr(
            dr.subprocess,
            "Popen",
            lambda *_args, **_kwargs: SimpleNamespace(pid=4321),
        )

        titles = {123: "Untitled - Notepad"}
        fake_win32gui = SimpleNamespace(
            IsWindowVisible=lambda hwnd: hwnd in titles,
            GetWindowText=lambda hwnd: titles.get(hwnd, ""),
            EnumWindows=lambda callback, arg: [callback(hwnd, arg) for hwnd in titles],
        )
        fake_win32process = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, 4321))
        fake_psutil = SimpleNamespace(Process=lambda _pid: SimpleNamespace(name=lambda: "notepad.exe"))
        monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
        monkeypatch.setitem(sys.modules, "win32process", fake_win32process)
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        result = json.loads(dr.launch_app("notepad"))

        assert result["status"] == "launched"
        assert result["window"]["status"] == "found"
        assert result["window"]["window"]["hwnd"] == 123

    def test_screen_click_blocked_when_screen_fallback_disabled(self, monkeypatch):
        monkeypatch.setattr("app.skills.computer_use.window_ops.is_screen_fallback_allowed", lambda: False)

        result = json.loads(_screen_click(10, 20))
        assert result["status"] == "blocked"
        assert result["reason_code"] == "screen_fallback_disabled"

    def test_precision_click_maps_screenshot_coordinates(self, monkeypatch):
        import app.skills.computer_use.window_ops as wc

        monkeypatch.setattr(
            wc,
            "_screen_click",
            lambda x, y, button="left", _bypass_gate=False: json.dumps(
                {"status": "ok", "x": x, "y": y, "button": button, "method": "click"}
            ),
        )

        result = json.loads(
            _precision_click(
                20,
                30,
                coordinate_mode="screenshot",
                origin_x=100,
                origin_y=200,
                width=300,
                height=400,
                _bypass_gate=True,
            )
        )

        assert result["status"] == "ok"
        assert result["method"] == "precision_click"
        assert result["mapped"] == {"x": 120, "y": 230}
        assert result["bounds"]["x"] == 100
        assert result["bounds"]["y"] == 200

    def test_precision_click_rejects_out_of_region_coordinates(self):
        result = json.loads(
            _precision_click(
                120,
                30,
                coordinate_mode="screenshot",
                origin_x=0,
                origin_y=0,
                width=100,
                height=100,
                _bypass_gate=True,
            )
        )

        assert result["status"] == "error"
        assert "outside the screenshot region" in result["error"]

    def test_double_click_uses_same_confirmation_gate_as_click(self, workspace, monkeypatch):
        import app.skills.computer_use.window_ops as wc

        workspace["state"].mode = PermissionMode.DEFAULT
        workspace["write_state"]()
        monkeypatch.setattr(wc, "is_screen_fallback_allowed", lambda: True)

        result = json.loads(wc._ctrl_double_click(10, 20))
        assert result["status"] == "pending_approval"
        assert result["reason_code"] == "confirmation_required"

    def test_screen_type_uses_keyboard_events(self, monkeypatch):
        import app.skills.computer_use.window_ops as wc

        key_events = []
        fake_win32api = SimpleNamespace(
            VkKeyScan=lambda char: ord(char.upper()),
            keybd_event=lambda vk, *_args: key_events.append(vk),
        )
        fake_win32con = SimpleNamespace(VK_SHIFT=0x10, KEYEVENTF_KEYUP=0x0002)
        monkeypatch.setitem(sys.modules, "win32api", fake_win32api)
        monkeypatch.setitem(sys.modules, "win32con", fake_win32con)
        monkeypatch.setattr(wc.time, "sleep", lambda *_args: None)

        result = json.loads(wc._screen_type("a long value", _bypass_gate=True))
        assert result["status"] == "ok"
        assert result["method"] == "type"
        assert result["typed_chars"] == len("a long value")
        assert len(key_events) == len("a long value") * 2

    def test_paste_text_refuses_non_text_clipboard(self, monkeypatch):
        import app.skills.computer_use.window_ops as wc

        emptied = {"called": False}
        formats = [15, 0]
        fake_win32api = SimpleNamespace(VkKeyScan=lambda char: ord(char.upper()), keybd_event=lambda *_args: None)
        fake_win32con = SimpleNamespace(CF_TEXT=1, CF_OEMTEXT=7, CF_UNICODETEXT=13, CF_LOCALE=16, KEYEVENTF_KEYUP=0x0002)
        fake_win32clipboard = SimpleNamespace(
            OpenClipboard=lambda: None,
            CloseClipboard=lambda: None,
            EnumClipboardFormats=lambda _fmt: formats.pop(0),
            IsClipboardFormatAvailable=lambda _fmt: False,
            GetClipboardData=lambda _fmt: "",
            EmptyClipboard=lambda: emptied.update({"called": True}),
            SetClipboardData=lambda _fmt, _text: None,
        )
        monkeypatch.setitem(sys.modules, "win32api", fake_win32api)
        monkeypatch.setitem(sys.modules, "win32con", fake_win32con)
        monkeypatch.setitem(sys.modules, "win32clipboard", fake_win32clipboard)

        result = wc._paste_text_via_clipboard("replacement")

        assert result["status"] == "error"
        assert result["reason_code"] == "clipboard_non_text_present"
        assert emptied["called"] is False

    def test_window_screenshot_captures_matched_window_region(self, monkeypatch):
        import app.skills.computer_use.window_ops as wc

        captured = {}
        monkeypatch.setattr(wc, "_gate", lambda *_args, **_kwargs: SimpleNamespace(blocked=False))
        monkeypatch.setattr(
            wc,
            "get_window_rect",
            lambda title="", hwnd=0: {
                "status": "ok",
                "window": {
                    "hwnd": hwnd,
                    "title": title or "Article - Chrome",
                    "rect": {"x": 10, "y": 20, "width": 300, "height": 200},
                },
            },
        )

        def fake_region(x, y, width, height):
            captured.update({"x": x, "y": y, "width": width, "height": height})
            return json.dumps({"status": "ok", "path": "window.png", "region": dict(captured)})

        monkeypatch.setattr(wc, "_ctrl_screenshot_region", fake_region)

        result = json.loads(wc._ctrl_screenshot_window(title="Article - Chrome", hwnd=123))

        assert result["status"] == "ok"
        assert captured == {"x": 10, "y": 20, "width": 300, "height": 200}
        assert result["mode"] == "window"
        assert result["window"]["hwnd"] == 123
        assert result["coordinate_origin"] == {"x": 10, "y": 20}

    def test_hotkey_approval_payload_captures_foreground_window(self, monkeypatch):
        import app.skills.computer_use.window_ops as wc

        captured = {}

        def fake_permission_result(*_args, **kwargs):
            captured.update(kwargs.get("payload_args") or {})
            return json.dumps({"status": "pending_approval", "ticket_id": "ticket-1"})

        fake_win32gui = SimpleNamespace(
            GetForegroundWindow=lambda: 123,
            IsWindow=lambda hwnd: hwnd == 123,
            GetWindowText=lambda hwnd: "Browser - Article" if hwnd == 123 else "",
        )
        fake_win32process = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (10, 456))
        fake_psutil = SimpleNamespace(Process=lambda _pid: SimpleNamespace(name=lambda: "msedge.exe"))
        monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
        monkeypatch.setitem(sys.modules, "win32process", fake_win32process)
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
        monkeypatch.setattr(wc, "_permission_result", fake_permission_result)

        result = json.loads(wc._ctrl_hotkey("ctrl+a"))

        assert result["status"] == "pending_approval"
        assert captured["keys"] == "ctrl+a"
        assert captured["target_hwnd"] == 123
        assert captured["target_title"] == "Browser - Article"
        assert captured["target_pid"] == 456
        assert captured["target_process_name"] == "msedge.exe"

    def test_hotkey_resume_refocuses_target_before_key_events(self, monkeypatch):
        import app.skills.computer_use.window_ops as wc

        events = []
        foreground = {"hwnd": 999}

        def set_foreground(hwnd):
            events.append(("focus", hwnd))
            foreground["hwnd"] = hwnd

        def keybd_event(vk, *_args):
            events.append(("key", vk))

        fake_win32gui = SimpleNamespace(
            IsWindow=lambda hwnd: hwnd == 123,
            IsWindowVisible=lambda hwnd: hwnd == 123,
            GetWindowText=lambda hwnd: "Browser - Article" if hwnd == 123 else "",
            ShowWindow=lambda *_args: events.append(("show", 123)),
            BringWindowToTop=lambda hwnd: events.append(("bring", hwnd)),
            SetForegroundWindow=set_foreground,
            GetForegroundWindow=lambda: foreground["hwnd"],
        )
        fake_win32process = SimpleNamespace(
            GetWindowThreadProcessId=lambda hwnd: (10 if hwnd == 123 else 20, hwnd + 1000),
            AttachThreadInput=lambda *_args: events.append(("attach",)),
        )
        fake_win32api = SimpleNamespace(
            GetCurrentThreadId=lambda: 30,
            VkKeyScan=lambda char: ord(char),
            keybd_event=keybd_event,
        )
        fake_win32con = SimpleNamespace(SW_RESTORE=9)
        monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
        monkeypatch.setitem(sys.modules, "win32process", fake_win32process)
        monkeypatch.setitem(sys.modules, "win32api", fake_win32api)
        monkeypatch.setitem(sys.modules, "win32con", fake_win32con)
        monkeypatch.setattr(wc.time, "sleep", lambda *_args: None)

        result = json.loads(
            wc._resume_hotkey(
                json.dumps(
                    {
                        "keys": "ctrl+a",
                        "target_hwnd": 123,
                        "target_title": "Browser - Article",
                    }
                )
            )
        )

        assert result["status"] == "ok"
        assert events.index(("focus", 123)) < next(
            index for index, event in enumerate(events) if event[0] == "key"
        )

    def test_hotkey_resume_refuses_ticket_without_target_window(self):
        import app.skills.computer_use.window_ops as wc

        result = json.loads(wc._resume_hotkey(json.dumps({"keys": "ctrl+a"})))

        assert result["status"] == "error"
        assert result["reason_code"] == "target_window_missing"

    def test_hotkey_resume_refuses_when_target_cannot_be_focused(self, monkeypatch):
        import app.skills.computer_use.window_ops as wc

        key_events = []
        fake_win32gui = SimpleNamespace(
            IsWindow=lambda hwnd: hwnd == 123,
            IsWindowVisible=lambda hwnd: hwnd == 123,
            GetWindowText=lambda hwnd: "Browser - Article" if hwnd == 123 else "",
            ShowWindow=lambda *_args: None,
            BringWindowToTop=lambda *_args: None,
            SetForegroundWindow=lambda *_args: None,
            GetForegroundWindow=lambda: 999,
        )
        fake_win32process = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (10, hwnd + 1000))
        fake_win32api = SimpleNamespace(
            GetCurrentThreadId=lambda: 30,
            VkKeyScan=lambda char: ord(char),
            keybd_event=lambda *args: key_events.append(args),
        )
        fake_win32con = SimpleNamespace(SW_RESTORE=9)
        monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
        monkeypatch.setitem(sys.modules, "win32process", fake_win32process)
        monkeypatch.setitem(sys.modules, "win32api", fake_win32api)
        monkeypatch.setitem(sys.modules, "win32con", fake_win32con)
        monkeypatch.setattr(wc.time, "sleep", lambda *_args: None)

        result = json.loads(
            wc._resume_hotkey(
                json.dumps(
                    {
                        "keys": "ctrl+a",
                        "target_hwnd": 123,
                        "target_title": "Browser - Article",
                    }
                )
            )
        )

        assert result["status"] == "error"
        assert result["reason_code"] == "target_window_not_focused"
        assert key_events == []

    def test_window_action_reports_ambiguous_title_matches(self, monkeypatch):
        import app.skills.computer_use.window_ops as wc

        titles = {101: "Settings", 102: "Settings"}
        fake_win32gui = SimpleNamespace(
            IsWindow=lambda hwnd: hwnd in titles,
            IsWindowVisible=lambda hwnd: hwnd in titles,
            GetWindowText=lambda hwnd: titles.get(hwnd, ""),
            EnumWindows=lambda callback, arg: [callback(hwnd, arg) for hwnd in titles],
        )
        fake_win32process = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, hwnd + 1000))
        fake_win32con = SimpleNamespace(
            SW_MINIMIZE=6,
            SW_MAXIMIZE=3,
            SW_RESTORE=9,
            WM_CLOSE=0x0010,
        )
        monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
        monkeypatch.setitem(sys.modules, "win32process", fake_win32process)
        monkeypatch.setitem(sys.modules, "win32con", fake_win32con)

        result = json.loads(wc._ctrl_window_action("Settings", "restore", _bypass_gate=True))
        assert result["status"] == "error"
        assert result["reason_code"] == "window_ambiguous"
        assert len(result["matches"]) == 2

    def test_focus_window_reports_ambiguous_title_matches(self, monkeypatch):
        import app.skills.computer_use.desktop_runtime as dr

        titles = {201: "Settings", 202: "Settings"}
        fake_win32gui = SimpleNamespace(
            IsWindow=lambda hwnd: hwnd in titles,
            IsWindowVisible=lambda hwnd: hwnd in titles,
            GetWindowText=lambda hwnd: titles.get(hwnd, ""),
            EnumWindows=lambda callback, arg: [callback(hwnd, arg) for hwnd in titles],
        )
        fake_win32process = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, hwnd + 1000))
        fake_win32con = SimpleNamespace(SW_RESTORE=9)
        monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
        monkeypatch.setitem(sys.modules, "win32process", fake_win32process)
        monkeypatch.setitem(sys.modules, "win32con", fake_win32con)

        result = json.loads(dr.focus_window("Settings"))
        assert result["status"] == "error"
        assert result["reason_code"] == "window_ambiguous"
        assert len(result["matches"]) == 2

    def test_uia_interact_reports_ambiguous_windows(self, monkeypatch):
        import app.skills.computer_use.window_ops as wc

        class FakeWindow:
            def __init__(self, title: str, handle: int):
                self._title = title
                self.handle = handle

            def window_text(self):
                return self._title

        fake_desktop = SimpleNamespace(
            windows=lambda: [
                FakeWindow("Notepad - draft 1", 101),
                FakeWindow("Notepad - draft 2", 102),
            ]
        )
        monkeypatch.setattr(wc, "_uia_available", lambda: True)
        monkeypatch.setattr(wc, "get_effective_app_rule", lambda alias: SimpleNamespace(uia_allowed=True))
        monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda backend=None: fake_desktop))

        result = json.loads(wc._uia_interact_app("notepad", "get_elements", {}, _bypass_gate=True))
        assert result["status"] == "error"
        assert result["reason_code"] == "window_ambiguous"
        assert len(result["matches"]) == 2

    def test_uia_interact_success_includes_matched_window(self, monkeypatch):
        import app.skills.computer_use.window_ops as wc

        clicked = {"value": False}

        class FakeRect:
            left = 10
            top = 20
            right = 110
            bottom = 60

        class FakeChild:
            element_info = SimpleNamespace(
                automation_id="file-button",
                control_type="Button",
                class_name="Button",
            )

            def window_text(self):
                return "File"

            def friendly_class_name(self):
                return "Button"

            def rectangle(self):
                return FakeRect()

            def click_input(self):
                clicked["value"] = True
                return None

        class FakeWindow:
            handle = 777

            def window_text(self):
                return "Notepad"

            def descendants(self):
                return [FakeChild()]

            def rectangle(self):
                return FakeRect()

        fake_desktop = SimpleNamespace(windows=lambda: [FakeWindow()])
        monkeypatch.setattr(wc, "_uia_available", lambda: True)
        monkeypatch.setattr(wc, "get_effective_app_rule", lambda alias: SimpleNamespace(uia_allowed=True))
        monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda backend=None: fake_desktop))

        result = json.loads(wc._uia_interact_app("notepad", "click_element", {"element": "File"}, _bypass_gate=True))
        assert result["status"] == "ok"
        assert clicked["value"] is True
        assert result["matched_window"]["title"] == "Notepad"
        assert result["matched_window"]["handle"] == 777
        assert result["target"]["center"] == {"x": 60, "y": 40}

    def test_uia_read_and_type_element(self, monkeypatch):
        import app.skills.computer_use.window_ops as wc

        class FakeRect:
            left = 10
            top = 20
            right = 110
            bottom = 60

        class FakeChild:
            element_info = SimpleNamespace(
                automation_id="edit-box",
                control_type="Edit",
                class_name="Edit",
            )

            def __init__(self):
                self.value = "before"

            def window_text(self):
                return "Name"

            def friendly_class_name(self):
                return "Edit"

            def rectangle(self):
                return FakeRect()

            def get_value(self):
                return self.value

            def set_edit_text(self, text):
                self.value = text

        child = FakeChild()

        class FakeWindow:
            handle = 777

            def window_text(self):
                return "Notepad"

            def descendants(self):
                return [child]

            def rectangle(self):
                return FakeRect()

        fake_desktop = SimpleNamespace(windows=lambda: [FakeWindow()])
        monkeypatch.setattr(wc, "_uia_available", lambda: True)
        monkeypatch.setattr(wc, "get_effective_app_rule", lambda alias: SimpleNamespace(uia_allowed=True))
        monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda backend=None: fake_desktop))

        read_result = json.loads(wc._uia_interact_app("notepad", "read_element", {"element": "Name"}, _bypass_gate=True))
        type_result = json.loads(
            wc._uia_interact_app(
                "notepad",
                "type_element",
                {"element": "Name", "text": "after"},
                _bypass_gate=True,
            )
        )

        assert read_result["status"] == "ok"
        assert read_result["value"] == "before"
        assert type_result["status"] == "ok"
        assert type_result["value_after"] == "after"
