from pathlib import Path

from app.agent.skill_loader import SkillSpec
from app.agent.skill_prompt import build_skill_prompt


def _skill_body(folder: str) -> str:
    raw = (Path(__file__).parents[2] / "skills" / folder / "SKILL.md").read_text(encoding="utf-8")
    return raw.split("---", 2)[-1].strip()


def test_build_skill_prompt_uses_browser_policy_from_skill_markdown():
    prompt = build_skill_prompt(
        [SkillSpec(slug="browser-use", name="browser-use", description="Browser", version="1.0.0", body=_skill_body("browser_use"), path=None)],  # type: ignore[arg-type]
        {"mcp__chrome-devtools__take_snapshot", "mcp__chrome-devtools__evaluate_script", "screenshot"},
    )

    assert "mcp__chrome-devtools__take_snapshot" in prompt
    assert "Do not use generic screenshot-style tools to read webpage content" in prompt
    assert "mcp__chrome-devtools__evaluate_script" in prompt


def test_build_skill_prompt_steers_interactive_browsing_to_devtools_tools():
    prompt = build_skill_prompt(
        [SkillSpec(slug="browser-use", name="browser-use", description="Browser", version="1.0.0", body=_skill_body("browser_use"), path=None)],  # type: ignore[arg-type]
        {"mcp__chrome-devtools__list_pages", "mcp__chrome-devtools__take_snapshot", "browser_fetch"},
    )

    assert "mcp__chrome-devtools__list_pages" in prompt
    assert "observe -> decide -> act -> verify" in prompt
    assert "take_snapshot` again to verify" in prompt
    assert "`browser_fetch`" in prompt
    assert "stalled_repeat_detected" in prompt
    assert "recipient chip/token" in prompt
    assert "Do not repeat the same browser action" in prompt
    assert "needs_browser_render" in prompt
    # The removed browser-use engine's tools and fetch modes are not mentioned.
    for removed in ("browser_open", "browser_snapshot", "browser_click", "browser_session", 'mode="dynamic"'):
        assert removed not in prompt


def test_build_skill_prompt_does_not_synthesize_browser_policy_for_unrelated_skill():
    prompt = build_skill_prompt(
        [SkillSpec(slug="core", name="core", description="Core tools", version="1.0.0", body="", path=None)],  # type: ignore[arg-type]
        {"screenshot"},
    )

    assert "## Browser Tool Policy" not in prompt


def test_build_skill_prompt_instructs_computer_functions_flow():
    prompt = build_skill_prompt(
        [SkillSpec(slug="computer-use", name="computer-use", description="Desktop", version="1.0.0", body=_skill_body("computer_use"), path=None)],  # type: ignore[arg-type]
        {
            "computer_functions_list_apps",
            "computer_functions_get_window",
            "computer_functions_get_window_state",
            "computer_functions_act",
        },
    )

    assert "## Computer Use Policy" in prompt
    assert "computer_functions_list_apps" in prompt
    assert "computer_functions_get_window_state" in prompt
    assert "computer_functions_act" in prompt
    assert "window-relative coordinates" in prompt
