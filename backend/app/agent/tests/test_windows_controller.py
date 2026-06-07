from __future__ import annotations

import json
from types import SimpleNamespace

from app.agent.controller_policy import PermissionDecision
from app.skills.computer_use import tools as computer_tools


def test_computer_functions_get_window_state_returns_state_id(monkeypatch):
    window = {
        "hwnd": 123,
        "title": "Untitled - Notepad",
        "process_name": "notepad.exe",
        "rect": {"x": 10, "y": 20, "width": 400, "height": 300},
    }
    monkeypatch.setattr(computer_tools, "_resolve_window", lambda **_kwargs: {"status": "ok", "window": window})
    monkeypatch.setattr(
        computer_tools.window_ops,
        "_ctrl_screenshot_window",
        lambda **_kwargs: json.dumps({"status": "ok", "path": "shot.png", "region": {"x": 10, "y": 20}}),
    )
    monkeypatch.setattr(
        computer_tools.window_ops,
        "_uia_interact_app",
        lambda *_args, **_kwargs: json.dumps({"status": "ok", "elements": [{"index": 1, "name": "File"}]}),
    )
    monkeypatch.setattr(computer_tools.desktop_runtime, "screen_info", lambda: json.dumps({"status": "ok"}))
    monkeypatch.setattr(computer_tools, "_active_window", lambda: {"hwnd": 123})

    result = json.loads(computer_tools._computer_functions_get_window_state(include_text=True))

    assert result["status"] == "ok"
    assert result["state_id"].startswith("cfs_")
    assert result["window"]["hwnd"] == 123
    assert result["screenshot"]["path"] == "shot.png"
    assert result["uia"]["elements"][0]["name"] == "File"


def test_computer_functions_get_window_blocks_sensitive_hwnd(monkeypatch):
    monkeypatch.setattr(
        computer_tools,
        "get_window_rect",
        lambda **_kwargs: {
            "status": "ok",
            "window": {
                "hwnd": 123,
                "title": "Command Prompt",
                "process_name": "cmd.exe",
                "rect": {"x": 0, "y": 0, "width": 600, "height": 400},
            },
        },
    )

    result = json.loads(computer_tools._computer_functions_get_window(hwnd=123))

    assert result["status"] == "blocked"
    assert result["reason_code"] == "blocked_app"


def test_computer_functions_act_batches_after_one_focus(monkeypatch):
    window = {
        "hwnd": 123,
        "title": "Untitled - Notepad",
        "process_name": "notepad.exe",
        "rect": {"x": 100, "y": 200, "width": 400, "height": 300},
    }
    events: list[tuple] = []
    monkeypatch.setattr(computer_tools, "_resolve_window", lambda **_kwargs: {"status": "ok", "window": window})
    monkeypatch.setattr(
        computer_tools.desktop_runtime,
        "focus_window",
        lambda **_kwargs: events.append(("focus", _kwargs.get("hwnd"))) or json.dumps({"status": "focused"}),
    )
    monkeypatch.setattr(
        computer_tools.window_ops,
        "_precision_click",
        lambda x, y, *_args, **_kwargs: events.append(("click", x, y)) or json.dumps({"status": "ok"}),
    )
    monkeypatch.setattr(
        computer_tools.window_ops,
        "_screen_type",
        lambda text, **_kwargs: events.append(("type", text)) or json.dumps({"status": "ok"}),
    )

    result = json.loads(
        computer_tools._computer_functions_act(
            window=window,
            actions=[
                {"type": "click", "x": 20, "y": 30},
                {"type": "type_text", "text": "hello"},
            ],
            _bypass_gate=True,
        )
    )

    assert result["status"] == "ok"
    assert events == [("focus", 123), ("click", 20.0, 30.0), ("type", "hello")]


def test_computer_functions_act_blocks_coordinate_action_when_screen_fallback_disabled(monkeypatch):
    window = {"hwnd": 123, "title": "Notepad", "process_name": "notepad.exe"}
    monkeypatch.setattr(computer_tools, "_resolve_window", lambda **_kwargs: {"status": "ok", "window": window})
    monkeypatch.setattr(computer_tools, "is_screen_fallback_allowed", lambda *_a, **_k: False)
    # The coordinate primitive is dispatched with _bypass_gate=True, so if the batch
    # gate failed to re-check screen fallback this click would execute.
    monkeypatch.setattr(
        computer_tools.window_ops,
        "_precision_click",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("click must be gated by screen fallback")),
    )

    result = json.loads(
        computer_tools._computer_functions_act(
            window=window,
            actions=[{"type": "click", "x": 1, "y": 2}],
        )
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "screen_fallback_disabled"


def test_is_blocked_window_uses_title_when_process_name_missing():
    # Elevated/admin windows surface with an empty process_name; the terminal must
    # still be blocked via its title rather than escaping the blocklist.
    assert computer_tools._is_blocked_window("", "Administrator: Command Prompt") is True
    assert computer_tools._is_blocked_window("", "Windows PowerShell") is True
    assert computer_tools._is_blocked_window("", "Untitled - Notepad") is False
    assert computer_tools._is_blocked_window("cmd.exe", "") is True
    assert computer_tools._is_blocked_window("notepad.exe", "Command Prompt") is False


def test_computer_functions_act_rejects_stale_state_id():
    result = json.loads(
        computer_tools._computer_functions_act(
            state_id="cfs_missing",
            actions=[{"type": "set_value", "element_index": 1, "value": "x"}],
            _bypass_gate=True,
        )
    )

    assert result["status"] == "error"
    assert result["reason_code"] == "stale_state"


def test_computer_functions_act_blocks_terminal_launch():
    result = json.loads(
        computer_tools._computer_functions_act(
            actions=[{"type": "launch_app", "app": "cmd.exe"}],
            _bypass_gate=True,
        )
    )

    assert result["status"] == "blocked"
    assert result["results"][0]["reason_code"] == "blocked_app"


def test_computer_functions_act_approval_payload_contains_batch(monkeypatch):
    window = {"hwnd": 123, "title": "Notepad", "process_name": "notepad.exe"}
    captured = {}
    monkeypatch.setattr(computer_tools, "_resolve_window", lambda **_kwargs: {"status": "ok", "window": window})
    monkeypatch.setattr(
        computer_tools,
        "resolve_permission",
        lambda *_args, **_kwargs: PermissionDecision(
            allowed=True,
            requires_confirmation=True,
            reason="confirm",
            reason_code="confirmation_required",
        ),
    )

    def fake_create_ticket(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="ticket-1")

    monkeypatch.setattr(computer_tools, "create_ticket", fake_create_ticket)

    result = json.loads(
        computer_tools._computer_functions_act(
            window=window,
            actions=[{"type": "click", "x": 1, "y": 2}],
        )
    )

    assert result["status"] == "pending_approval"
    assert captured["tool_name"] == "computer_functions_act"
    assert captured["payload"]["args"]["window"]["hwnd"] == 123
    assert captured["payload"]["args"]["actions"] == [{"type": "click", "x": 1, "y": 2}]


def test_computer_functions_act_launch_uses_batch_approval(monkeypatch):
    captured = {}
    launch_calls = []
    monkeypatch.setattr(
        computer_tools,
        "resolve_permission",
        lambda *_args, **_kwargs: PermissionDecision(
            allowed=True,
            requires_confirmation=True,
            reason="confirm",
            reason_code="confirmation_required",
        ),
    )
    monkeypatch.setattr(
        computer_tools.desktop_runtime,
        "launch_app",
        lambda *args, **kwargs: launch_calls.append((args, kwargs)) or json.dumps({"status": "launched"}),
    )

    def fake_create_ticket(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="ticket-2")

    monkeypatch.setattr(computer_tools, "create_ticket", fake_create_ticket)

    result = json.loads(
        computer_tools._computer_functions_act(
            actions=[{"type": "launch_app", "app": "notepad"}],
        )
    )

    assert result["status"] == "pending_approval"
    assert captured["tool_name"] == "computer_functions_act"
    assert captured["target_app"] == "notepad"
    assert captured["payload"]["args"]["actions"] == [{"type": "launch_app", "app": "notepad"}]
    assert launch_calls == []
