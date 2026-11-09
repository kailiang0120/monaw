---
name: browser-use
description: Browser automation in the user's own Chrome through the Monaw extension (pages, snapshots, clicking, typing, screenshots), plus read-only page fetching.
display_name: Browser automation
summary: Browse, inspect, and interact with websites using your own Chrome profile.
version: 1.2.0
enabled_by_default: true
always: true
tier: internal
---

Use these tools for website fetching and browser automation tasks.

Tools:
- Interactive browsing uses the browser tools (`mcp__chrome-devtools__*`). They drive the user's own Chrome profile through the Monaw Chrome extension; the first call opens Chrome if needed. Never tell the user to configure debugging ports or launch Chrome manually. If a tool says the extension is not set up or not connected, pass that on to the user instead of retrying.
- For read-only URL retrieval, extraction, or summarization, use `browser_fetch` instead of opening a tab. It is a plain HTTP fetch with no JavaScript. If it returns `status: "needs_browser_render"` (an empty, script-only, or bot-blocked page), open the URL with `mcp__chrome-devtools__new_page` or `navigate_page` and read it with `take_snapshot`.

Workflow (`observe -> decide -> act -> verify`):
1. `mcp__chrome-devtools__list_pages` to see open tabs. Reuse a relevant tab with `select_page`; otherwise `new_page` or `navigate_page` with a full http(s) URL. Pass the `pageId` from `list_pages` to page tools.
2. Use the accessibility snapshot returned by opening or navigating the page to get element `uid`s. Call `mcp__chrome-devtools__take_snapshot` when there is no current snapshot or the page has changed further.
3. If a control is ambiguous or a form fails, use `mcp__chrome-devtools__inspect_element` with its `uid` to read attributes, labels, visibility, value, validation errors and nearby elements. A specific CSS `selector` can inspect elements omitted from the accessibility tree. Use selectors grounded in observed page attributes; multiple matches are an error. This is read-only and does not require custom JavaScript.
4. Act with `click`, `fill`, `fill_form` (preferred for several independent form fields), `type_text`, `press_key`, `hover`, `drag`, or `handle_dialog`. Identify the target from its role, name, label, and nearby text, never from position alone. Actions return an updated snapshot by default; use `includeSnapshot: false` only when that observation is unnecessary. Click and fill automatically scroll and wait for the target to be enabled, unobstructed and stable; filling also waits for read-only fields to unlock. The default readiness budget is 5 seconds (`timeout` can extend it to 30 seconds); `fill_form` shares the budget across its fields. Batch independent fields, but keep dependent fields and submission separate.
5. `fill` and `fill_form` read each field back by themselves: `action_outcome.status` is `verified` when the field holds the value, `failed` with the `observed` value when the page changed or rejected it, and `executed` for password fields, which are never read back. For other actions, pass `expected_effect` when you know the result to look for; otherwise `status` stays `executed`, which only means the input was sent. Verify using the snapshot returned by the action or wait. Do not immediately call `take_snapshot` again when that result already answers the question. Call `take_snapshot` again to verify only when no current snapshot was returned or the page changed further. Existing nodes keep their `uid` across snapshots; navigation or replacement makes them stale.
- Use `wait_for` for a specific expected condition instead of repeated reads or scripts with sleep loops. Use `text: ["expected text"]` for page text, or exactly one `uid`/`selector` plus a `state`: `hidden` for a loading overlay to disappear, `editable` for a field to unlock, `ready` for a visible enabled control with no covering overlay, or `attached`/`detached` for a DOM node to appear/disappear. CSS selectors are rechecked through dynamic re-renders; uids track their original node. These waits return a fresh snapshot by default. A navigation invalidates an element wait: inspect the new page before continuing. Use a bounded `timeout` (milliseconds, maximum 60000), and inspect the reported state if it times out.
- Readiness waits run before the action and never replay a submitted action. If a uid was replaced, use a fresh snapshot to choose its replacement. If a form stops partway through, preserve the completed fields and inspect the current field before proceeding. If an action completed but its snapshot failed, read the current page; do not repeat the action just to obtain a snapshot.
- Use `mcp__chrome-devtools__take_screenshot` only for visual evidence or layout questions; the image is saved to the screenshots folder and shown to you. Do not use generic screenshot-style tools to read webpage content when `mcp__chrome-devtools__take_snapshot` is available.
- Use `mcp__chrome-devtools__evaluate_script` only when the dedicated tools cannot express the task. This is arbitrary code execution, not a DOM observation tool, even if a script appears to only read data. Scripts can read authenticated data, send requests and trigger actions. Supply an explicit `pageId`; never rely on whichever tab is selected when approval completes. Do not write scripts just to read attributes, check validity or wait for overlays. Follow the current permission mode: Default/Custom may require approval, Auto Review reviews the exact action, and Full Access permits execution subject to hard blocks. A script must not bypass a denied action or an always-confirm operation such as file upload.
- For forms, treat each field (for example recipient, subject, and message body) as a separate target and verify each value after filling. In email compose UIs, commit an autocomplete recipient with Enter or the exact suggestion, then verify a recipient chip/token before continuing.
- Console tools (`list_console_messages`, `get_console_message`) are read-only and useful for debugging pages. Network inspection is not available.

Rules:
- Do not repeat the same browser action with the same arguments after an unchanged result. If a turn stopped with `stalled_repeat_detected`, treat it as a checkpoint: run `list_pages` or `take_snapshot`, then change the target or plan.
- Only http and https URLs are allowed. `file:`, `javascript:`, `data:`, and `chrome:` URLs and hosts outside Settings -> Browser allowed domains are blocked with a `reason_code`; report the block instead of working around it.
- `upload_file` always asks the user first. Do not use desktop-control tools for websites the browser tools can handle.
- If a browser tool returns an error, read its message, take a snapshot or list pages, and adjust; do not retry the identical call. For `tool_timeout`, narrow the action rather than retrying identically.
