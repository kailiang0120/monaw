"""System prompt construction for the skill runtime."""

from __future__ import annotations

from app.agent.prompt_loader import load_prompt_template
from app.agent.skill_loader import SkillSpec

BASE_RUNTIME_PROMPT = load_prompt_template("base_runtime.md")


def _browser_tool_policy(tool_names: set[str]) -> str:
    has_browser_use = "browser_snapshot" in tool_names
    has_chrome_snapshot = "mcp__Chrome-dev-tools__take_snapshot" in tool_names
    has_chrome_eval = "mcp__Chrome-dev-tools__evaluate_script" in tool_names
    if not has_browser_use and not has_chrome_snapshot:
        return ""

    lines = [
        "## Browser Tool Policy",
    ]
    if has_browser_use:
        lines.extend(
            [
                "- For built-in browser automation, start with `browser_open` or `browser_snapshot`.",
                "- Use the loop `observe -> decide -> act -> verify`: inspect the page, choose one reversible next action, execute it, then verify before continuing.",
                "- When resuming browser work, call `browser_tabs` or `browser_snapshot` first and switch to a relevant existing tab before opening or navigating a URL.",
                "- If a prior browser turn stopped with `stalled_repeat_detected`, treat it as a planning checkpoint, not a lost browser session. Resume with `browser_tabs` or `browser_snapshot`; do not call `browser_session(action=\"use_system\")`, reset, or reopen unless status proves the session is gone.",
                "- Use `browser_snapshot` to discover the current page and stable element `ref` values before `browser_click` or `browser_type`.",
                "- Prefer `ref` from `browser_snapshot` over raw selectors when possible.",
                "- Before clicking or typing, identify the target from `ref` plus label, role, placeholder, name, nearby text, or `field_candidates`; do not act from position alone unless no DOM target is available.",
                "- If `browser_snapshot` lacks enough information, call `browser_snapshot(include_screenshot=true, limit=120)`, then use `browser_evaluate` for focused DOM inspection before mutating the page.",
                "- For forms, match fields using `target_hint`, `field_candidates`, labels, role, placeholder, name, and current value. For email compose flows, confirm recipient, subject, and message body refs separately before typing.",
                "- In email compose UIs, typed recipients may remain in an autocomplete editor. Commit the recipient with Enter or by clicking the exact suggestion, then verify a recipient chip/token exists before moving to subject/body.",
                "- After typing into any browser form field, inspect the returned `target_after` metadata or take a fresh snapshot before typing into the next field.",
                "- Do not repeat the same browser action with the same arguments after an unchanged result. Inspect status/tabs/snapshot, use screenshot evidence, or report the blocker.",
                "- The browser `mode` is configured by the user in Settings. Always call tools with `mode=\"auto\"` (the default) so the user's preferred mode (managed vs system) is honoured; do not pass `mode=\"managed\"` or `mode=\"system\"` explicitly unless the user requests a one-off override.",
                "- Use `browser_screenshot` or `browser_snapshot(include_screenshot=true)` to evaluate visual state when the structured snapshot is incomplete, stale, or visually ambiguous.",
                "- Use `browser_full_page_screenshot` when the user asks for full-page webpage evidence. It can apply stealth-oriented browser settings, remove common fixed banners/popups, unlock scrolling, and report archive/proxy fallback URLs when a public page remains obscured or blocked.",
                "- If a browser tool returns `status: error`, do not retry the same call first. Call `browser_session(action=\"doctor\")` to inspect `last_error`, `cdp_endpoint_alive`, and `recent_launches`.",
                "- For `reason_code: managed_cdp_timeout` or `managed_chrome_exited`, call `browser_session(action=\"reset\")` once, then retry the original intent. Do not loop more than once.",
                "- For `reason_code: chrome_executable_missing` or `system_launch_disabled`, stop and report the blocker because user action is needed.",
                "- For `reason_code: tool_timeout`, do not retry identically. Narrow the action, for example with a smaller `limit` on `browser_snapshot`, or call `doctor` first.",
                "- The first `browser_open` of a session can take up to 60 seconds on a cold Windows machine; treat first-launch latency as expected.",
            ]
        )
    if has_chrome_snapshot:
        lines.extend(
            [
                "- For MCP-backed Chrome inspection, use `mcp__Chrome-dev-tools__take_snapshot` as the default snapshot tool.",
                "- Do not use generic screenshot-style tools to read webpage content when `mcp__Chrome-dev-tools__take_snapshot` is available.",
            ]
        )
    if has_chrome_eval:
        lines.append(
            "- Use `mcp__Chrome-dev-tools__evaluate_script` only when you specifically need DOM or HTML details that the accessibility snapshot does not provide."
        )
    return "\n".join(lines)


def _computer_use_tool_policy(tool_names: set[str]) -> str:
    has_desktop = bool(
        {
            "screen_info",
            "inspect_ui",
            "click_ui_element",
            "precision_click",
            "screenshot",
            "click",
            "scroll",
        }
        & tool_names
    )
    if not has_desktop:
        return ""

    lines = [
        "## Computer Use Policy",
        "- For Windows computer-use tasks, prefer `inspect_ui` and `click_ui_element` when accessible element data is available.",
        "- Use `screen_info` or the `screenshot` region metadata before screenshot-based coordinate clicks; account for virtual-screen origin and multi-monitor offsets.",
        "- Use `precision_click` for screenshot, window, monitor, or normalized coordinates instead of manually converting pixels to raw `click` coordinates.",
        "- For scrollable panes, pass `x` and `y` to `scroll` so the wheel targets the intended list or panel.",
        "- After any computer-use click, type, or scroll, verify the new state with `inspect_ui`, `screenshot`, or `list_windows` before continuing.",
    ]
    return "\n".join(lines)


def build_skill_prompt_sections(skills: list[SkillSpec], tool_names: set[str] | None = None) -> dict[str, str]:
    checklist_lines = [
        f"- `{skill.name}`: {skill.description or 'No description provided.'}"
        for skill in skills
    ] or ["- No optional skills enabled."]

    skill_sections = []
    for skill in skills:
        body = skill.body.strip()
        if body:
            skill_sections.append(f"### {skill.name}\n{body}")

    return {
        "runtime_rules": BASE_RUNTIME_PROMPT.strip(),
        "capability_checklist": "## Capability Checklist\n" + "\n".join(checklist_lines),
        "browser_policy": _browser_tool_policy(tool_names or set()),
        "computer_use_policy": _computer_use_tool_policy(tool_names or set()),
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
