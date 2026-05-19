"""Windows controller behavior for unrestricted non-blocked paths."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

from app.agent.settings_store import AgentSettings, AppRule, PathRule, save_agent_settings


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Isolated workspace with patched policy merged into settings."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("hello")
    (root / "subdir").mkdir()

    import app.agent.controller_policy as cp

    state = cp.ControllerPolicyState(
        mode=cp.PermissionMode.FULL_ACCESS,
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
            if state.mode == cp.PermissionMode.DEFAULT
            else "full_access"
            if state.mode == cp.PermissionMode.FULL_ACCESS
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


def test_open_folder_unknown_path_is_allowed(workspace, tmp_path):
    from app.skills.computer_use.window_ops import _cmd_open_folder

    outside = tmp_path / "grant_open_here"
    outside.mkdir(parents=True)

    result = json.loads(_cmd_open_folder(str(outside)))
    assert result["status"] == "ok"
    assert result.get("method") == "cmd"


def test_create_folder_unknown_path_is_allowed(workspace, tmp_path):
    from app.skills.computer_use.window_ops import _cmd_create_folder

    target = str(tmp_path / "new_unknown" / "nested")
    result = json.loads(_cmd_create_folder(target))
    assert result["status"] == "ok"
    assert os.path.isdir(target)


def test_delete_unknown_path_is_blocked_until_enabled(workspace, tmp_path):
    from app.skills.computer_use.window_ops import _cmd_delete_item

    victim = tmp_path / "outside_delete" / "x.txt"
    victim.parent.mkdir(parents=True)
    victim.write_text("x")

    result = json.loads(_cmd_delete_item(str(victim)))
    assert result["status"] == "blocked"
    assert result["reason_code"] == "delete_disabled"


def test_interact_app_unknown_alias_no_longer_needs_grant(workspace, monkeypatch):
    from app.skills.computer_use import window_ops as wc

    monkeypatch.setattr(wc, "_uia_available", lambda: True)
    fake_desktop = SimpleNamespace(windows=lambda: [])
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda backend=None: fake_desktop))

    result = json.loads(wc._uia_interact_app("totally_unknown_app_xyz", "get_elements", {}))
    assert result["status"] == "error"
    assert result["reason_code"] == "window_not_found"


def test_launch_app_session_grant_can_run_without_persisted_allowlist(workspace, monkeypatch):
    import app.agent.controller_policy as cp
    import app.agent.access_grant_broker as grants
    import app.skills.computer_use.desktop_runtime as dr

    workspace["state"].mode = cp.PermissionMode.DEFAULT
    workspace["state"].allowlisted_apps = []
    workspace["write_state"]()

    monkeypatch.setattr(cp, "_check_session_grant", lambda target_type, identifier: target_type == "app" and identifier == "notepad")
    monkeypatch.setattr(grants, "_discover_exe_for_alias", lambda _alias: "notepad.exe")
    monkeypatch.setattr(
        dr.subprocess,
        "Popen",
        lambda *_args, **_kwargs: SimpleNamespace(pid=7777),
    )

    result = json.loads(dr.launch_app("notepad"))
    assert result["status"] == "launched"
    assert result["pid"] == 7777


def test_delete_permitted_path_needs_enable_then_approval(workspace):
    from app.skills.computer_use.window_ops import _cmd_delete_item

    root = workspace["root"]
    workspace["state"].allow_delete = True
    workspace["write_state"]()
    target = str(root / "file.txt")
    assert os.path.exists(target)

    result = json.loads(_cmd_delete_item(target))
    assert result["status"] == "pending_approval"
    assert "ticket_id" in result
