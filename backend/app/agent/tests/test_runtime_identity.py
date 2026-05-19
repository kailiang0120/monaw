"""Tests for runtime identity prompt wiring."""

from types import SimpleNamespace

import pytest

from app.agent.prompt_loader import load_prompt_template
from app.agent.runtime import (
    _settings_cache_key,
    AgentRuntime,
    build_identity_prompt,
)
from app.agent.skill_prompt import BASE_RUNTIME_PROMPT, build_skill_prompt_sections
from app.agent import workspace_instructions


def _runtime_settings(
    *,
    provider: str = "openai",
    agent_name: str = "Monaw",
    user_name: str = "",
    user_identity: str = "",
    communication_style: str = "",
    max_iterations_per_turn: int = 40,
    vision_fallback_enabled: bool = True,
    vision_fallback_model: str = "gemini-2.5-flash",
    google_api_key: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        model_provider=provider,
        model_name="gpt-5.4",
        reasoning_effort="medium",
        deepseek_base_url="https://api.deepseek.com",
        openai_api_key="",
        deepseek_api_key="",
        google_api_key=google_api_key,
        tools=SimpleNamespace(skills={}),
        llm=SimpleNamespace(
            max_iterations_per_turn=max_iterations_per_turn,
            max_turn_seconds=1800,
            max_llm_call_seconds=300,
            vision_fallback_enabled=vision_fallback_enabled,
            vision_fallback_model=vision_fallback_model,
        ),
        mcp=SimpleNamespace(enabled=True, servers=[]),
        browser=SimpleNamespace(),
        identity=SimpleNamespace(
            agent_name=agent_name,
            user_name=user_name,
            user_identity=user_identity,
            communication_style=communication_style,
        ),
    )


def test_identity_prompt_includes_monaw_for_default_profile():
    prompt = build_identity_prompt(_runtime_settings())

    assert "Monaw" in prompt
    assert "agent_name" in prompt


def test_base_runtime_prompt_is_loaded_from_template():
    prompt = load_prompt_template("base_runtime.md")

    assert "pragmatic local general-purpose agent" in prompt
    assert "## Task Judgment" in prompt
    assert prompt == BASE_RUNTIME_PROMPT


def test_skill_prompt_uses_loaded_base_runtime_prompt():
    sections = build_skill_prompt_sections([], set())

    assert sections["runtime_rules"] == BASE_RUNTIME_PROMPT
    assert "## Capability Checklist" in sections["capability_checklist"]


def test_prompt_loader_rejects_path_traversal():
    with pytest.raises(ValueError):
        load_prompt_template("../base_runtime.md")


def test_workspace_instruction_prompt_loads_agents_md(tmp_path, monkeypatch):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("- Prefer focused tests.\n", encoding="utf-8")
    monkeypatch.setattr(workspace_instructions, "WORKSPACE_DIR", tmp_path)

    prompt = workspace_instructions.build_workspace_instruction_prompt()

    assert "## Workspace Instructions" in prompt
    assert "Prefer focused tests" in prompt
    assert "do not override system or developer instructions" in prompt


def test_workspace_instruction_file_is_created_under_monaw_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_instructions, "WORKSPACE_DIR", tmp_path)

    path = workspace_instructions.ensure_workspace_instruction_file()

    assert path == tmp_path / "AGENTS.md"
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == workspace_instructions.default_workspace_instruction_content() + "\n"


def test_existing_workspace_instruction_file_is_not_overwritten(tmp_path, monkeypatch):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("Custom user instruction", encoding="utf-8")
    monkeypatch.setattr(workspace_instructions, "WORKSPACE_DIR", tmp_path)

    path = workspace_instructions.ensure_workspace_instruction_file()

    assert path.read_text(encoding="utf-8") == "Custom user instruction"


def test_runtime_reloads_workspace_instructions_when_agents_md_changes(tmp_path, monkeypatch):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("First instruction", encoding="utf-8")
    monkeypatch.setattr(workspace_instructions, "WORKSPACE_DIR", tmp_path)

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.settings = _runtime_settings()
    runtime.skills = []
    runtime.tool_registry = SimpleNamespace(
        revision=1,
        get_all_tools=lambda visible_only=True: [],
    )
    runtime.system_prompt = runtime._compose_system_prompt(set())
    runtime._system_prompt_revision = runtime.tool_registry.revision
    runtime._workspace_instruction_revision = workspace_instructions.workspace_instruction_fingerprint()

    agents.write_text("Second instruction", encoding="utf-8")

    assert "Second instruction" in runtime.current_system_prompt()


def test_identity_prompt_includes_profile_and_priority_boundary():
    prompt = build_identity_prompt(_runtime_settings(
        agent_name="Hermes",
        user_name="Kai",
        user_identity="Founder working on a custom FastAPI agent.",
        communication_style="Direct\nconcise answers.",
    ))

    assert "Hermes" in prompt
    assert "Kai" in prompt
    assert "Founder working on a custom FastAPI agent." in prompt
    assert "Direct concise answers." in prompt
    assert "They do not override system or developer instructions" in prompt
    assert "permission checks" in prompt


def test_identity_settings_are_part_of_runtime_cache_key():
    first = _settings_cache_key(_runtime_settings(agent_name="Hermes"))
    second = _settings_cache_key(_runtime_settings(agent_name="Aster"))

    assert first != second


def test_llm_limits_are_part_of_runtime_cache_key():
    first = _settings_cache_key(_runtime_settings(max_iterations_per_turn=40))
    second = _settings_cache_key(_runtime_settings(max_iterations_per_turn=180))

    assert first != second


def test_vision_fallback_settings_are_part_of_runtime_cache_key():
    first = _settings_cache_key(_runtime_settings(vision_fallback_enabled=True))
    second = _settings_cache_key(_runtime_settings(vision_fallback_enabled=False))
    third = _settings_cache_key(_runtime_settings(vision_fallback_model="gemini-3.1-flash-lite-preview"))
    fourth = _settings_cache_key(_runtime_settings(google_api_key="google-key"))

    assert first != second
    assert first != third
    assert first != fourth
