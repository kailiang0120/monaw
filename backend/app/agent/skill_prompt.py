"""System prompt construction for the skill runtime."""

from __future__ import annotations

from app.agent.prompt_loader import load_prompt_template
from app.agent.skill_loader import SkillSpec

BASE_RUNTIME_PROMPT = load_prompt_template("base_runtime.md")


def build_skill_prompt_sections(skills: list[SkillSpec], tool_names: set[str] | None = None) -> dict[str, str]:
    checklist_lines = [
        f"- `{skill.name}`: {skill.description or 'No description provided.'}"
        for skill in skills
    ] or ["- No enabled skills available."]

    skill_sections = []
    for skill in skills:
        body = skill.body.strip()
        if body:
            skill_sections.append(f"### {skill.name}\n{body}")

    return {
        "runtime_rules": BASE_RUNTIME_PROMPT.strip(),
        "capability_checklist": "## Capability Checklist\n" + "\n".join(checklist_lines),
        # Kept as empty compatibility sections for the context-usage route. Skill-specific
        # policy belongs in each owning SKILL.md body and is included below.
        "browser_policy": "",
        "computer_use_policy": "",
        "skills_available": "## Skills Available\n"
        + ("\n\n".join(skill_sections) if skill_sections else "No skill bodies available."),
    }


def build_skill_prompt(skills: list[SkillSpec], tool_names: set[str] | None = None) -> str:
    sections = build_skill_prompt_sections(skills, tool_names)
    ordered = [
        sections["runtime_rules"],
        sections["capability_checklist"],
        sections["browser_policy"],
        sections["computer_use_policy"],
        sections["skills_available"],
    ]
    return "\n\n".join(part for part in ordered if part).strip()
