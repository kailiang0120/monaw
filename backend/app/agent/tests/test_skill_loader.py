from types import SimpleNamespace

from app.schemas import SkillDescriptorPayload
from app.agent.settings_store import AgentSettings, build_runtime_namespace
from app.agent.skill_loader import available_skill_payload, discover_skills, load_tools


def _base_settings() -> SimpleNamespace:
    return SimpleNamespace(
        model_provider="openai",
        model_name="gpt-5.6-luna",
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
    assert "mcp-bridge" in names
    assert "skill-creator" not in names
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
    assert next(item for item in payload if item["name"] == "mcp-bridge")["hidden"] is True
    browser = next(item for item in payload if item["name"] == "browser-use")
    assert browser["display_name"] == "Browser automation"
    assert browser["summary"] == "Browse, inspect, and interact with websites using managed or system Chrome."
    assert browser["load_error"] == ""


def test_available_skill_payload_keeps_recommended_skills():
    settings_data = AgentSettings()
    runtime_settings = build_runtime_namespace(_base_settings(), settings_data)

    payload = available_skill_payload(runtime_settings)
    recommended = {item["name"] for item in payload if item["tier"] == "recommended"}

    assert recommended == {
        "browser-use",
        "computer-use",
        "web-search",
    }
    assert {item["name"] for item in payload if not item["hidden"]} == {
        "browser-use",
        "computer-use",
        "scheduling",
        "skill-creator",
        "web-search",
    }
    assert {item["name"] for item in payload if item["hidden"]} >= {
        "core",
        "exec",
        "filesystem",
        "memory",
        "mcp-bridge",
    }


def test_internal_skills_are_always_enabled_but_hidden_from_settings_payload():
    settings_data = AgentSettings()
    settings_data.tools.skills.update({
        "core": False,
        "exec": False,
        "filesystem": False,
        "memory": False,
        "mcp-bridge": False,
    })
    runtime_settings = build_runtime_namespace(_base_settings(), settings_data)

    skills, registry = load_tools(runtime_settings)
    internal = {skill.name: skill for skill in skills if skill.tier == "internal"}

    assert set(internal) == {"core", "exec", "filesystem", "memory", "mcp-bridge"}
    assert all(skill.always and skill.enabled for skill in internal.values())
    assert registry.get_tool("exec") is not None
    assert registry.get_tool("mcp_status") is not None


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


def test_malformed_skill_metadata_remains_visible_with_load_error(tmp_path, monkeypatch):
    skill_dir = tmp_path / "malformed_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: [unterminated\n", encoding="utf-8")
    monkeypatch.setattr("app.agent.skill_loader.SKILLS_DIR", tmp_path)

    runtime_settings = build_runtime_namespace(_base_settings(), AgentSettings())
    payload = available_skill_payload(runtime_settings)
    malformed = next(item for item in payload if item["name"] == "malformed-skill")

    assert malformed["available"] is False
    assert malformed["unavailable_reason"] == "load_error"
    assert malformed["load_error"]
    assert malformed["hidden"] is False


def test_load_tools_isolates_broken_skill(tmp_path, monkeypatch):
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

    monkeypatch.setattr("app.agent.skill_loader.SKILLS_DIR", tmp_path)
    bad_payload = next(
        item for item in available_skill_payload(runtime_settings) if item["name"] == "bad-skill"
    )
    descriptor = SkillDescriptorPayload.model_validate(bad_payload)
    assert descriptor.load_error == "broken import"
    assert descriptor.unavailable_reason == "load_error"


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
    (mcp_dir / "SKILL.md").write_text(
        "---\nname: mcp-bridge\ntier: internal\nalways: true\n---\nMCP\n",
        encoding="utf-8",
    )
    (mcp_dir / "tools.py").write_text("raise RuntimeError('mcp broken')\n", encoding="utf-8")

    runtime_settings = build_runtime_namespace(_base_settings(), AgentSettings())
    skills, registry = load_tools(runtime_settings, skills_dir=tmp_path)

    assert registry.get_tool("ok_tool") is not None
    mcp_skill = next(skill for skill in skills if skill.name == "mcp-bridge")
    assert mcp_skill.unavailable_reason == "load_error"
    assert mcp_skill.load_error == "mcp broken"
