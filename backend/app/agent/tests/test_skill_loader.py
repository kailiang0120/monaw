from types import SimpleNamespace

from app.agent.settings_store import AgentSettings, build_runtime_namespace
from app.agent.skill_loader import available_skill_payload, discover_skills, load_tools


def _base_settings() -> SimpleNamespace:
    return SimpleNamespace(
        model_provider="openai",
        model_name="gpt-5.4-mini",
        openai_api_key="key",
        google_api_key="",
        tavily_api_key="",
        reasoning_effort="medium",
        allow_arbitrary_app_paths=False,
    )


def test_load_tools_returns_enabled_skills_only():
    settings_data = AgentSettings()
    runtime_settings = build_runtime_namespace(_base_settings(), settings_data)

    skills, registry = load_tools(runtime_settings)
    names = {skill.name for skill in skills}
    tool_names = {tool["name"] for tool in registry.get_all_tools()}

    assert "core" in names
    assert "exec" in names
    assert "mcp-bridge" not in names
    assert "exec" in tool_names
    assert "memory" in names
    assert "calculator" in tool_names
    assert "memory_search" in tool_names
    assert "recall_memory" in tool_names
    recall_tool = registry.get_tool("recall_memory")
    assert recall_tool is not None
    assert "Skip it if the injected memories already answer the question" in recall_tool["description"]
    assert "mcp_status" in tool_names
    assert "computer_functions_list_apps" in tool_names
    assert "computer_functions_get_window_state" in tool_names
    assert "computer_functions_act" in tool_names
    assert "screen_info" not in tool_names
    assert "inspect_ui" not in tool_names
    assert "precision_click" not in tool_names


def test_load_tools_omits_mcp_feature_when_disabled():
    settings_data = AgentSettings()
    settings_data.mcp.enabled = False
    runtime_settings = build_runtime_namespace(_base_settings(), settings_data)

    _skills, registry = load_tools(runtime_settings)
    tool_names = {tool["name"] for tool in registry.get_all_tools()}

    assert "mcp_status" not in tool_names


def test_available_skill_payload_omits_removed_email_skill():
    settings_data = AgentSettings()
    runtime_settings = build_runtime_namespace(_base_settings(), settings_data)

    payload = available_skill_payload(runtime_settings)
    names = {item["name"] for item in payload}

    assert "email-windows" not in names
    assert "mcp-bridge" not in names


def test_available_skill_payload_keeps_recommended_skills():
    settings_data = AgentSettings()
    runtime_settings = build_runtime_namespace(_base_settings(), settings_data)

    payload = available_skill_payload(runtime_settings)
    recommended = {item["name"] for item in payload if item["tier"] == "recommended"}

    assert recommended == {
        "browser-use",
        "core",
        "computer-use",
        "exec",
        "filesystem",
        "memory",
        "skill-creator",
    }
    assert "background-check" not in {item["name"] for item in payload}


def test_discover_skills_marks_python_module_requirement_as_missing_env(tmp_path, monkeypatch):
    skill_dir = tmp_path / "external_lookup"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: external-lookup
description: External lookup
metadata:
  openclaw:
    requires:
      python_module:
        - definitely_missing_module
---
External lookup skill
""",
        encoding="utf-8",
    )

    monkeypatch.setattr("app.agent.skill_loader.importlib.util.find_spec", lambda name: None)

    runtime_settings = build_runtime_namespace(_base_settings(), AgentSettings())

    skills = discover_skills(runtime_settings, skills_dir=tmp_path)

    assert len(skills) == 1
    assert skills[0].available is False
    assert skills[0].unavailable_reason == "missing_env"


def test_load_tools_isolates_broken_skill(tmp_path):
    good_dir = tmp_path / "good_skill"
    good_dir.mkdir()
    (good_dir / "SKILL.md").write_text(
        """---
name: good-skill
description: Good
---
Good skill
""",
        encoding="utf-8",
    )
    (good_dir / "tools.py").write_text(
        "def register_tools(registry, settings=None):\n"
        "    registry.register({'name':'good_tool','description':'ok','parameters':{'type':'object','properties':{},'required':[]},'callable':lambda:'ok','domain':'general','execution_mode':'sync_stateless','affinity_group':None})\n",
        encoding="utf-8",
    )
    bad_dir = tmp_path / "bad_skill"
    bad_dir.mkdir()
    (bad_dir / "SKILL.md").write_text(
        """---
name: bad-skill
description: Bad
---
Bad skill
""",
        encoding="utf-8",
    )
    (bad_dir / "tools.py").write_text("raise RuntimeError('broken import')\n", encoding="utf-8")

    runtime_settings = build_runtime_namespace(_base_settings(), AgentSettings())
    skills, registry = load_tools(runtime_settings, skills_dir=tmp_path)

    assert registry.get_tool("good_tool") is not None
    bad_skill = next(skill for skill in skills if skill.name == "bad-skill")
    assert bad_skill.available is False
    assert bad_skill.unavailable_reason == "load_error"


def test_load_tools_does_not_leak_partially_registered_tools(tmp_path):
    partial_dir = tmp_path / "partial_skill"
    partial_dir.mkdir()
    (partial_dir / "SKILL.md").write_text(
        """---
name: partial-skill
description: Partial
---
Partial skill
""",
        encoding="utf-8",
    )
    (partial_dir / "tools.py").write_text(
        "def register_tools(registry, settings=None):\n"
        "    registry.register({'name':'leaked_tool','description':'bad','parameters':{'type':'object','properties':{},'required':[]},'callable':lambda:'bad','domain':'general','execution_mode':'sync_stateless','affinity_group':None})\n"
        "    raise RuntimeError('boom after register')\n",
        encoding="utf-8",
    )

    runtime_settings = build_runtime_namespace(_base_settings(), AgentSettings())
    skills, registry = load_tools(runtime_settings, skills_dir=tmp_path)

    assert registry.get_tool("leaked_tool") is None
    partial_skill = next(skill for skill in skills if skill.name == "partial-skill")
    assert partial_skill.available is False
    assert partial_skill.unavailable_reason == "load_error"


def test_load_tools_isolates_builtin_mcp_failure(tmp_path):
    core_dir = tmp_path / "core"
    core_dir.mkdir()
    (core_dir / "SKILL.md").write_text(
        """---
name: core
description: Core
---
Core
""",
        encoding="utf-8",
    )
    (core_dir / "tools.py").write_text(
        "def register_tools(registry, settings=None):\n"
        "    registry.register({'name':'ok_tool','description':'ok','parameters':{'type':'object','properties':{},'required':[]},'callable':lambda:'ok','domain':'general','execution_mode':'sync_stateless','affinity_group':None})\n",
        encoding="utf-8",
    )

    mcp_dir = tmp_path / "mcp_bridge"
    mcp_dir.mkdir()
    (mcp_dir / "tools.py").write_text("raise RuntimeError('mcp broken')\n", encoding="utf-8")

    runtime_settings = build_runtime_namespace(_base_settings(), AgentSettings())
    _skills, registry = load_tools(runtime_settings, skills_dir=tmp_path)

    assert registry.get_tool("ok_tool") is not None
