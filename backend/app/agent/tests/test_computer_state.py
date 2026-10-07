from copy import deepcopy
import json

import pytest
from PIL import Image

from app.agent import run_context, tool_cancellation as cancellation
from app.agent.controller_policy import PermissionDecision
from app.skills.computer_use import session, tools


@pytest.fixture(autouse=True)
def clean_states():
    session.clear_states()
    yield
    session.clear_states()


@pytest.fixture
def desktop(monkeypatch):
    window = {"hwnd": 123, "pid": 10, "process_name": "notepad.exe", "title": "Notes",
              "process_create_time": 10.5, "dpi": 144, "rect": {"x": -500, "y": 20, "width": 400, "height": 300}}
    uia = {"status": "ok", "limit": 100, "elements": [
        {"index": 0, "name": "Text", "automation_id": "editor", "runtime_id": [1, 2], "control_type": "Edit", "value": ""}]}
    events = []
    monkeypatch.setattr(tools, "_resolve_window", lambda **k: {"status": "ok", "window": deepcopy(window)})
    monkeypatch.setattr(tools, "resolve_permission", lambda *a, **k: PermissionDecision(allowed=True))
    monkeypatch.setattr(tools, "is_screen_fallback_allowed", lambda *a, **k: True)
    monkeypatch.setattr(tools, "_active_window", lambda: {"hwnd": 123})
    monkeypatch.setattr(tools.window_ops, "_uia_interact_app", lambda *a, **k: json.dumps(uia))
    monkeypatch.setattr(tools.desktop_runtime, "focus_window", lambda **k: events.append("focus") or '{"status":"focused"}')
    monkeypatch.setattr(tools, "_computer_functions_get_window_state", lambda **k: '{"status":"error","reason_code":"observation_failed"}')
    monkeypatch.setattr(tools, "_dispatch_batch_action", lambda *a, **k: events.append(a[0]) or {"status": "ok"})
    state_id = session.store_state(window=window, screenshot={"status": "skipped"}, uia=uia)
    return window, uia, state_id, events


@pytest.mark.parametrize("setter,resetter,value", [
    (run_context.set_current_conversation_id, run_context.reset_current_conversation_id, "another-chat"),
    (run_context.set_current_control_session_id, run_context.reset_current_control_session_id, "another-session"),
    (run_context.set_current_execution_source, run_context.reset_current_execution_source, "telegram"),
    (run_context.set_current_principal_id, run_context.reset_current_principal_id, "another-user"),
    (run_context.set_current_permission_profile_id, run_context.reset_current_permission_profile_id, "another-profile"),
    (cancellation.set_tool_scope, cancellation.reset_tool_scope, "another-turn"),
])
def test_observations_cannot_cross_owner_boundaries(desktop, setter, resetter, value):
    state_id = desktop[2]
    token = setter(value)
    try:
        assert session.get_state(state_id) is None
    finally:
        resetter(token)
    assert session.get_state(state_id) is not None


def test_cache_is_immutable_expiring_and_bounded(desktop, monkeypatch):
    window, uia, state_id, _ = desktop
    window["title"] = "Changed"
    uia["elements"].clear()
    state = session.get_state(state_id)
    assert state.window["title"] == "Notes"
    assert state.uia["elements"]
    state.window["title"] = "Changed copy"
    assert session.get_state(state_id).window["title"] == "Notes"
    monkeypatch.setattr(session, "_STATE_TTL_SECONDS", -1)
    assert session.get_state(state_id) is None
    monkeypatch.setattr(session, "_STATE_TTL_SECONDS", 120)
    ids = [session.store_state(window=window, screenshot={}, uia=uia) for _ in range(35)]
    assert session.get_state(ids[0]) is None
    assert session.get_state(ids[-1]) is not None
    assert len(session._STATES) == 32


@pytest.mark.parametrize("change", ["pid", "process_create_time", "dpi", "rect", "title"])
def test_live_identity_geometry_and_scaling_changes_refuse_input(desktop, change):
    window, _, state_id, events = desktop
    if change == "rect":
        window["rect"]["x"] += 1
    elif change == "title":
        window[change] = "Popup"
    else:
        window[change] += 1
    result = json.loads(tools._computer_functions_act(actions=[{"type": "click", "x": 20, "y": 20}], state_id=state_id))
    assert result["reason_code"] == "stale_state"
    assert not events
    assert session.get_state(state_id) is None


def test_added_control_is_detected_without_truncating_live_query(desktop):
    _, uia, state_id, events = desktop
    uia["elements"].append({"index": 1, "name": "Unexpected modal"})
    result = json.loads(tools._computer_functions_act(actions=[{"type": "set_value", "element_index": 0, "value": "hello"}], state_id=state_id))
    assert result["reason_code"] == "stale_state"
    assert not events


def test_visible_changes_refuse_coordinate_input(desktop, tmp_path, monkeypatch):
    window, _, _, events = desktop
    before, after = tmp_path / "before.png", tmp_path / "after.png"
    Image.new("RGB", (100, 100), "white").save(before)
    Image.new("RGB", (100, 100), "black").save(after)
    state_id = session.store_state(window=window, uia={"status": "skipped"}, screenshot={"status": "ok", "path": str(before)})
    monkeypatch.setattr(tools.window_ops, "_ctrl_screenshot_window", lambda **k: json.dumps({"status": "ok", "path": str(after)}))
    result = json.loads(tools._computer_functions_act(actions=[{"type": "click", "x": 10, "y": 10}], state_id=state_id))
    assert result["reason_code"] == "stale_state"
    assert not events


def test_snapshot_consumed_after_partial_stop(desktop, monkeypatch):
    _, _, state_id, events = desktop
    token = cancellation.set_current_call_id("stop-batch")
    def act(*a, **k):
        events.append(a[0])
        cancellation.cancel_call("stop-batch")
        return {"status": "ok"}
    monkeypatch.setattr(tools, "_dispatch_batch_action", act)
    try:
        result = json.loads(tools._computer_functions_act(actions=[{"type": "press_key", "key": "tab"}] * 2, state_id=state_id))
        assert result["status"] == "cancelled"
        assert result["completed_actions"] == 1
        assert events == ["focus", "press_key"]
        assert session.get_state(state_id) is None
        assert not result["verified"]
    finally:
        cancellation.finish_cancellation(cancellation.current_call_id())
        cancellation.reset_current_call_id(token)


def test_second_pointer_action_refuses_changed_ui(desktop, monkeypatch):
    _, uia, state_id, events = desktop
    def act(*a, **k):
        events.append(a[0])
        uia["elements"][0]["value"] = "UI changed"
        return {"status": "ok"}
    monkeypatch.setattr(tools, "_dispatch_batch_action", act)
    result = json.loads(tools._computer_functions_act(actions=[{"type": "click", "x": 20, "y": 20}] * 2, state_id=state_id))
    assert result["reason_code"] == "stale_state"
    assert result["completed_actions"] == 1
    assert events == ["focus", "click"]


def test_focus_unknown_stops_uia_action(desktop, monkeypatch):
    _, _, state_id, events = desktop
    monkeypatch.setattr(tools, "_active_window", lambda: {})
    result = json.loads(tools._computer_functions_act(actions=[{"type": "set_value", "element_index": 0, "value": "hello"}], state_id=state_id))
    assert result["reason_code"] == "target_focus_changed"
    assert events == ["focus"]


def test_approved_batch_rechecks_type_permission_before_input(desktop, monkeypatch):
    _, _, state_id, events = desktop
    seen = []
    def permission(action, **k):
        seen.append(action.value)
        return PermissionDecision(blocked=True, reason_code="type_revoked")
    monkeypatch.setattr(tools, "resolve_permission", permission)
    result = json.loads(tools._computer_functions_act(actions=[{"type": "set_value", "element_index": 0, "value": "hello"}], state_id=state_id, _bypass_gate=True))
    assert result["reason_code"] == "type_revoked"
    assert seen == ["type"]
    assert not events


def test_failed_outcome_check_preserves_input_and_never_replays(desktop, monkeypatch):
    window, _, state_id, events = desktop
    monkeypatch.setattr(tools.time, "monotonic", lambda: 10)
    monkeypatch.setattr(tools, "cancellable_sleep", lambda _: setattr(tools.time, "monotonic", lambda: 12))
    result = json.loads(tools._computer_functions_act(actions=[
        {"type": "invoke", "element_index": 0, "expect": {"type": "window_title", "equals": "Saved"}},
        {"type": "press_key", "key": "enter"}], state_id=state_id))
    assert result["reason_code"] == "verification_failed"
    assert result["completed_actions"] == 1
    assert result["results"][0]["verification"]["actual"] == window["title"]
    assert events == ["focus", "invoke"]
    assert "do not replay" in result["instruction"]


def test_file_verification_checks_access_before_input(desktop, monkeypatch, tmp_path):
    _, _, state_id, events = desktop
    monkeypatch.setattr(tools, "resolve_permission", lambda *a, **k: PermissionDecision(blocked=True, reason_code="path_blocked"))
    result = json.loads(tools._computer_functions_act(actions=[{"type": "invoke", "element_index": 0,
        "expect": {"type": "file_exists", "path": str(tmp_path / "saved.txt")}}], state_id=state_id))
    assert result["reason_code"] == "path_blocked"
    assert not events


def test_file_postcondition_verifies_actual_created_file(desktop, monkeypatch, tmp_path):
    _, _, state_id, events = desktop
    saved = tmp_path / "saved.txt"
    def act(*a, **k):
        events.append(a[0])
        saved.write_text("saved")
        return {"status": "ok"}
    monkeypatch.setattr(tools, "_dispatch_batch_action", act)
    result = json.loads(tools._computer_functions_act(actions=[{"type": "invoke", "element_index": 0,
        "expect": {"type": "file_exists", "path": str(saved)}}], state_id=state_id))
    assert result["status"] == "ok"
    assert result["verified"] is True


@pytest.mark.parametrize("action", [
    {"type": "scroll", "x": float("nan")}, {"type": "set_value", "element_index": 0, "value": "x" * 4001},
    {"type": "set_value", "element_index": 0, "value": "123", "label": "Password"},
    {"type": "invoke", "element_index": "0"},
    {"type": "invoke", "element_index": 0, "expect": {"type": "element_value", "equals": "x"}},
])
def test_invalid_later_action_cannot_partially_execute_batch(desktop, action):
    _, _, state_id, events = desktop
    result = json.loads(tools._computer_functions_act(actions=[{"type": "press_key", "key": "tab"}, action], state_id=state_id))
    assert result["status"] in {"error", "blocked"}
    assert not events


def test_pending_gate_after_dispatch_cannot_trigger_batch_replay(desktop, monkeypatch):
    _, _, state_id, _ = desktop
    monkeypatch.setattr(tools, "_dispatch_batch_action", lambda *a: {"status": "pending_approval", "ticket_id": "new-ticket"})
    result = json.loads(tools._computer_functions_act(actions=[{"type": "invoke", "element_index": 0}], state_id=state_id))
    assert result["status"] == "error"
    assert result["reason_code"] == "batch_interrupted"


def test_after_capture_failure_does_not_erase_completed_input(desktop, monkeypatch):
    _, _, state_id, events = desktop
    def fail(**k):
        raise OSError("capture unavailable")
    monkeypatch.setattr(tools, "_computer_functions_get_window_state", fail)
    result = json.loads(tools._computer_functions_act(actions=[{"type": "invoke", "element_index": 0}], state_id=state_id))
    assert result["completed_actions"] == 1
    assert result["after_state"]["reason_code"] == "observation_failed"
    assert not result["verified"]
    assert events == ["focus", "invoke"]


def test_focus_activation_cannot_silently_replace_observed_pixels(desktop, monkeypatch, tmp_path):
    window, _, _, events = desktop
    before, after = tmp_path / "before.png", tmp_path / "after.png"
    Image.new("RGB", (100, 100), "white").save(before)
    Image.new("RGB", (100, 100), "black").save(after)
    state_id = session.store_state(window=window, uia={"status": "skipped"}, screenshot={"status": "ok", "path": str(before)})
    captures = iter([before, after])
    monkeypatch.setattr(tools.window_ops, "_ctrl_screenshot_window", lambda **k: json.dumps({"status": "ok", "path": str(next(captures))}))
    result = json.loads(tools._computer_functions_act(actions=[{"type": "click", "x": 10, "y": 10}], state_id=state_id))
    assert result["reason_code"] == "stale_state"
    assert events == ["focus"]
