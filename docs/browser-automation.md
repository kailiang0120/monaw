# Browser Automation

Monaw uses the `browser-use` skill for browser automation. It can open pages, inspect DOM state, click, type, switch tabs, take screenshots, extract text, and run focused JavaScript.

## Where To Configure

Open Settings -> Browser.

Browser settings are stored in runtime settings. Runtime browser folders are under:

```text
%USERPROFILE%\.monaw\browser\
```

Default runtime folders:

```text
browser\profiles\managed\
browser\downloads\
browser\screenshots\
browser\traces\
browser\chrome.log
```

## Browser Modes

| Mode | Meaning |
| --- | --- |
| `auto` | Use saved settings and fallback behavior. Default. |
| `managed` | Launch an isolated Chrome profile controlled by Monaw. |
| `system` | Attach to an already-running Chrome DevTools endpoint. |

Managed mode is safest for automation because it uses an isolated profile and dedicated runtime folder.

System mode does not launch your normal Chrome profile. It only attaches to an existing DevTools endpoint, normally `http://127.0.0.1:9222`.

## Important Settings

| Setting | Meaning |
| --- | --- |
| `mode` | `auto`, `managed`, or `system`. |
| `enable_system_fallback` | Allows fallback to system connection when configured. |
| `headless` | Runs managed Chrome without a visible window. |
| `keep_alive` | Keeps the browser session alive between actions. |
| `system_connection_strategy` | `auto`, `attach`, or `launch`. Current system mode is designed to attach, not manage the user's real Chrome profile. |
| `system_cdp_url` | Chrome DevTools endpoint, default `http://127.0.0.1:9222`. |
| `managed_profile_dir` | Isolated Chrome profile directory. |
| `downloads_dir` | Managed browser downloads directory. |
| `screenshots_dir` | Screenshot output directory. |
| `traces_dir` | Trace output directory. |
| `system_profile_directory` | Optional Chrome profile directory name for system-profile workflows. |
| `allowed_domains` | Optional domain allow list. Empty means no domain restriction. |

## Starting Chrome For System Mode

Use managed mode unless you specifically need a real Chrome profile.

For Chrome DevTools workflows, start Chrome manually with remote debugging and set the system CDP URL in Settings -> Browser:

```text
http://127.0.0.1:9222
```

Modern Chrome may block remote debugging on the default user-data-dir. If system mode cannot attach, use managed mode or start Chrome manually with a non-default `--user-data-dir`.

## Main Browser Tools

| Tool | Purpose |
| --- | --- |
| `browser_session` | Inspect, doctor, reset, stop, list profiles, or one-off switch modes. |
| `browser_open` | Start or reuse a browser session and optionally open a URL. |
| `browser_navigate` | Navigate the current tab. |
| `browser_tabs` | List, create, switch, or close tabs. |
| `browser_snapshot` | Capture DOM state and stable refs for visible interactive elements. |
| `browser_click` | Click by ref, selector, text, or coordinates. |
| `browser_type` | Fill text into an input or editable element. |
| `browser_press` | Send a key press. |
| `browser_select_option` | Select values in a `select` element. |
| `browser_scroll` | Scroll by pixel deltas. |
| `browser_wait` | Wait by time, selector, visible text, or URL substring. |
| `browser_find` | Find visible elements by text, label, or role. |
| `browser_extract_text` | Extract visible page text. |
| `browser_fill_form` | Fill multiple fields and optionally submit. |
| `browser_downloads` | List or clear managed downloads. |
| `browser_console` | Read browser console diagnostics when available. |
| `browser_network_summary` | Summarize recent resource timing entries. |
| `browser_screenshot` | Capture current tab or element screenshot. |
| `browser_full_page_screenshot` | Capture full-page screenshots with optional declutter and fallback suggestions. |
| `browser_evaluate` | Run focused JavaScript in the active tab. |

## Recommended Automation Flow

Use this flow for reliable browser work:

```text
open -> snapshot -> decide -> act -> verify
```

Practical rules:

| Rule | Reason |
| --- | --- |
| Start with `browser_open`. | Creates or reuses the session. |
| Use `browser_snapshot` before mutating. | Gets stable refs and visible state. |
| Prefer refs over selectors. | Refs come from observed state and reduce selector guessing. |
| Verify after every click or type. | Pages can rerender, reject input, or open new tabs. |
| Do not repeat the same failed action. | Inspect, take a screenshot, or run doctor first. |
| Use `browser_evaluate` only when snapshot is insufficient. | Keeps automation safer and more explainable. |

## Screenshots And Downloads

Screenshots are saved under the configured screenshots directory. Managed downloads are saved under the configured downloads directory.

`browser_full_page_screenshot` can optionally remove common fixed banners and popups. It can also return public archive/proxy fallback URLs when public content remains blocked, but third-party fallbacks should only be used when sending the URL to that service is acceptable.

## Diagnostics

Use:

```text
browser_session(action="doctor")
```

Doctor output can show:

| Field | Meaning |
| --- | --- |
| `last_error` | Last browser manager error. |
| `cdp_endpoint_alive` | Whether the configured CDP endpoint is reachable. |
| `recent_launches` | Recent launch attempts and reason codes. |
| `current_mode` | Current active mode. |
| `current_cdp_url` | Active CDP URL. |
| `chrome_executable` | Chrome executable used by managed mode. |

## Common Reason Codes

| Reason Code | Meaning | Action |
| --- | --- | --- |
| `managed_cdp_timeout` | Managed Chrome did not expose CDP in time. | Reset once, then check `chrome.log`. |
| `managed_chrome_exited` | Managed Chrome exited during launch. | Check `chrome.log` and profile lock state. |
| `chrome_executable_missing` | Chrome executable could not be found. | Install Chrome or set a valid path if supported by settings. |
| `system_launch_disabled` | System mode will not launch the user's real Chrome profile. | Start Chrome with remote debugging or use managed mode. |
| `tool_timeout` | A browser tool timed out. | Narrow the operation or inspect with doctor/screenshot. |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Browser does not open. | Confirm `browser-use` is installed and check `browser\chrome.log` under the active runtime folder. |
| System mode cannot attach. | Open `http://127.0.0.1:9222/json/version` in a browser or PowerShell to confirm CDP is live. |
| Actions hit the wrong field. | Use `browser_snapshot(include_screenshot=true)` and identify the target by ref, label, role, placeholder, and nearby text. |
| Tab state is stale. | Run `browser_tabs(action="list")` and switch to the expected tab. |
| Downloads are missing. | Check the configured `downloads_dir`, not the normal Windows Downloads folder. |
| Repeated action fails. | Stop repeating it; inspect snapshot, screenshot, console, or network summary. |

## Safety Notes

Browser automation can click, type, submit forms, and interact with authenticated sessions. For sensitive pages, prefer explicit user confirmation before submitting or sending data.

Use managed mode for untrusted automation. Use system mode only when the task needs an existing logged-in browser session.
