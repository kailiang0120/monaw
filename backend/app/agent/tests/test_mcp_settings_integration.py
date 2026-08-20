from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.routes.settings import _settings_patch_from_body
from app.agent.runtime import _settings_cache_key
from app.schemas import SettingsUpdate


def _runtime_settings(*, servers: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        model_provider="openai",
        model_name="gpt-5.6-luna",
        openai_api_key="key",
        google_api_key="",
        reasoning_effort="medium",
        tools=SimpleNamespace(skills={"core": True}),
        mcp=SimpleNamespace(enabled=True, servers=servers),
    )


def test_settings_patch_from_body_includes_mcp_servers():
    body = SettingsUpdate.model_validate(
        {
            "mcp": {
                "enabled": True,
                "servers": [
                    {
                        "name": "filesystem",
                        "enabled": True,
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", r"C:\Repo"],
                        "env": {"MCP_LOG_LEVEL": "debug"},
                        "cwd": r"C:\Repo",
                        "url": "",
                        "headers": {},
                        "startup_timeout_ms": 9000,
                        "call_timeout_ms": 45000,
                        "reconnect_on_unhealthy": True,
                        "allow_list": ["list_directory"],
                        "trusted_tools": ["read_file"],
                        "tool_risk_overrides": {"write_file": "high"},
                        "description": "Filesystem server",
                    }
                ]
            }
        }
    )

    patch = _settings_patch_from_body(body)

    assert patch["mcp"]["servers"][0]["name"] == "filesystem"
    assert patch["mcp"]["servers"][0]["command"] == "npx"
    assert patch["mcp"]["servers"][0]["allow_list"] == ["list_directory"]
    assert patch["mcp"]["servers"][0]["trusted_tools"] == ["read_file"]
    assert patch["mcp"]["servers"][0]["tool_risk_overrides"] == {"write_file": "high"}
    assert patch["mcp"]["enabled"] is True


def test_settings_patch_from_body_includes_streamable_http_mcp_server():
    body = SettingsUpdate.model_validate(
        {
            "mcp": {
                "enabled": False,
                "servers": [
                    {
                        "name": "remote",
                        "enabled": True,
                        "transport": "streamable_http",
                        "url": "http://127.0.0.1:3000/mcp",
                        "headers": {"Authorization": "Bearer token"},
                        "startup_timeout_ms": 9000,
                        "call_timeout_ms": 45000,
                        "reconnect_on_unhealthy": False,
                    }
                ]
            }
        }
    )

    patch = _settings_patch_from_body(body)

    server = patch["mcp"]["servers"][0]
    assert server["transport"] == "streamable_http"
    assert server["url"] == "http://127.0.0.1:3000/mcp"
    assert server["headers"] == {"Authorization": "Bearer token"}
    assert server["reconnect_on_unhealthy"] is False
    assert patch["mcp"]["enabled"] is False


def test_settings_patch_from_body_includes_llm_fields():
    body = SettingsUpdate.model_validate(
        {
            "llm": {
                "provider": "gemini",
                "model_name": "gemini-3.1-pro-preview",
                "reasoning_effort": "high",
            }
        }
    )

    patch = _settings_patch_from_body(body)

    assert patch["llm"]["provider"] == "gemini"
    assert patch["llm"]["model_name"] == "gemini-3.1-pro-preview"
    assert patch["llm"]["reasoning_effort"] == "high"
    assert "vision_fallback_enabled" not in patch["llm"]
    assert "vision_fallback_model" not in patch["llm"]


def test_settings_update_rejects_duplicate_mcp_server_names():
    with pytest.raises(ValidationError, match="MCP server names must be unique"):
        SettingsUpdate.model_validate(
            {
                "mcp": {
                    "servers": [
                        {"name": "duplicate", "transport": "stdio"},
                        {"name": "duplicate", "transport": "stdio"},
                    ]
                }
            }
        )


def test_settings_cache_key_changes_when_mcp_servers_change():
    baseline = _settings_cache_key(_runtime_settings(servers=[]))
    with_server = _settings_cache_key(
        _runtime_settings(
            servers=[
                {
                    "name": "filesystem",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", r"C:\Repo"],
                }
            ]
        )
    )

    assert baseline != with_server


def test_settings_cache_key_changes_when_mcp_feature_toggle_changes():
    enabled = _settings_cache_key(_runtime_settings(servers=[]))
    disabled_runtime = _runtime_settings(servers=[])
    disabled_runtime.mcp.enabled = False

    assert enabled != _settings_cache_key(disabled_runtime)
