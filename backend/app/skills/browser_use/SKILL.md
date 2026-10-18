---
name: browser-use
description: Browser automation in the user's own Chrome through Chrome DevTools tools (pages, snapshots, clicking, typing, screenshots), plus read-only page fetching.
display_name: Browser automation
summary: Browse, inspect, and interact with websites using your own Chrome profile.
version: 1.0.0
enabled_by_default: true
always: true
tier: internal
---

Use these tools for website fetching and browser automation tasks.

Tools:
- Interactive browsing uses the Chrome DevTools tools (`mcp__chrome-devtools__*`). They drive the user's own Chrome profile; the first call launches it if needed, and on Windows Monaw clicks Chrome's Allow prompt unless disabled in Settings. Never tell the user to configure debugging ports or launch Chrome manually.
- For read-only URL retrieval, extraction, or summarization, use `browser_fetch` instead of opening a tab. It is a plain HTTP fetch with no JavaScript. If it returns `status: "needs_browser_render"` (an empty, script-only, or bot-blocked page), open the URL with `mcp__chrome-devtools__new_page` or `navigate_page` and read it with `take_snapshot`.

Workflow (`observe -> decide -> act -> verify`):
1. `mcp__chrome-devtools__list_pages` to see open tabs. Reuse a relevant tab with `select_page`; otherwise `new_page` or `navigate_page` with a full http(s) URL. Pass the `pageId` from `list_pages` to page tools.
2. `mcp__chrome-devtools__take_snapshot` to get the page's accessibility tree with element `uid`s.
3. Act on one `uid` at a time with `click`, `fill`, `fill_form` (preferred for several form fields), `type_text`, `press_key`, `hover`, `drag`, or `handle_dialog`. Identify the target from its role, name, label, and nearby text in the snapshot, never from position alone.
4. `take_snapshot` again to verify the result before the next action. `uid`s change after navigation or re-render; if a call reports a stale or missing `uid`, take a fresh snapshot.
- Use `wait_for` when the page needs time to show expected text.
- Use `mcp__chrome-devtools__take_screenshot` only for visual evidence or layout questions; the image is saved to the screenshots folder and shown to you. Do not use generic screenshot-style tools to read webpage content when `mcp__chrome-devtools__take_snapshot` is available.
- Use `mcp__chrome-devtools__evaluate_script` only for DOM details the accessibility snapshot cannot provide. It runs page JavaScript and asks the user for approval outside Full access.
- For forms, treat each field (for example recipient, subject, and message body) as a separate target and verify each value after filling. In email compose UIs, commit an autocomplete recipient with Enter or the exact suggestion, then verify a recipient chip/token before continuing.
- Console and network tools (`list_console_messages`, `list_network_requests`, and their `get_*` forms) are read-only and useful for debugging pages.

Rules:
- Do not repeat the same browser action with the same arguments after an unchanged result. If a turn stopped with `stalled_repeat_detected`, treat it as a checkpoint: run `list_pages` or `take_snapshot`, then change the target or plan.
- Only http and https URLs are allowed. `file:`, `javascript:`, `data:`, and `chrome:` URLs and hosts outside Settings -> Browser allowed domains are blocked with a `reason_code`; report the block instead of working around it.
- `upload_file` always asks the user first. Do not use desktop-control tools for websites the browser tools can handle.
- If a DevTools tool returns an error, read its message, take a snapshot or list pages, and adjust; do not retry the identical call. For `tool_timeout`, narrow the action rather than retrying identically.
