"""Tests for canonical runtime settings storage and migration."""

import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agent.settings_store import (
    AgentSettings,
    AppRule,
    BrowserUseSettings,
    MCPServerConfig,
    PathRule,
    _LEGACY_BROWSER_DOWNLOADS_DIR,
    _LEGACY_BROWSER_SCREENSHOTS_DIR,
    confirmation_settings_for_mode,
    api_settings_payload,
    build_runtime_namespace,
    load_agent_settings,
    merge_agent_settings,
    save_agent_settings,
)
from app.agent.runtime_paths import MONAW_HOME_DIR


def _workspace_tmp_dir(name: str) -> Path:
    path = Path.cwd() / f".tmp-{name}-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _base_settings() -> SimpleNamespace:
    return SimpleNamespace(
        model_provider="openai",
        model_name="gpt-5.4",
        openai_api_key="secret-openai-key",
        deepseek_api_key="secret-deepseek-key",
        deepseek_base_url="https://api.deepseek.com",
        google_api_key="secret-google-key",
        tavily_api_key="tvly",
        telegram_allowed_user_ids="",
        telegram_allowed_chat_ids="",
        reasoning_effort="medium",
        vision_fallback_enabled=True,
        vision_fallback_model="gemini-2.5-flash",
        allow_arbitrary_app_paths=False,
    )


def test_migrates_legacy_policy_into_settings_json(tmp_path):
    settings_path = tmp_path / "settings.json"
    legacy_path = tmp_path / "controller_policy.json"
    legacy_path.write_text(json.dumps({
        "mode": "user_config",
        "permitted_roots": [r"C:\Work"],
        "blocked_roots": [r"C:\Windows"],
        "dangerous_actions_require_confirm": True,
        "allowlisted_apps": [
            {"alias": "myapp", "display_name": "My App", "exe_paths": [r"C:\Apps\myapp.exe"]},
        ],
    }), encoding="utf-8")

    settings_data = load_agent_settings(
        _base_settings(),
        settings_path=settings_path,
        legacy_policy_path=legacy_path,
    )

    assert settings_path.exists()
    assert settings_data.permissions.mode == "custom"
    assert settings_data.permissions.path_rules[0].path == r"C:\Work"
    assert settings_data.permissions.app_rules[0].alias == "myapp"


def test_round_trip_preserves_folder_and_app_rules(tmp_path):
    settings_path = tmp_path / "settings.json"
    current = AgentSettings()
    current.permissions.path_rules = [
        PathRule(path=r"C:\Projects", read=True, write=True, delete=False, launch=False, enabled=True)
    ]
    current.permissions.app_rules = [
        AppRule(alias="custom-tool", display_name="Custom Tool", exe_paths=[r"C:\Apps\custom-tool.exe"])
    ]

    save_agent_settings(current, settings_path=settings_path)
    loaded = load_agent_settings(settings_path=settings_path)

    assert loaded.permissions.path_rules[0].path == r"C:\Projects"
    assert loaded.permissions.path_rules[0].delete is False
    assert loaded.permissions.app_rules[0].alias == "custom-tool"


def test_round_trip_preserves_mcp_servers(tmp_path):
    settings_path = tmp_path / "settings.json"
    current = AgentSettings()
    current.mcp.servers = [
        MCPServerConfig(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", r"C:\Work"],
            env={"MCP_LOG_LEVEL": "debug"},
            cwd=r"C:\Work",
            startup_timeout_ms=9000,
            call_timeout_ms=45000,
            allow_list=["list_directory", "read_file"],
            description="Filesystem access",
        )
    ]

    save_agent_settings(current, settings_path=settings_path)
    loaded = load_agent_settings(settings_path=settings_path)

    assert loaded.mcp.servers[0].name == "filesystem"
    assert loaded.mcp.servers[0].command == "npx"
    assert loaded.mcp.servers[0].allow_list == ["list_directory", "read_file"]


def test_round_trip_preserves_streamable_http_mcp_servers(tmp_path):
    settings_path = tmp_path / "settings.json"
    current = AgentSettings()
    current.mcp.servers = [
        MCPServerConfig(
            name="remote",
            transport="streamable_http",
            url="http://127.0.0.1:3000/mcp",
            headers={"Authorization": "Bearer token"},
            reconnect_on_unhealthy=False,
            description="Remote MCP",
        )
    ]

    save_agent_settings(current, settings_path=settings_path)
    loaded = load_agent_settings(settings_path=settings_path)

    assert loaded.mcp.servers[0].transport == "streamable_http"
    assert loaded.mcp.servers[0].url == "http://127.0.0.1:3000/mcp"
    assert loaded.mcp.servers[0].headers == {"Authorization": "Bearer token"}
    assert loaded.mcp.servers[0].reconnect_on_unhealthy is False


def test_round_trip_preserves_identity_settings(tmp_path):
    settings_path = tmp_path / "settings.json"
    current = AgentSettings()
    current.identity.agent_name = "Hermes"
    current.identity.user_name = "Kai"
    current.identity.user_identity = "Builder working on Windows automation."
    current.identity.communication_style = "Be direct and concise."

    save_agent_settings(current, settings_path=settings_path)
    loaded = load_agent_settings(settings_path=settings_path)

    assert loaded.identity.agent_name == "Hermes"
    assert loaded.identity.user_name == "Kai"
    assert loaded.identity.user_identity == "Builder working on Windows automation."
    assert loaded.identity.communication_style == "Be direct and concise."


def test_identity_defaults_to_monaw_for_blank_or_legacy_agent_name():
    assert AgentSettings().identity.agent_name == "Monaw"
    assert AgentSettings.model_validate({"identity": {"agent_name": ""}}).identity.agent_name == "Monaw"
    assert AgentSettings.model_validate({"identity": {"agent_name": "Agent"}}).identity.agent_name == "Monaw"


def test_agent_settings_rejects_duplicate_mcp_server_names():
    with pytest.raises(ValidationError, match="MCP server names must be unique"):
        AgentSettings.model_validate(
            {
                "mcp": {
                    "servers": [
                        {"name": "duplicate"},
                        {"name": "duplicate"},
                    ]
                }
            }
        )


def test_merge_settings_update_applies_nested_patch():
    current = AgentSettings()
    updated = merge_agent_settings(current, {
        "permissions": {"mode": "custom", "allow_screen_fallback": True},
    })

    assert updated.permissions.mode == "custom"
    assert updated.permissions.allow_screen_fallback is True


def test_full_access_mode_change_enables_screen_fallback_for_clean_preset_patch():
    current = AgentSettings()

    updated = merge_agent_settings(current, {"permissions": {"mode": "full_access"}})

    assert updated.permissions.mode == "full_access"
    assert updated.permissions.allow_screen_fallback is True


def test_editing_full_access_preset_switches_to_custom():
    current = AgentSettings()
    current = merge_agent_settings(current, {"permissions": {"mode": "full_access"}})

    disabled_after_mode_change = merge_agent_settings(current, {
        "permissions": {"allow_screen_fallback": False},
    })

    assert disabled_after_mode_change.permissions.mode == "custom"
    assert disabled_after_mode_change.permissions.allow_screen_fallback is False


def test_switching_back_to_custom_restores_saved_custom_profile():
    current = merge_agent_settings(AgentSettings(), {
        "permissions": {
            "mode": "custom",
            "confirmations": {
                "mutate": False,
                "delete": False,
                "launch_app": False,
                "click": False,
                "type": True,
            },
            "allow_delete": True,
            "dangerous_actions_require_confirm": False,
            "allow_screen_fallback": False,
        },
    })

    full_access = merge_agent_settings(current, {"permissions": {"mode": "full_access"}})
    restored = merge_agent_settings(full_access, {"permissions": {"mode": "custom"}})

    assert full_access.permissions.mode == "full_access"
    assert full_access.permissions.allow_screen_fallback is True
    assert restored.permissions.mode == "custom"
    assert restored.permissions.allow_delete is True
    assert restored.permissions.allow_screen_fallback is False
    assert restored.permissions.confirmations.type is True
    assert restored.permissions.confirmations.delete is False


def test_merge_settings_update_applies_identity_patch():
    current = AgentSettings()
    updated = merge_agent_settings(current, {
        "identity": {
            "agent_name": "Hermes",
            "user_name": "Kai",
            "communication_style": "Use short answers.",
        },
    })

    assert updated.identity.agent_name == "Hermes"
    assert updated.identity.user_name == "Kai"
    assert updated.identity.communication_style == "Use short answers."


def test_merge_settings_update_applies_llm_runtime_limit_patch():
    current = AgentSettings()
    updated = merge_agent_settings(current, {
        "llm": {"max_iterations_per_turn": 180},
    })

    assert updated.llm.max_iterations_per_turn == 180


def test_gemini_provider_uses_chat_model_list_not_vision_fallback_models():
    settings_data = AgentSettings.model_validate(
        {
            "llm": {
                "provider": "gemini",
                "model_name": "gemini-2.5-flash",
                "reasoning_effort": "medium",
            }
        }
    )

    assert settings_data.llm.model_name == "gemini-3.1-pro-preview"


def test_default_skills_use_recommended_profile():
    settings_data = AgentSettings()

    assert settings_data.llm.provider == "openai"
    assert settings_data.llm.model_name == "gpt-5.4"
    assert settings_data.tools.skills == {
        "core": True,
        "exec": True,
        "computer-use": True,
        "filesystem": True,
        "memory": True,
        "background-check": False,
        "browser-use": True,
    }
    assert settings_data.mcp.enabled is True


def test_default_browser_artifacts_live_under_monaw_home():
    browser = BrowserUseSettings()

    assert Path(browser.downloads_dir).is_relative_to(MONAW_HOME_DIR / "browser")
    assert Path(browser.screenshots_dir).is_relative_to(MONAW_HOME_DIR / "browser")
    assert Path(browser.traces_dir).is_relative_to(MONAW_HOME_DIR / "browser")
    assert Path(browser.managed_profile_dir).is_relative_to(MONAW_HOME_DIR / "browser")


def test_load_settings_preserves_legacy_skill_defaults_when_keys_are_missing(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "llm": {
            "provider": "openai",
            "model_name": "gpt-5.4-mini",
            "reasoning_effort": "medium",
        },
        "tools": {
            "skills": {
                "core": True,
                "exec": True,
                "computer-use": True,
                "filesystem": True,
            }
        },
        "permissions": {
            "mode": "default",
            "confirmations": {
                "mutate": True,
                "delete": True,
                "launch_app": True,
                "click": True,
                "type": True,
            },
            "blocked_roots": [],
            "path_rules": [],
            "app_rules": [],
            "allow_delete": False,
            "dangerous_actions_require_confirm": True,
            "allow_screen_fallback": False,
        },
    }), encoding="utf-8")

    loaded = load_agent_settings(settings_path=settings_path)

    assert loaded.tools.skills == {
        "core": True,
        "exec": True,
        "computer-use": True,
        "filesystem": True,
        "memory": True,
        "background-check": False,
        "browser-use": True,
    }
    assert loaded.mcp.enabled is True


def test_load_settings_migrates_legacy_desktop_control_skill_name(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "tools": {
            "skills": {
                "core": True,
                "exec": True,
                "desktop-control-win": False,
                "filesystem": True,
            }
        }
    }), encoding="utf-8")

    loaded = load_agent_settings(settings_path=settings_path)

    assert "desktop-control-win" not in loaded.tools.skills
    assert loaded.tools.skills["computer-use"] is False


def test_load_settings_migrates_legacy_hidden_artifact_dirs(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "browser": {
            "downloads_dir": str(_LEGACY_BROWSER_DOWNLOADS_DIR),
            "screenshots_dir": str(_LEGACY_BROWSER_SCREENSHOTS_DIR),
        },
    }), encoding="utf-8")

    loaded = load_agent_settings(settings_path=settings_path)
    defaults = BrowserUseSettings()

    assert loaded.browser.downloads_dir == defaults.downloads_dir
    assert loaded.browser.screenshots_dir == defaults.screenshots_dir


def test_load_settings_migrates_legacy_mcp_skill_flag_to_feature_enabled(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "tools": {"skills": {"core": True, "mcp-bridge": False}},
        "mcp": {"servers": []},
    }), encoding="utf-8")

    loaded = load_agent_settings(settings_path=settings_path)

    assert "mcp-bridge" not in loaded.tools.skills
    assert loaded.mcp.enabled is False


def test_load_settings_normalizes_preset_mode_confirmations(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "llm": {
            "provider": "openai",
            "model_name": "gpt-5.4-mini",
            "reasoning_effort": "high",
        },
        "tools": {"skills": {"core": True}},
        "permissions": {
            "mode": "full_access",
            "confirmations": {
                "mutate": True,
                "delete": True,
                "launch_app": True,
                "click": True,
                "type": True,
            },
            "blocked_roots": [],
            "path_rules": [],
            "app_rules": [],
            "allow_delete": False,
            "dangerous_actions_require_confirm": False,
            "allow_screen_fallback": True,
        },
    }), encoding="utf-8")

    loaded = load_agent_settings(settings_path=settings_path)

    assert loaded.permissions.confirmations.model_dump() == confirmation_settings_for_mode("full_access").model_dump()


def test_load_settings_normalizes_preset_mode_risk_gates():
    tmp_dir = _workspace_tmp_dir("settings-preset-risk")
    try:
        settings_path = tmp_dir / "settings.json"
        settings_path.write_text(json.dumps({
            "permissions": {
                "mode": "default",
                "confirmations": {
                    "mutate": False,
                    "delete": False,
                    "launch_app": False,
                    "click": False,
                    "type": False,
                },
                "allow_delete": True,
                "dangerous_actions_require_confirm": False,
                "allow_screen_fallback": True,
            },
        }), encoding="utf-8")

        loaded = load_agent_settings(settings_path=settings_path)

        assert loaded.permissions.mode == "default"
        assert loaded.permissions.confirmations.model_dump() == confirmation_settings_for_mode("default").model_dump()
        assert loaded.permissions.allow_delete is False
        assert loaded.permissions.dangerous_actions_require_confirm is True
        assert loaded.permissions.allow_screen_fallback is False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_api_payload_exposes_key_status_without_secret_values():
    base = _base_settings()
    base.telegram_allowed_user_ids = "123456789"
    payload = api_settings_payload(base, AgentSettings())

    assert payload["api_keys"]["has_google_key"] is True
    assert payload["api_keys"]["has_deepseek_key"] is True
    assert payload["api_keys"]["has_tavily_key"] is True
    assert payload["api_keys"]["has_telegram_allowlist"] is True
    assert payload["telegram_allowed_user_ids"] == "123456789"
    assert "secret-google-key" not in json.dumps(payload)
    assert "secret-deepseek-key" not in json.dumps(payload)


def test_runtime_namespace_uses_settings_json_values():
    settings_data = AgentSettings()
    settings_data.llm.provider = "openai"
    settings_data.mcp.servers = [
        MCPServerConfig(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", r"C:\Repo"],
        )
    ]

    runtime = build_runtime_namespace(_base_settings(), settings_data)

    assert runtime.model_provider == "openai"
    assert runtime.mcp.servers[0]["name"] == "filesystem"
    assert runtime.mcp.servers[0]["command"] == "npx"


def test_runtime_namespace_includes_identity_settings():
    settings_data = AgentSettings()
    settings_data.identity.agent_name = "Hermes"
    settings_data.identity.user_name = "Kai"

    runtime = build_runtime_namespace(_base_settings(), settings_data)

    assert runtime.identity.agent_name == "Hermes"
    assert runtime.identity.user_name == "Kai"


def test_runtime_namespace_includes_speech_to_text_settings():
    settings_data = AgentSettings()
    settings_data.speech_to_text.engine = "cloud"
    settings_data.speech_to_text.cloud_model = "gemini-2.5-flash"

    runtime = build_runtime_namespace(_base_settings(), settings_data)

    assert runtime.speech_to_text.engine == "cloud"
    assert runtime.speech_to_text.local_model == "base"
    assert runtime.speech_to_text.cloud_provider == "gemini"
    assert runtime.speech_to_text.cloud_model == "gemini-2.5-flash"


def test_runtime_namespace_includes_llm_runtime_limits():
    settings_data = AgentSettings()
    settings_data.llm.max_iterations_per_turn = 180
    settings_data.llm.max_turn_seconds = 3600
    settings_data.llm.max_llm_call_seconds = 600

    runtime = build_runtime_namespace(_base_settings(), settings_data)

    assert runtime.llm.max_iterations_per_turn == 180
    assert runtime.llm.max_turn_seconds == 3600
    assert runtime.llm.max_llm_call_seconds == 600


def test_runtime_namespace_includes_vision_fallback_settings():
    settings_data = AgentSettings()
    settings_data.llm.vision_fallback_enabled = False
    settings_data.llm.vision_fallback_model = "gemini-3.1-flash-lite-preview"
    settings_data.llm.vision_fallback_max_output_tokens = 1200

    runtime = build_runtime_namespace(_base_settings(), settings_data)

    assert runtime.llm.vision_fallback_enabled is False
    assert runtime.llm.vision_fallback_model == "gemini-3.1-flash-lite-preview"
    assert runtime.llm.vision_fallback_max_output_tokens == 1200


def test_runtime_namespace_includes_memory_settings():
    settings_data = AgentSettings()
    settings_data.memory.retrieval_limit = 10
    settings_data.memory.max_injected_chars = 4000
    settings_data.memory.min_relevance_score = 0.25
    settings_data.memory.maintenance_cooldown_hours = 12

    runtime = build_runtime_namespace(_base_settings(), settings_data)

    assert runtime.memory.retrieval_limit == 10
    assert runtime.memory.max_injected_chars == 4000
    assert runtime.memory.min_relevance_score == 0.25
    assert runtime.memory.maintenance_cooldown_hours == 12
