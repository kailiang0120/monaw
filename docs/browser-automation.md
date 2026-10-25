# Browser Automation

Monaw drives your own Chrome, with your saved logins, bookmarks, and tabs, through the **Monaw Chrome extension** (`browser-extension/`). The extension relays Chrome DevTools commands through Chrome's `chrome.debugger` API. There is no permission prompt and no change to Chrome's own settings; while Monaw works, Chrome shows "Monaw started debugging this browser".

The `browser-use` skill bundles two things:

| Part | What it does |
| --- | --- |
| Browser tools (`mcp__chrome-devtools__*`) | Open and switch tabs, read pages as accessibility snapshots, click, fill forms, type, press keys, handle dialogs, upload files, take screenshots, and read console messages. |
| `browser_fetch` | Read-only HTTP fetch of a known URL, parsed with Scrapling. It never opens a browser and does not run JavaScript. |

There is no separate isolated browser profile and no other browser engine.

## Setting Up the Extension

Chrome 137 and later ignore the `--load-extension` flag, so the extension is loaded once by hand:

1. In Settings -> Browser, click **Set up extension**. Monaw copies the extension to `%USERPROFILE%\.monaw\browser\extension`, pairs it, opens that folder, and tries to open `chrome://extensions`.
2. In Chrome, turn on **Developer mode** (top right of `chrome://extensions`).
3. Click **Load unpacked** and select the folder. Load it in the Chrome profile you want Monaw to use.

The status changes to **Connected** within a few seconds. When a newer extension ships with Monaw, the button reads **Update extension**; updating copies the new files into the same folder and the extension reloads itself. Chrome 125 or later is required.

## How It Connects

The built-in `chrome-devtools` server runs as `python -m app.skills.browser_use.extension` from the backend folder (`backend/app/skills/browser_use/extension/`, started by `backend/app/skills/mcp_bridge/builtin.py`). It serves the browser tools over MCP and listens on `ws://127.0.0.1:8436/extension` (`browser.extension_port`) for the extension. It is not listed under Settings -> Tools -> MCP and runs even when your own MCP connections are switched off. The server keeps the `chrome-devtools` name and tool names so permissions, the repeat guard, and the chat UI recognise its tools.

- Only loopback connections are accepted, and the `Origin` header must be the extension's fixed origin (`chrome-extension://nklbigohmenhbcfdincebbokjpeaaihc`). Web pages cannot connect, because browsers always send their real origin.
- The extension then sends the pairing token from `pairing.json` in its folder. The token is created on setup and kept across updates.
- One extension connection is served at a time; a second Chrome profile is refused.

The extension holds no policy of its own. It forwards DevTools commands to tabs; lists, opens, focuses, and closes tabs; and reports page events. Tabs that Monaw opens join a **Monaw** tab group. It asks for the `debugger`, `tabs`, `tabGroups`, `storage`, and `alarms` permissions, with no host permissions or content scripts.

If the extension is not connected when a browser tool runs, Monaw opens Chrome normally (its last-used profile, with no debugging flags, detached so Chrome outlives Monaw) and waits up to 35 seconds for the extension. Clicking **Cancel** on Chrome's debugging banner detaches Monaw from that tab until the next browser tool call. Monaw never closes your Chrome.

## Tools

| Tool | Notes |
| --- | --- |
| `list_pages`, `select_page`, `new_page`, `navigate_page`, `close_page` | Pages have small numeric ids that stay stable while the tab is open. `navigate_page` takes `type` `url`, `back`, `forward`, or `reload`. The last open page cannot be closed. |
| `take_snapshot` | The main frame's accessibility tree as lines such as `uid=1_5 button "Submit"`. A node keeps its `uid` until the page navigates. Content inside iframes is not listed. |
| `click`, `hover`, `drag` | Real mouse events at the element's centre. When a control is visually hidden behind its label (custom radios and checkboxes, such as colour swatches), Monaw clicks the label; with no visible label, `click` calls the element's own `click()` and says so. |
| `fill`, `fill_form`, `type_text`, `press_key` | `fill` types into text fields, picks a `<select>` option by text or value, and takes `"true"` / `"false"` for checkboxes, radios, and switches. |
| `handle_dialog` | Accepts or dismisses an open `alert` / `confirm` / `prompt`. While a dialog is open, other page tools report it instead of hanging. |
| `take_screenshot` | Brings the tab to the front, then captures the viewport, the full page, or one element (`uid`). |
| `wait_for`, `evaluate_script`, `upload_file` | `upload_file` takes `filePaths`, a list of absolute local paths. |
| `list_console_messages`, `get_console_message` | Console messages and uncaught errors, kept for the last three navigations. |

Snapshots and screenshots are returned in the chat and never written to arbitrary paths. Chrome blocks extensions from `chrome://` pages and the Chrome Web Store.

## Permissions

Each browser tool has an explicit entry in Monaw's permission table (`BUILTIN_TOOL_POLICIES` in `builtin.py`) instead of a guess from its name:

| Tools | Treated as | Asks first? |
| --- | --- | --- |
| `list_pages`, `select_page`, `take_snapshot`, `take_screenshot`, `wait_for`, console readers | Read | No |
| `click`, `hover`, `drag`, `handle_dialog` | Click | Follows your permission mode (no prompt in the default interactive mode) |
| `fill`, `fill_form`, `type_text`, `press_key` | Type | Follows your permission mode |
| `navigate_page`, `new_page`, `close_page` | Navigation | Follows your permission mode |
| `evaluate_script` | Run code (high risk) | Yes, outside Full access |
| `upload_file` | File change (high risk) | Always |

The **Navigation target domains** setting (`allowed_domains`) is a navigation-target policy. Before an explicit URL in `navigate_page` or `new_page` reaches Chrome, Monaw checks that submitted URL:

- Only `http` and `https` are allowed. `file:`, `javascript:`, `data:`, `blob:`, `about:`, `chrome:`, `chrome-extension:`, `devtools:`, and `view-source:` URLs are blocked with a `reason_code`.
- Submitted hosts outside **Navigation target domains** are blocked.
- Scheduled and other non-interactive runs need an allowed-domains list and may only open public hosts.
- `navigate_page` with `initScript` is refused; page JavaScript goes through `evaluate_script`, which asks first.

This is not browser-origin containment. It does not inspect the current origin of existing tabs or restrict snapshots, screenshots, page selection, back/forward/reload without a URL, click-driven navigation, popups, or Chrome redirects. An allowed URL may lead Chrome to another origin. The separate interaction and unattended-run permission rules still apply, but do not turn the domain list into an all-origin boundary. Use a Chrome profile whose accessible tabs and accounts you intend the agent to use.

The shell sandbox does not contain your personal Chrome, desktop automation, or external MCP servers. Each uses its own permissions and environment. Controlled adapter tests cover which submitted targets are intercepted and show that returned redirect/popup destinations are not checked; these are not live-Chrome containment tests.

Mutating browser tools are guarded against loops: repeating the same action with unchanged page text inserts a recovery checkpoint (`stalled_repeat_detected`) that tells the model to take a snapshot or list pages before trying again.

## Screenshots

Images returned by browser tools (for example `take_screenshot`) are saved as `mcp_screenshot_*` files in the screenshots folder and shown to the model for that step only. They are not attached to the final answer unless you ask for the image. The default folder is:

```text
%USERPROFILE%\.monaw\browser\screenshots\
```

The screenshots folder stays readable and writable for the agent even though the rest of Monaw's runtime folder is protected.

## browser_fetch

Use `browser_fetch` when you give Monaw a URL and only need its text, links, metadata, HTML, or a CSS-selector extract. It returns JSON with `status`, `url`, `final_url`, `fetcher`, `http_status`, `title`, `content`, `links`, `metadata`, and `truncated`.

| Argument | Meaning |
| --- | --- |
| `mode` | `auto` (default) flags pages that look empty, script-only, or bot-blocked; `http` returns the raw HTTP result as is. |
| `selector` | CSS selector to extract instead of the whole page. |
| `output` | `text`, `markdown`, `html`, `links`, or `metadata`. |
| `max_chars` | Content limit (500 to 200,000). |
| `timeout_ms` | Request timeout (1 to 120 seconds). |

When a page needs JavaScript, `browser_fetch` returns `status: "needs_browser_render"` with whatever it fetched and a `hint` to open the URL with `mcp__chrome-devtools__new_page` (or `navigate_page`) and read it with `take_snapshot`. The old `dynamic` and `stealth` modes now return that same hint without fetching.

URL safety:

- Only `http` and `https` URLs, and only hosts in **Navigation target domains** when that list is set.
- Hosts that resolve to private, loopback, link-local, reserved, or multicast addresses are refused (`blocked_host`).
- Every redirect hop is checked against the same rules before it is followed (`redirect_domain_not_allowed`, `blocked_redirect_host`), and the final URL is checked again.
- Non-interactive runs need an allowed-domains list.

`browser_fetch` holds no browser state, so it can run in parallel with other read-only tools.

## Settings

Open Settings -> Browser.

| Setting | Meaning |
| --- | --- |
| Status | Whether Chrome was found and whether the extension is connected. **Reconnect** restarts the built-in browser server and the extension reconnects; Chrome stays open. |
| Monaw extension | **Set up extension**, **Update extension**, or **Set up again**, with the folder to load and the setup steps. |
| Extension port (`extension_port`) | Local port the extension connects to. Default 8436; settings file only. |
| Navigation target domains (`allowed_domains`) | Explicit navigation targets and `browser_fetch` URLs. Fetch also checks redirect hops; Chrome redirects and implicit navigation are outside this guard. Empty allows any target in interactive chats; unattended navigation/fetch requires a list. |
| Screenshots folder (`screenshots_dir`) | Where browser screenshots are saved. |

Settings files from earlier versions may still contain removed browser options: those of the old Python browser engine (`headless`, `keep_alive`, `dom_inspection_engine`, iframe options, `system_connection_strategy`, `system_cdp_url`, `downloads_dir`, `traces_dir`) and of the former Chrome remote-debugging connection (`connection`, `system_profile_directory`, `auto_approve_local_debugging`). They are ignored when the file loads and dropped on the next save. A saved MCP server entry for `chrome-devtools-mcp --autoConnect` from older versions is removed the same way.

The same status is available from `GET /api/diagnostics/browser-use`: `chrome_found`, an `extension` object (`installed`, `paired`, `installed_version`, `bundled_version`, `update_available`, `connected`, `connected_version`, `last_error`), and a `devtools` object with the built-in server's `running`, `connected`, `state`, `tool_count`, and `last_error`. `POST /api/diagnostics/browser-use/reset` restarts the server. Paths are not returned, except by `POST /api/diagnostics/browser-use/extension/install`, which sets up or updates the extension and returns the folder to load in Chrome.

## Common Errors

| Reason code or message | Meaning | Action |
| --- | --- | --- |
| "The Monaw Chrome extension is not set up" | Setup never ran. | Click **Set up extension** in Settings -> Browser. |
| "the Monaw extension has not connected" | Chrome is open but the extension did not connect within 35 seconds. | Check the extension is on at `chrome://extensions` in the profile you use. |
| "Google Chrome was not found" | Chrome is not installed. | Install Google Chrome 125 or later. |
| "Port 8436 is already in use" | Another program holds the extension port. | Change `browser.extension_port` in the settings file, then click **Set up again**. |
| `domain_not_allowed`, `url_scheme_not_allowed`, `blocked_host` | The target URL policy refused the URL. | Adjust **Navigation target domains** or use a public http(s) URL. |
| `needs_browser_render` (status) | `browser_fetch` could not read a JavaScript page. | Open it with `new_page` and `take_snapshot`. |
| `tool_timeout` | A browser call took too long. | Narrow the action instead of repeating it. |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Status shows Chrome **Not found**. | Install Google Chrome. Monaw does not use other Chromium browsers on Windows. |
| Extension stays **Waiting for Chrome**. | Load the folder with **Load unpacked** and check the extension is on. Its toolbar popup shows the connection state and has a **Reconnect** button. |
| Extension popup says the pairing was rejected. | Click **Set up again** in Settings -> Browser; the extension picks up the new pairing on its next attempt. |
| Status shows **Needs attention**. | Click **Reconnect**. If it persists, check `backend.log` in Monaw's runtime folder for the browser server's error. |
| Monaw uses the wrong Chrome profile. | Load the extension only in the profile you want Monaw to use; it controls the profile it is loaded in. |
| A page fetch returns `needs_browser_render`. | Expected for JavaScript-heavy pages; the model should switch to the browser tools. |

## Safety Notes

Browser automation acts in your logged-in Chrome profile: it can click, type, submit forms, and read authenticated pages. Use **Navigation target domains** to limit which sites it may open, and keep sensitive actions behind your permission mode's approval prompts.
