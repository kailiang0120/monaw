import json

from app.agent.approval_broker import approve_ticket, get_ticket
from app.agent.execution_resume import resume_approved_ticket
from app.agent.run_context import reset_current_interactive, set_current_interactive
from app.agent.tool_registry import ToolRegistry
from app.skills.skill_creator import tools as skill_creator_tools


def test_skill_create_requires_approval_before_writing(monkeypatch, tmp_path):
    monkeypatch.setattr(skill_creator_tools, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(skill_creator_tools, "SKILL_STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(skill_creator_tools, "SKILL_ROLLBACK_DIR", tmp_path / "rollback")
    monkeypatch.setattr(skill_creator_tools, "_enable_skill", lambda _name, _enabled: None)
    monkeypatch.setattr(skill_creator_tools, "_reload_runtime", lambda: (_ for _ in ()).throw(AssertionError("same-turn reload")))

    registry = ToolRegistry()
    skill_creator_tools.register_tools(registry, settings=object())
    tool = registry.get_tool("skill_create")
    assert tool is not None

    pending = json.loads(
        tool["callable"](
            name="demo-skill",
            description="Demo skill",
            instructions="Use for demo workflows.",
            tools_py="def register_tools(registry, settings=None):\n    pass\n",
            enabled=True,
            reload_runtime=True,
        )
    )

    assert pending["status"] == "pending_approval"
    assert not (tmp_path / "skills" / "demo_skill").exists()
    ticket = get_ticket(pending["ticket_id"])
    assert ticket is not None
    assert ticket.action_type == "skill_creator"
    assert ticket.payload["args"]["enabled"] is False
    assert ticket.payload["args"]["reload_runtime"] is False

    approved = approve_ticket(ticket.id)
    assert approved is not None
    resumed = resume_approved_ticket(approved)
    result = json.loads(resumed.execution_result or "{}")

    assert resumed.status.value == "applied"
    assert result["status"] == "ok"
    assert result["enabled"] is False
    assert result["runtime_reloaded"] is False
    assert result["content_hash"]
    assert (tmp_path / "skills" / "demo_skill" / "SKILL.md").exists()
    assert (tmp_path / "skills" / "demo_skill" / "tools.py").exists()


def test_skill_create_rejects_reserved_and_ads_names():
    assert json.loads(skill_creator_tools._raw_skill_create("con", "Desc", "Body"))["reason_code"] == "invalid_skill_name"
    assert json.loads(skill_creator_tools._raw_skill_create("demo:ads", "Desc", "Body"))["reason_code"] == "invalid_skill_name"


def test_skill_create_rejects_non_interactive_source():
    token = set_current_interactive(False)
    try:
        result = json.loads(skill_creator_tools.skill_create("demo", "Desc", "Body"))
    finally:
        reset_current_interactive(token)

    assert result["status"] == "error"
    assert result["reason_code"] == "interactive_admin_required"
