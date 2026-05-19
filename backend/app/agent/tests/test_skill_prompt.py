from app.agent.skill_loader import SkillSpec
from app.agent.skill_prompt import build_skill_prompt


def test_build_skill_prompt_prefers_chrome_mcp_snapshot_for_webpages():
    prompt = build_skill_prompt(
        [SkillSpec(slug="core", name="core", description="Core tools", version="1.0.0", body="", path=None)],  # type: ignore[arg-type]
        {"mcp__Chrome-dev-tools__take_snapshot", "mcp__Chrome-dev-tools__evaluate_script", "screenshot"},
    )

    assert "mcp__Chrome-dev-tools__take_snapshot" in prompt
    assert "Do not use generic screenshot-style tools to read webpage content" in prompt
    assert "mcp__Chrome-dev-tools__evaluate_script" in prompt


def test_build_skill_prompt_instructs_browser_resume_and_field_verification():
    prompt = build_skill_prompt(
        [SkillSpec(slug="browser-use", name="browser-use", description="Browser", version="1.0.0", body="", path=None)],  # type: ignore[arg-type]
        {"browser_open", "browser_tabs", "browser_snapshot", "browser_type"},
    )

    assert "When resuming browser work" in prompt
    assert "browser_tabs" in prompt
    assert "stalled_repeat_detected" in prompt
    assert 'browser_session(action="use_system")' in prompt
    assert "observe -> decide -> act -> verify" in prompt
    assert "target_hint" in prompt
    assert "field_candidates" in prompt
    assert "recipient, subject, and message body refs separately" in prompt
    assert "recipient chip/token" in prompt
    assert "browser_snapshot(include_screenshot=true, limit=120)" in prompt
    assert "Do not repeat the same browser action" in prompt
    assert "target_after" in prompt
    assert "browser_full_page_screenshot" in prompt


def test_build_skill_prompt_omits_browser_policy_without_chrome_snapshot():
    prompt = build_skill_prompt(
        [SkillSpec(slug="core", name="core", description="Core tools", version="1.0.0", body="", path=None)],  # type: ignore[arg-type]
        {"screenshot"},
    )

    assert "## Browser Tool Policy" not in prompt


def test_build_skill_prompt_instructs_desktop_precision_clicking():
    prompt = build_skill_prompt(
        [SkillSpec(slug="computer-use", name="computer-use", description="Desktop", version="1.0.0", body="", path=None)],  # type: ignore[arg-type]
        {"screen_info", "inspect_ui", "click_ui_element", "precision_click", "scroll"},
    )

    assert "## Computer Use Policy" in prompt
    assert "inspect_ui" in prompt
    assert "precision_click" in prompt
    assert "pass `x` and `y` to `scroll`" in prompt
