---
name: browser-use
description: Managed browser automation with snapshot, tabs, navigation, clicking, typing, screenshots, and system-profile fallback.
display_name: Browser automation
summary: Browse, inspect, and interact with websites using managed or system Chrome.
version: 1.0.0
enabled_by_default: true
tier: recommended
metadata:
  openclaw:
    requires:
      python_module:
        - browser_use
---

Use these tools for website fetching and browser automation tasks.

Preferred workflow:
- For read-only URL retrieval, extraction, or summarization, use `browser_fetch` before opening a browser tab. It uses local HTTP plus Scrapling parsing first, with optional rendering through Monaw's browser session.
- Start with `browser_open` to create or reuse the browser session.
- Always use `observe -> decide -> act -> verify`: inspect state, choose one safe next action, execute it, then verify before continuing.
- When resuming browser work, call `browser_tabs` or `browser_snapshot` first and switch to a relevant existing tab before opening or navigating a URL.
- If the previous turn stopped with `stalled_repeat_detected`, treat that as a planning checkpoint, not a lost browser session. Resume with `browser_tabs` or `browser_snapshot`; do not use `browser_session(action="use_system")`, reset, or reopen unless status proves the session is gone.
- Use `browser_snapshot` before interacting so you get stable `ref` values for visible elements.
- Prefer `ref` from `browser_snapshot` over raw CSS selectors when possible.
- `browser_snapshot` may use enhanced DOM inspection. Prefer enhanced refs the same way as legacy refs; they can include accessibility role/name, state, bounds, iframe/shadow context, and control metadata.
- If a browser action reports a stale ref or `needs_snapshot`, take a fresh `browser_snapshot` before acting again.
- Use `browser_get_element(ref=...)` when one observed ref needs closer inspection before clicking or typing.
- Before clicking or typing, identify the target by `ref` plus labels, role, placeholder, name, `field_candidates`, and nearby context. Do not act from position alone unless no DOM target is available.
- If `browser_snapshot` lacks enough information, call `browser_snapshot(include_screenshot=true, limit=120)`, then use `browser_evaluate` for focused DOM inspection before mutating.
- For forms, identify the field by `target_hint`, `field_candidates`, labels, role, placeholder, name, and nearby context before typing. Treat recipient, subject, and message body fields as separate targets and verify each one after filling.
- In email compose UIs, a typed recipient can remain inside an autocomplete editor. Commit it with Enter or by clicking the exact suggestion, then verify a recipient chip/token exists before filling subject/body.
- Use `browser_click`, `browser_type`, `browser_press`, `browser_select_option`, and `browser_wait` to complete the flow.
- Use `browser_screenshot` or `browser_snapshot(include_screenshot=true)` for evidence or visual confirmation when the structured snapshot is incomplete or ambiguous.
- Use `browser_full_page_screenshot` when full-page webpage evidence is needed. It applies stealth-oriented browser settings, removes common fixed/sticky banners and popups, unlocks scrolling, and can return archive/proxy fallback URLs when public content remains obscured.
- Use `browser_fetch(mode="dynamic")` when a read-only page needs JavaScript rendering. Use `browser_fetch(mode="stealth")` only when the user explicitly asks for a stealth/protected-page fetch or a normal/dynamic fetch reports a blocker.
- Do not repeat the same browser action with the same arguments after an unchanged result. Inspect status/tabs/snapshot, use screenshot evidence, or report the blocker.
- If a browser tool returns `status: error`, do not retry the same call first. Call `browser_session(action="doctor")` to inspect `last_error`, `cdp_endpoint_alive`, and `recent_launches`.
- For `reason_code: managed_cdp_timeout` or `managed_chrome_exited`, call `browser_session(action="reset")` once, then retry the original intent. Do not loop more than once.
- For `reason_code: chrome_executable_missing` or `system_launch_disabled`, stop and report the blocker because user action is needed.
- For `reason_code: tool_timeout`, do not retry identically. Narrow the action, for example with a smaller `limit` on `browser_snapshot`, or call `doctor` first.
- The first `browser_open` of a session can take up to 60 seconds on a cold Windows machine; treat first-launch latency as expected.

Session behavior:
- The `mode` for browser tools is driven by user Settings. Always call tools with the default `mode="auto"` so the user's preferred mode is used; do NOT pass `mode="managed"` or `mode="system"` unless the user explicitly requests an override for one call.
- Managed mode launches an isolated profile Chrome with a dedicated CDP port and no user data.
- System mode attaches only to an already-running Chrome DevTools endpoint. It does not close or relaunch the user's real Chrome profile.
- `browser_session(action="use_system")` and `browser_session(action="use_managed")` are one-off switches; they do not change the saved user preference.
- Inspect Chrome profiles with `browser_session(action="list_profiles")` and switch with `profile_directory`.

Rules:
- Do not use desktop-control tools for websites when browser-use tools can do the job directly.
- Do not open a browser tab just to read a public URL if `browser_fetch` can retrieve the needed text or selector content.
- Do not guess selectors if `browser_snapshot` can give you a `ref`.
- Use `browser_evaluate` only when the structured snapshot is not enough.
- If multiple visible fields could match, do not type yet. Take another snapshot, use `browser_evaluate` to inspect labels/attributes, or click only after the correct field is clear.

## Browser Tool Policy
- For built-in browser automation, start with `browser_open` or `browser_snapshot`.
- Use `observe -> decide -> act -> verify`: inspect the page, choose one reversible next action, execute it, then verify before continuing.
- Before clicking or typing, identify the target from `ref` plus label, role, placeholder, name, nearby text, or `field_candidates`; do not act from position alone unless no DOM target is available.
- For forms, match fields using `target_hint`, `field_candidates`, labels, role, placeholder, name, and current value. After typing into any field, inspect returned `target_after` metadata or take a fresh snapshot before continuing.
- In email compose UIs, confirm recipient, subject, and message body refs separately before typing. Commit autocomplete recipients with Enter or the exact suggestion, then verify a recipient chip/token before filling subject or body.
- The browser `mode` is configured by the user in Settings. Call tools with `mode="auto"` by default so the saved preference is honored; only pass `managed` or `system` for a requested one-off override.
- If a browser tool returns `status: error`, call `browser_session(action="doctor")` before retrying. For `managed_cdp_timeout` or `managed_chrome_exited`, call `browser_session(action="reset")` once, then retry the intent once. Stop for `chrome_executable_missing` or `system_launch_disabled`; user action is required.
- For `tool_timeout`, narrow the action rather than retrying identically. The first `browser_open` of a session can take up to 60 seconds on a cold Windows machine.
- When the Chrome DevTools MCP tools are available, use `mcp__Chrome-dev-tools__take_snapshot` for webpage inspection.
- Use `mcp__Chrome-dev-tools__evaluate_script` only for DOM details the accessibility snapshot cannot provide.
- Do not use generic screenshot-style tools to read webpage content when `mcp__Chrome-dev-tools__take_snapshot` is available.
