"""Tests for controller policy – persistence, permission resolver, path validation."""

import json
import os
from pathlib import Path

import pytest

from app.agent.controller_policy import (
    ActionType,
    AppEntry,
    ControllerPolicyState,
    PermissionDecision,
    PermissionMode,
    canonical,
    get_effective_app_rule,
    is_path_permitted,
    resolve_permission,
    save_policy,
)
from app.agent.settings_store import AgentSettings, AppRule, PathRule, save_agent_settings


@pytest.fixture
def tmp_policy(tmp_path, monkeypatch):
    """Patch policy dir to tmp for isolation."""
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()

    import app.agent.controller_policy as cp
    monkeypatch.setattr(cp, "_POLICY_DIR", policy_dir)
    monkeypatch.setattr(cp, "_POLICY_FILE", policy_dir / "controller_policy.md")
    monkeypatch.setattr(cp, "_ALLOWLIST_FILE", policy_dir / "allowlisted_apps.md")

    return policy_dir


@pytest.fixture
def sample_state(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    return ControllerPolicyState(
        mode=PermissionMode.DEFAULT,
        permitted_roots=[str(root)],
        blocked_roots=[str(blocked)],
        allowlisted_apps=[
            AppEntry(alias="notepad", display_name="Notepad", exe_paths=[]),
        ],
        allow_delete=False,
    )


class TestPathValidation:
    def test_permitted_path(self, sample_state, tmp_path):
        root = tmp_path / "allowed"
        assert is_path_permitted(str(root / "file.txt"), sample_state)

    def test_blocked_path(self, sample_state, tmp_path):
        blocked = tmp_path / "blocked"
        assert not is_path_permitted(str(blocked / "secret.txt"), sample_state)

    def test_outside_all_roots(self, sample_state):
        assert is_path_permitted(r"C:\Users\Test\Projects\notes.txt", sample_state)

    def test_traversal_blocked(self, sample_state, tmp_path):
        blocked = tmp_path / "blocked"
        nested = str(blocked / "nested" / "secret.txt")
        assert not is_path_permitted(nested, sample_state)


class TestPermissionResolver:
    def test_default_mode_requires_confirm_on_mutate(self, sample_state):
        dec = resolve_permission(ActionType.MUTATE, state=sample_state)
        assert dec.requires_confirmation

    def test_default_mode_requires_confirm_on_click(self, sample_state):
        dec = resolve_permission(ActionType.CLICK, state=sample_state)
        assert dec.requires_confirmation

    def test_default_mode_read_allowed(self, sample_state):
        dec = resolve_permission(ActionType.READ, state=sample_state)
        assert dec.allowed
        assert not dec.requires_confirmation

    def test_full_access_mutate_no_confirm(self, sample_state):
        sample_state.mode = PermissionMode.FULL_ACCESS
        dec = resolve_permission(ActionType.MUTATE, state=sample_state)
        assert dec.allowed
        assert not dec.requires_confirmation

    def test_full_access_delete_requires_confirm(self, sample_state):
        sample_state.mode = PermissionMode.FULL_ACCESS
        sample_state.allow_delete = True
        dec = resolve_permission(ActionType.DELETE, state=sample_state)
        assert dec.requires_confirmation

    def test_delete_blocked_until_enabled(self, sample_state):
        sample_state.mode = PermissionMode.FULL_ACCESS
        dec = resolve_permission(ActionType.DELETE, state=sample_state)
        assert dec.blocked
        assert dec.reason_code == "delete_disabled"

    def test_blocked_path(self, sample_state, tmp_path):
        blocked = tmp_path / "blocked"
        dec = resolve_permission(
            ActionType.READ,
            target_path=str(blocked / "file.txt"),
            state=sample_state,
        )
        assert dec.blocked

    def test_user_private_blocked_path_requires_access_grant(self, sample_state):
        appdata = Path.home() / "AppData"
        sample_state.blocked_roots = [str(appdata)]
        dec = resolve_permission(
            ActionType.READ,
            target_path=str(appdata / "Local" / "SomeApp" / "file.txt"),
            state=sample_state,
        )
        assert dec.requires_access_grant
        assert not dec.blocked

    def test_blocked_process(self, sample_state):
        dec = resolve_permission(
            ActionType.LAUNCH_APP,
            target_app="regedit.exe",
            state=sample_state,
        )
        assert dec.blocked

    def test_blocked_process_alias_without_extension(self, sample_state):
        dec = resolve_permission(
            ActionType.LAUNCH_APP,
            target_app="regedit",
            state=sample_state,
        )
        assert dec.blocked
        assert dec.reason_code == "blocked_process"

    def test_allowed_app(self, sample_state):
        dec = resolve_permission(
            ActionType.LAUNCH_APP,
            target_app="notepad",
            state=sample_state,
        )
        assert not dec.blocked
        assert not dec.requires_confirmation
        assert not dec.requires_access_grant

    def test_unknown_app_requires_access_grant_by_default(self, sample_state):
        # Not in sample_state allowlisted apps — triggers interactive dialog.
        dec = resolve_permission(
            ActionType.LAUNCH_APP,
            target_app="random_unknown_app_xyz",
            state=sample_state,
        )
        assert dec.requires_access_grant
        assert not dec.requires_confirmation

    def test_unknown_app_allowed_in_full_access(self, sample_state):
        sample_state.mode = PermissionMode.FULL_ACCESS
        dec = resolve_permission(
            ActionType.LAUNCH_APP,
            target_app="random_unknown_app_xyz",
            state=sample_state,
        )
        assert dec.allowed
        assert not dec.requires_confirmation
        assert not dec.requires_access_grant


class TestPolicyPersistence:
    def test_save_and_read(self, tmp_policy):
        import app.agent.controller_policy as cp
        state = ControllerPolicyState(
            mode=PermissionMode.FULL_ACCESS,
            permitted_roots=[r"C:\TestRoot"],
            blocked_roots=[],
            allowlisted_apps=[
                AppEntry(alias="myapp", display_name="My App", exe_paths=[r"C:\myapp.exe"]),
            ],
        )
        cp._write_policy_json(state)

        loaded = cp._read_policy_json()
        assert loaded["mode"] == "full_access"
        assert len(loaded["allowlisted_apps"]) == 1
        assert loaded["allowlisted_apps"][0]["alias"] == "myapp"

    def test_markdown_generated(self, tmp_policy):
        import app.agent.controller_policy as cp
        state = ControllerPolicyState()
        cp._write_policy_md(state)
        cp._write_allowlist_md(state.allowlisted_apps)

        policy_md = cp._POLICY_FILE.read_text(encoding="utf-8")
        assert "Controller Policy" in policy_md
        assert "default" in policy_md

        allowlist_md = cp._ALLOWLIST_FILE.read_text(encoding="utf-8")
        assert "Allowlisted Applications" in allowlist_md

    def test_blocked_process_rejected(self):
        from app.agent.controller_policy import add_allowlisted_app
        with pytest.raises(ValueError, match="blocked"):
            add_allowlisted_app(
                AppEntry(alias="badapp", exe_paths=["regedit.exe"])
            )

    def test_blocked_alias_rejected_without_extension(self):
        from app.agent.controller_policy import add_allowlisted_app
        with pytest.raises(ValueError, match="blocked"):
            add_allowlisted_app(AppEntry(alias="regedit", exe_paths=[]))


class TestUserConfig:
    def test_user_config_dangerous_confirm(self, sample_state):
        sample_state.mode = PermissionMode.USER_CONFIG
        sample_state.dangerous_actions_require_confirm = True
        sample_state.allow_delete = True
        dec = resolve_permission(ActionType.DELETE, state=sample_state)
        assert dec.requires_confirmation

    def test_user_config_no_dangerous_confirm(self, sample_state):
        sample_state.mode = PermissionMode.USER_CONFIG
        sample_state.dangerous_actions_require_confirm = False
        sample_state.allow_delete = True
        dec = resolve_permission(ActionType.DELETE, state=sample_state)
        assert not dec.requires_confirmation


class TestUnifiedSettingsPermissions:
    def test_settings_json_path_rule_blocks_write(self, tmp_policy, tmp_path):
        import app.agent.controller_policy as cp

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        settings_data = AgentSettings()
        settings_data.permissions.mode = "custom"
        settings_data.permissions.blocked_roots = []
        settings_data.permissions.path_rules = [
            PathRule(path=str(allowed), read=True, write=False, delete=False, launch=False, enabled=True),
        ]
        save_agent_settings(settings_data, settings_path=cp._POLICY_DIR.parent / "settings.json")

        dec = resolve_permission(ActionType.MUTATE, target_path=str(allowed / "file.txt"))
        assert dec.blocked
        assert dec.reason_code == "path_not_permitted"

    @pytest.mark.parametrize("action", [ActionType.READ, ActionType.MUTATE, ActionType.EXEC])
    def test_settings_json_unknown_path_requires_access_grant_before_action_prompt(
        self,
        tmp_policy,
        tmp_path,
        action,
    ):
        import app.agent.controller_policy as cp

        target = tmp_path / "outside" / "secret.txt"
        target.parent.mkdir()
        target.write_text("secret", encoding="utf-8")

        settings_data = AgentSettings()
        settings_data.permissions.mode = "default"
        settings_data.permissions.blocked_roots = []
        settings_data.permissions.path_rules = []
        save_agent_settings(settings_data, settings_path=cp._POLICY_DIR.parent / "settings.json")

        dec = resolve_permission(action, target_path=str(target))

        assert dec.requires_access_grant
        assert not dec.requires_confirmation
        assert dec.reason_code == "access_grant_required"

    def test_settings_json_unknown_path_allowed_in_full_access(self, tmp_policy, tmp_path):
        import app.agent.controller_policy as cp

        target = tmp_path / "outside" / "notes.txt"
        target.parent.mkdir()
        target.write_text("ok", encoding="utf-8")

        settings_data = AgentSettings()
        settings_data.permissions.mode = "full_access"
        settings_data.permissions.blocked_roots = []
        settings_data.permissions.path_rules = []
        save_agent_settings(settings_data, settings_path=cp._POLICY_DIR.parent / "settings.json")

        dec = resolve_permission(ActionType.READ, target_path=str(target))

        assert dec.allowed
        assert not dec.requires_access_grant

    def test_settings_json_app_rule_blocks_launch(self, tmp_policy):
        import app.agent.controller_policy as cp

        settings_data = AgentSettings()
        settings_data.permissions.app_rules = [
            AppRule(alias="myapp", launch_allowed=False, uia_allowed=True, enabled=True),
        ]
        save_agent_settings(settings_data, settings_path=cp._POLICY_DIR.parent / "settings.json")

        dec = resolve_permission(ActionType.LAUNCH_APP, target_app="myapp")
        assert dec.blocked
        assert dec.reason_code == "launch_not_allowed"

    def test_settings_json_delete_blocked_by_default(self, tmp_policy, tmp_path):
        import app.agent.controller_policy as cp

        target = tmp_path / "allowed" / "file.txt"
        target.parent.mkdir()
        target.write_text("x")

        settings_data = AgentSettings()
        settings_data.permissions.mode = "full_access"
        settings_data.permissions.blocked_roots = []
        save_agent_settings(settings_data, settings_path=cp._POLICY_DIR.parent / "settings.json")

        dec = resolve_permission(ActionType.DELETE, target_path=str(target))
        assert dec.blocked
        assert dec.reason_code == "delete_disabled"

    def test_settings_json_user_private_blocked_root_requires_access_grant(self, tmp_policy):
        import app.agent.controller_policy as cp

        appdata = Path.home() / "AppData"
        settings_data = AgentSettings()
        settings_data.permissions.blocked_roots = [str(appdata)]
        save_agent_settings(settings_data, settings_path=cp._POLICY_DIR.parent / "settings.json")

        dec = resolve_permission(
            ActionType.READ,
            target_path=str(appdata / "Local" / "SomeApp" / "file.txt"),
        )
        assert dec.requires_access_grant
        assert not dec.blocked

    def test_settings_json_trusted_runtime_path_overrides_blocked_root(self, tmp_policy, tmp_path, monkeypatch):
        import app.agent.controller_policy as cp

        blocked_root = tmp_path / "blocked"
        runtime_root = blocked_root / "runtime"
        runtime_root.mkdir(parents=True)
        monkeypatch.setattr(cp, "RUNTIME_DIR", runtime_root)

        settings_data = AgentSettings()
        settings_data.permissions.blocked_roots = [str(blocked_root)]
        save_agent_settings(settings_data, settings_path=cp._POLICY_DIR.parent / "settings.json")

        dec = resolve_permission(
            ActionType.READ,
            target_path=str(runtime_root / "browser-use" / "screenshots"),
        )
        assert dec.allowed
        assert not dec.blocked
        assert not dec.requires_access_grant

    def test_unknown_app_rule_without_settings_override(self, tmp_policy):
        rule = get_effective_app_rule("notepad")
        assert rule is None

    def test_quoted_app_alias_matches_allowlisted_rule(self, tmp_policy):
        import app.agent.controller_policy as cp

        settings_data = AgentSettings()
        settings_data.permissions.app_rules = [
            AppRule(alias="docker desktop", launch_allowed=True, uia_allowed=True, enabled=True),
        ]
        save_agent_settings(settings_data, settings_path=cp._POLICY_DIR.parent / "settings.json")

        dec = resolve_permission(ActionType.LAUNCH_APP, target_app="'docker desktop'")
        assert dec.allowed
        assert not dec.blocked

    def test_settings_json_unknown_app_requires_access_grant_in_default(self, tmp_policy):
        import app.agent.controller_policy as cp

        settings_data = AgentSettings()
        settings_data.permissions.mode = "default"
        save_agent_settings(settings_data, settings_path=cp._POLICY_DIR.parent / "settings.json")

        dec = resolve_permission(ActionType.LAUNCH_APP, target_app="random_unknown_app_xyz")
        assert dec.requires_access_grant
        assert not dec.requires_confirmation

    def test_settings_json_unknown_app_allowed_in_full_access(self, tmp_policy):
        import app.agent.controller_policy as cp

        settings_data = AgentSettings()
        settings_data.permissions.mode = "full_access"
        save_agent_settings(settings_data, settings_path=cp._POLICY_DIR.parent / "settings.json")

        dec = resolve_permission(ActionType.LAUNCH_APP, target_app="random_unknown_app_xyz")
        assert dec.allowed
        assert not dec.requires_confirmation
        assert not dec.requires_access_grant

    def test_settings_json_allowlisted_app_skips_default_confirmation(self, tmp_policy):
        import app.agent.controller_policy as cp

        settings_data = AgentSettings()
        settings_data.permissions.mode = "default"
        settings_data.permissions.app_rules = [
            AppRule(alias="notepad", launch_allowed=True, uia_allowed=True, enabled=True),
        ]
        save_agent_settings(settings_data, settings_path=cp._POLICY_DIR.parent / "settings.json")

        dec = resolve_permission(ActionType.LAUNCH_APP, target_app="notepad")
        assert dec.allowed
        assert not dec.requires_confirmation
        assert not dec.requires_access_grant

    def test_settings_json_app_rule_can_force_default_confirmation(self, tmp_policy):
        import app.agent.controller_policy as cp

        settings_data = AgentSettings()
        settings_data.permissions.mode = "default"
        settings_data.permissions.app_rules = [
            AppRule(
                alias="notepad",
                launch_allowed=True,
                uia_allowed=True,
                require_confirmation=True,
                enabled=True,
            ),
        ]
        save_agent_settings(settings_data, settings_path=cp._POLICY_DIR.parent / "settings.json")

        dec = resolve_permission(ActionType.LAUNCH_APP, target_app="notepad")
        assert dec.requires_confirmation
        assert not dec.requires_access_grant
