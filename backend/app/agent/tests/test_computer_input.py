import json
import sys
import ctypes
from types import SimpleNamespace

import pytest

from app.agent import tool_cancellation as cancellation
from app.skills.computer_use import window_ops as ops, screen_geometry as geometry

_REAL_MOUSE_TARGET_ERROR = ops._mouse_target_error


@pytest.fixture
def inputs(monkeypatch):
    events = []
    api = SimpleNamespace(VkKeyScan=lambda char: ord(char) if ord(char) < 128 else -1,
        keybd_event=lambda vk, _scan, flags, _extra: events.append(("key", vk, flags)))
    user = SimpleNamespace(SetCursorPos=lambda x, y: events.append(("move", x, y)),
        mouse_event=lambda flag, *a: events.append(("mouse", flag)))
    monkeypatch.setitem(sys.modules, "win32api", api)
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(KEYEVENTF_KEYUP=2, CF_UNICODETEXT=13, VK_CONTROL=17))
    monkeypatch.setattr(ops.ctypes, "windll", SimpleNamespace(user32=user), raising=False)
    monkeypatch.setitem(sys.modules, "pythoncom", SimpleNamespace(COINIT_MULTITHREADED=0, CoInitializeEx=lambda _: None, CoUninitialize=lambda: None))
    monkeypatch.setattr(ops, "_focus_target_window", lambda **k: None)
    monkeypatch.setattr(ops, "_input_target_error", lambda *a: None)
    monkeypatch.setattr(ops, "_mouse_target_error", lambda *a: None)
    monkeypatch.setattr(ops, "_keyboard_target_error", lambda: None)
    monkeypatch.setattr(ops, "cancellable_sleep", lambda _: cancellation.check_cancelled())
    return events, api, user


@pytest.fixture
def call_scope():
    token = cancellation.set_current_call_id("native-input")
    yield
    cancellation.finish_cancellation(cancellation.current_call_id())
    cancellation.reset_current_call_id(token)


def test_unicode_is_preflighted_before_any_keyboard_input(inputs):
    events, _, _ = inputs
    result = ops._type_text_via_keyboard("hello 中文")
    assert result["reason_code"] == "unsupported_keyboard_character"
    assert result["typed_chars"] == 0
    assert events == []


def test_stop_during_typing_releases_modifier_and_skips_remaining_text(inputs, call_scope):
    events, api, _ = inputs
    api.VkKeyScan = lambda _: 65 | (1 << 8)
    def key(vk, scan, flags, extra):
        events.append(("key", vk, flags))
        if flags == 0:
            cancellation.cancel_call("native-input")
    api.keybd_event = key
    result = ops._type_text_via_keyboard("AAAA")
    assert result["status"] == "cancelled"
    assert events == [("key", 16, 0), ("key", 16, 2)]
    assert result["typed_chars"] == 0


def test_hotkey_stop_releases_pressed_keys(inputs, call_scope):
    events, api, _ = inputs
    def key(vk, scan, flags, extra):
        events.append(("key", vk, flags))
        if not flags:
            cancellation.cancel_call("native-input")
    api.keybd_event = key
    result = json.loads(ops._ctrl_hotkey("ctrl+s", target_hwnd=123, _bypass_gate=True))
    assert result["status"] == "cancelled"
    assert events == [("key", 17, 0), ("key", 17, 2)]


def test_drag_stop_releases_button_and_skips_later_moves(inputs, call_scope):
    events, _, user = inputs
    def move(x, y):
        events.append(("move", x, y))
        if x > 0:
            cancellation.cancel_call("native-input")
    user.SetCursorPos = move
    result = json.loads(ops._ctrl_drag(0, 0, 150, 150, target_hwnd=123, _bypass_gate=True))
    assert result["status"] == "cancelled"
    assert events == [("move", 0, 0), ("mouse", 2), ("move", 10, 10), ("mouse", 4)]


def test_negative_monitor_scroll_coordinates_are_used(inputs):
    events, _, user = inputs
    user.mouse_event = lambda flag, *a: events.append(("mouse", flag))
    result = json.loads(ops._ctrl_scroll(x=-400, y=50, target_hwnd=123, _bypass_gate=True))
    assert result["status"] == "ok"
    assert events[0] == ("move", -400, 50)


@pytest.fixture
def clipboard(monkeypatch, inputs):
    data = {"text": "previous", "sequence": 0}
    def set_text(_fmt, text):
        data["text"] = text
        data["sequence"] += 1
    clip = SimpleNamespace(OpenClipboard=lambda: None, CloseClipboard=lambda: None,
        IsClipboardFormatAvailable=lambda _: data["text"] is not None,
        GetClipboardData=lambda _: data["text"], EmptyClipboard=lambda: set_text(13, None),
        SetClipboardData=set_text, GetClipboardSequenceNumber=lambda: data["sequence"])
    monkeypatch.setitem(sys.modules, "win32clipboard", clip)
    monkeypatch.setattr(ops, "_sensitive_clipboard_reason", lambda _: "")
    return data, clip


def test_paste_restores_clipboard_even_on_stop(clipboard, inputs, call_scope):
    data, _ = clipboard
    events, api, _ = inputs
    def key(vk, scan, flags, extra):
        events.append(("key", vk, flags))
        if vk == 86 and not flags:
            cancellation.cancel_call("native-input")
    api.VkKeyScan = lambda _: 86
    api.keybd_event = key
    with pytest.raises(cancellation.ToolCancelled):
        ops._paste_text_via_clipboard("中文", target_hwnd=123)
    assert data["text"] == "previous"
    assert events[-2:] == [("key", 86, 2), ("key", 17, 2)]


def test_paste_preserves_new_user_clipboard_content(clipboard, inputs):
    data, clip = clipboard
    _, api, _ = inputs
    def key(vk, scan, flags, extra):
        if vk == 86 and not flags:
            clip.SetClipboardData(13, "user copied this")
    api.VkKeyScan = lambda _: 86
    api.keybd_event = key
    result = ops._paste_text_via_clipboard("中文", target_hwnd=123)
    assert result["status"] == "ok"
    assert result["clipboard_restored"] is False
    assert data["text"] == "user copied this"


@pytest.fixture
def uia(monkeypatch, inputs):
    class Element:
        element_info = SimpleNamespace(name="Text", control_type="Edit", automation_id="editor", runtime_id=[1, 2],
            class_name="Edit", element=SimpleNamespace(CurrentIsPassword=False))
        value = ""
        sets = 0
        invokes = 0
        clicks = 0
        option = "Old"
        def window_text(self): return "Text"
        def friendly_class_name(self): return "Edit"
        def is_enabled(self): return True
        def get_value(self): return self.value
        def set_edit_text(self, text): self.sets += 1; self.value = text
        def invoke(self): self.invokes += 1
        def click_input(self): self.clicks += 1
        def select(self, option): self.option = option
        def selected_text(self): return self.option
    element = Element()
    window = SimpleNamespace(handle=123, window_text=lambda: "Notes", descendants=lambda: [element])
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda **k: SimpleNamespace(windows=lambda: [window])))
    monkeypatch.setitem(sys.modules, "pywinauto.uia_defines", SimpleNamespace(NoPatternInterfaceError=type("NoPatternInterfaceError", (Exception,), {})))
    monkeypatch.setattr(ops, "_uia_available", lambda: True)
    monkeypatch.setattr(ops, "get_effective_app_rule", lambda _: None)
    monkeypatch.setattr(ops, "_uia_window_process_name", lambda _: "notepad.exe")
    return element


def interact(action, **params):
    return json.loads(ops._uia_interact_app("notepad.exe", action,
        {"hwnd": 123, "automation_id": "editor", "runtime_id": [1, 2], **params}, _bypass_gate=True))


def test_uia_set_value_verified_including_chinese(uia):
    result = interact("set_value", text="hello 中文")
    assert result["status"] == "ok"
    assert result["verification"]["verified"] is True
    assert uia.sets == 1


@pytest.mark.parametrize("length,is_password", [(4001, False), (4, True)])
def test_actual_control_and_length_checks_block_before_setter(uia, length, is_password):
    uia.element_info.element.CurrentIsPassword = is_password
    result = interact("set_value", text="x" * length)
    assert result["reason_code"] == ("credential_entry_restricted" if is_password else "typed_text_limit_exceeded")
    assert uia.sets == 0
    assert uia.clicks == 0
    if is_password:
        assert "value" not in ops._element_summary(uia, 0)


def test_failed_verification_never_repeats_text_input(uia, monkeypatch):
    def setter(text): uia.sets += 1
    uia.set_edit_text = setter
    monkeypatch.setattr(ops, "_verify_control_value", lambda e, expected: {"verified": False, "reason_code": "verification_failed", "actual": ""})
    result = interact("set_value", text="hello")
    assert result["reason_code"] == "verification_failed"
    assert uia.sets == 1
    assert uia.clicks == 0


def test_setter_that_changes_then_raises_never_falls_back(uia):
    def setter(text):
        uia.value = text
        uia.sets += 1
        raise OSError("provider error after write")
    uia.set_edit_text = setter
    result = interact("set_value", text="hello")
    assert result["reason_code"] == "action_completion_unknown"
    assert uia.sets == 1
    assert uia.clicks == 0


def test_setter_exception_with_unchanged_value_is_still_uncertain(uia):
    def setter(text):
        uia.sets += 1
        raise OSError("provider may have queued the change")
    uia.set_edit_text = setter
    result = interact("set_value", text="hello")
    assert result["reason_code"] == "action_completion_unknown"
    assert uia.sets == 1
    assert uia.clicks == 0


def test_uia_provider_focus_change_refuses_input(uia, monkeypatch):
    monkeypatch.setattr(ops, "_mouse_target_error", lambda *_: "Focus changed during provider lookup")
    result = interact("set_value", text="hello")
    assert result["reason_code"] == "target_focus_changed"
    assert uia.sets == 0


def test_invoke_that_raises_is_not_followed_by_click(uia):
    def invoke():
        uia.invokes += 1
        raise OSError("provider error after invoke")
    uia.invoke = invoke
    result = interact("invoke_element")
    assert result["reason_code"] == "action_completion_unknown"
    assert uia.invokes == 1
    assert uia.clicks == 0


def test_selection_reads_actual_option_and_does_not_accept_click_only(uia, monkeypatch):
    result = interact("select_option", option="New")
    assert result["verification"]["verified"] is True
    assert interact("read_element")["selected"] == "New"
    uia.select = lambda _: None
    monkeypatch.setattr(ops.time, "monotonic", lambda: 10)
    monkeypatch.setattr(ops, "cancellable_sleep", lambda _: setattr(ops.time, "monotonic", lambda: 12))
    result = interact("select_option", option="Missing")
    assert result["reason_code"] == "verification_failed"
    assert uia.clicks == 0


def test_unreadable_value_is_not_reported_as_empty(uia, monkeypatch):
    monkeypatch.setattr(ops, "_read_control_value", lambda _: None)
    assert interact("read_element")["value"] is None
    result = interact("type_element", text="hello", clear=False)
    assert result["reason_code"] == "verification_unavailable"
    assert uia.clicks == 0


def test_screen_typing_refuses_uninspectable_control(inputs, monkeypatch):
    monkeypatch.setattr(ops, "_focused_control", lambda: None)
    result = json.loads(ops._screen_type("hello", _bypass_gate=True))
    assert result["reason_code"] == "input_target_unavailable"
    assert inputs[0] == []


def test_password_control_is_checked_before_clear_hotkeys(uia, monkeypatch, inputs):
    from app.skills.computer_use import tools
    uia.element_info.element.CurrentIsPassword = True
    monkeypatch.setattr(ops, "_focused_control", lambda: uia)
    result = tools._dispatch_batch_action("type_text", {"text": "hello", "clear": True}, {"hwnd": 123}, "", None, True)
    assert result["reason_code"] == "credential_entry_restricted"
    assert inputs[0] == []


def test_screen_unicode_fallback_sends_full_string_once(uia, monkeypatch, inputs):
    monkeypatch.setattr(ops, "_focused_control", lambda: uia)
    pasted = []
    def paste(text, **kwargs):
        pasted.append(text)
        uia.value = text
        return {"status": "ok", "typed_chars": len(text)}
    monkeypatch.setattr(ops, "_paste_text_via_clipboard", paste)
    result = json.loads(ops._screen_type("hello 中文", _bypass_gate=True))
    assert result["verification"]["verified"] is True
    assert pasted == ["hello 中文"]
    assert inputs[0] == []


def test_typing_verification_uses_selection_instead_of_assuming_append():
    class Range:
        def __init__(self, text): self.text = text
        def Clone(self): return Range(self.text)
        def GetText(self, _): return self.text
        def MoveEndpointByRange(self, endpoint, selection, other):
            self.text = "left " if endpoint == 1 else " right"
    pattern = SimpleNamespace(DocumentRange=Range("left old right"),
        GetSelection=lambda: SimpleNamespace(Length=1, GetElement=lambda _: Range("old")))
    control = SimpleNamespace(iface_text=pattern)
    assert ops._expected_typed_value(control, "left old right", "new") == "left new right"
    assert ops._expected_typed_value(control, "different document", "new") is None
    assert ops._expected_typed_value(SimpleNamespace(), "existing", "new") is None


def test_pointer_hit_testing_refuses_occluding_window(inputs, monkeypatch):
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(GetForegroundWindow=lambda: 123,
        WindowFromPoint=lambda _: 999, GetAncestor=lambda *_: 999))
    assert "covered" in _REAL_MOUSE_TARGET_ERROR(123, 20, 20)


def test_dpi_context_is_restored_on_failure(monkeypatch):
    calls = []
    def setter(context):
        calls.append(context.value)
        return 42
    monkeypatch.setattr(geometry.ctypes, "windll", SimpleNamespace(user32=SimpleNamespace(SetThreadDpiAwarenessContext=setter)), raising=False)
    with pytest.raises(ValueError), geometry.physical_coordinates():
        raise ValueError("fail")
    assert calls == [ctypes.c_void_p(-4).value, 42]


@pytest.mark.parametrize("label", ["txtPassword", "PasswordBox", "password_input", "apiKeyInput", "otp_code", "Passwords"])
def test_credential_labels_match_without_word_boundaries(label):
    from app.skills.computer_use.input_safety import text_entry_error
    assert text_entry_error("x", {"automation_id": label})["reason_code"] == "credential_entry_restricted"


def test_invoking_password_related_link_is_not_credential_entry(uia):
    uia.element_info.name = "Forgot password?"
    uia.window_text = lambda: "Forgot password?"
    assert ops._element_summary(uia, 0)["name"] == "Forgot password?"
    result = interact("invoke_element")
    assert result["status"] == "ok"
    assert uia.invokes == 1
    assert interact("read_element")["reason_code"] == "credential_entry_restricted"


def test_missing_value_pattern_falls_back_even_when_value_is_unreadable(uia, monkeypatch):
    error = sys.modules["pywinauto.uia_defines"].NoPatternInterfaceError
    def setter(text):
        raise error()
    uia.set_edit_text = setter
    monkeypatch.setattr(ops, "_read_control_value", lambda _: None)
    monkeypatch.setattr(ops, "_ctrl_hotkey", lambda *a, **k: json.dumps({"status": "ok"}))
    monkeypatch.setattr(ops, "_paste_text_via_clipboard", lambda text, **k: {"status": "ok", "typed_chars": len(text)})
    result = interact("set_value", text="hello")
    assert result["status"] == "ok"
    assert result["verification"]["verified"] is False
    assert uia.clicks == 1


def test_append_moves_caret_to_end_before_pasting(uia, monkeypatch):
    uia.value = "hello"
    sent = []
    monkeypatch.setattr(ops, "_ctrl_hotkey", lambda keys, **k: sent.append(keys) or json.dumps({"status": "ok"}))
    def paste(text, **k):
        assert sent == ["ctrl+end"]
        uia.value += text
        return {"status": "ok", "typed_chars": len(text)}
    monkeypatch.setattr(ops, "_paste_text_via_clipboard", paste)
    result = interact("type_element", text=" world", clear=False)
    assert result["verification"]["verified"] is True
    assert result["status"] == "ok"


def test_unverifiable_screen_typing_is_sent_not_failed(inputs, monkeypatch):
    control = SimpleNamespace(element_info=SimpleNamespace(name="Terminal", control_type="Pane", automation_id="",
        class_name="", element=SimpleNamespace(CurrentIsPassword=False)), window_text=lambda: "Terminal",
        friendly_class_name=lambda: "Pane", is_enabled=lambda: True)
    monkeypatch.setattr(ops, "_focused_control", lambda: control)
    monkeypatch.setattr(ops, "_read_control_value", lambda _: None)
    result = json.loads(ops._screen_type("ls", _bypass_gate=True))
    assert result["status"] == "ok"
    assert result["verification"]["verified"] is False
    assert result["verification"]["reason_code"] == "verification_unavailable"


def test_pointer_hit_testing_accepts_popup_owned_by_target(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(GetForegroundWindow=lambda: 123,
        WindowFromPoint=lambda _: 555, GetAncestor=lambda *_: 555, GetWindow=lambda hwnd, _: 123 if hwnd == 555 else 0))
    assert _REAL_MOUSE_TARGET_ERROR(123, 20, 20) is None


def test_pointer_hit_testing_accepts_unowned_menu_of_same_process(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(GetForegroundWindow=lambda: 123,
        WindowFromPoint=lambda _: 555, GetAncestor=lambda *_: 555, GetWindow=lambda *_: 0,
        GetWindowLong=lambda hwnd, _: 0x80000000 if hwnd == 555 else 0))
    monkeypatch.setattr(ops, "_window_pid", lambda hwnd: 42)
    assert _REAL_MOUSE_TARGET_ERROR(123, 20, 20) is None
    monkeypatch.setattr(ops, "_window_pid", lambda hwnd: 42 if hwnd == 123 else 7)
    assert "covered" in _REAL_MOUSE_TARGET_ERROR(123, 20, 20)
