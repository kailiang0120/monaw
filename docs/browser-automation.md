# Browser Automation

Monaw drives your own Chrome, with your saved logins, bookmarks, and tabs, through the Chrome DevTools MCP server ([`chrome-devtools-mcp`](https://github.com/ChromeDevTools/chrome-devtools-mcp)). The `browser-use` skill bundles two things:

| Part | What it does |
| --- | --- |
| Chrome DevTools tools (`mcp__chrome-devtools__*`) | Open and switch tabs, read pages as accessibility snapshots, click, fill forms, type, press keys, handle dialogs, upload files, take screenshots, and read console and network activity. |
| `browser_fetch` | Read-only HTTP fetch of a known URL, parsed with Scrapling. It never opens a browser and does not run JavaScript. |

There is no separate isolated browser profile and no other browser engine.

## How It Connects To Your Chrome

The DevTools server is built in (`backend/app/skills/mcp_bridge/builtin.py`). It starts with the backend as `npx -y chrome-devtools-mcp@1.11.0 --autoConnect`, with usage statistics, update checks, file-URL navigation, and the memory, performance, and extension tool categories turned off. It is not listed under Settings -> Tools -> MCP and it runs even when your own MCP connections are switched off. Older saved Chrome DevTools MCP entries are removed from settings automatically so only one server runs.

Chrome is not touched until the first browser tool call. Then Monaw:

1. Finds Chrome. On Windows it checks the standard install folders (`Program Files`, `Program Files (x86)`, `%LOCALAPPDATA%\Google\Chrome\Application`) and then the `App Paths\chrome.exe` registry key. On other systems it checks `/Applications` (macOS) or `google-chrome` / `chromium` on `PATH`.
2. Picks the profile: the one chosen in Settings -> Browser, otherwise Chrome's last-used profile from `Local State`.
3. Prepares the profile once. While Chrome is closed, it sets Chrome's supported remote-debugging preference in `Local State`, preserving every other setting. If Chrome is already running at that moment, Monaw asks you to close it once and retry (`chrome_setup_requires_restart`), rather than overwriting live preferences.
4. Reuses a running Chrome when its `DevToolsActivePort` endpoint is alive. Otherwise it launches the profile with `--enable-features=DevToolsAcceptDebuggingConnections` in a new window, detached from Monaw so Chrome survives Monaw exiting, and waits up to 20 seconds for the endpoint.
5. Lets `chrome-devtools-mcp --autoConnect` attach to that endpoint.

Chrome 136+ refuses `--remote-debugging-port` on the default user-data folder, so Monaw relies on Chrome 144+'s user-approved connection instead. Monaw never closes your Chrome; disconnecting or reconnecting leaves it and its tabs open.

### The Allow Prompt

Chrome 144 and later shows **Allow remote debugging?** when a tool connects. Chrome has no remembered approval, so it can ask again after a reconnection.

With **Auto-approve Chrome's connection prompt** on (the default, Windows only), Monaw watches for that native dialog only while a DevTools call is in progress. It first checks that the local debugging port belongs to the installed Chrome executable, ignores any prompt that was already open before the call, refuses to act when several prompts are open, and never clicks anything inside a web page. It then focuses the dialog and clicks **Allow**. If accessibility is unavailable or the prompt is translated, you click **Allow** yourself. Turn the setting off to always approve manually.

## Permissions

Each DevTools tool has an explicit entry in Monaw's permission table (`BUILTIN_TOOL_POLICIES` in `builtin.py`) instead of a guess from its name:

| Tools | Treated as | Asks first? |
| --- | --- | --- |
| `list_pages`, `select_page`, `take_snapshot`, `take_screenshot`, `wait_for`, console and network readers, `get_css_styles` | Read | No |
| `click`, `click_at`, `hover`, `drag`, `handle_dialog` | Click | Follows your permission mode (no prompt in the default interactive mode) |
| `fill`, `fill_form`, `type_text`, `press_key` | Type | Follows your permission mode |
| `navigate_page`, `new_page`, `close_page`, `resize_page`, `emulate` | Navigation | Follows your permission mode |
| `evaluate_script` | Run code (high risk) | Yes, outside Full access |
| `upload_file` | File change (high risk) | Always |
| `lighthouse_audit`, screencast tools | File change | Follows your permission mode |

The **Navigation target domains** setting (`allowed_domains`) is a navigation-target policy. Before an explicit URL in `navigate_page` or `new_page` reaches Chrome, Monaw checks that submitted URL:

- Only `http` and `https` are allowed. `file:`, `javascript:`, `data:`, `blob:`, `about:`, `chrome:`, `chrome-extension:`, `devtools:`, and `view-source:` URLs are blocked with a `reason_code`.
- Submitted hosts outside **Navigation target domains** are blocked.
- Scheduled and other non-interactive runs need an allowed-domains list and may only open public hosts.
- `navigate_page` with `initScript` is refused; page JavaScript goes through `evaluate_script`, which asks first.

This is not browser-origin containment. It does not inspect the current origin of existing tabs or restrict snapshots, screenshots, page selection, back/forward/reload without a URL, click-driven navigation, popups, or Chrome redirects. An allowed URL may lead Chrome to another origin. The separate interaction and unattended-run permission rules still apply, but do not turn the domain list into an all-origin boundary. Use a Chrome profile whose accessible tabs and accounts you intend the agent to use.

The shell sandbox does not contain your personal Chrome, desktop automation, or external MCP servers. Each uses its own permissions and environment. Controlled adapter tests cover which submitted targets are intercepted and show that returned redirect/popup destinations are not checked; these are not live-Chrome containment tests.

Mutating browser tools are guarded against loops: repeating the same action with unchanged page text inserts a recovery checkpoint (`stalled_repeat_detected`) that tells the model to take a snapshot or list pages before trying again.

## Screenshots

Images returned by DevTools tools (for example `take_screenshot`) are saved as `mcp_screenshot_*` files in the screenshots folder and shown to the model for that step only. They are not attached to the final answer unless you ask for the image. The default folder is:

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
| Status | Whether Chrome was found and the state of the DevTools connection. **Reconnect** restarts the built-in DevTools server; Chrome stays open. |
| Chrome profile (`system_profile_directory`) | The profile to open. Empty uses Chrome's last-used profile. |
| Auto-approve Chrome's connection prompt (`auto_approve_local_debugging`) | Windows only. Default on. |
| Navigation target domains (`allowed_domains`) | Explicit DevTools navigation targets and `browser_fetch` URLs. Fetch also checks redirect hops; Chrome redirects and implicit navigation are outside this guard. Empty allows any target in interactive chats; unattended navigation/fetch requires a list. |
| Screenshots folder (`screenshots_dir`) | Where browser screenshots are saved. |

Settings files from earlier versions may still contain options of the removed Python browser engine (`headless`, `keep_alive`, `dom_inspection_engine`, iframe options, `system_connection_strategy`, `system_cdp_url`, `downloads_dir`, `traces_dir`). They are ignored when the file loads and dropped on the next save.

The same status is available from `GET /api/diagnostics/browser-use`: `chrome_found`, the Chrome profiles, the selected and last-used profile, and a `devtools` object with the built-in server's `running`, `connected`, `state`, `tool_count`, and `last_error`. `POST /api/diagnostics/browser-use/reset` reconnects the server. Paths are not returned.

## Common Reason Codes

| Reason code | Meaning | Action |
| --- | --- | --- |
| `chrome_executable_missing` | Chrome was not found. | Install Google Chrome 144 or later. |
| `chrome_profile_missing` | The chosen profile does not exist. | Pick a profile in Settings -> Browser. |
| `chrome_setup_requires_restart` | First-time setup needs Chrome closed. | Close Chrome once, then retry. |
| `chrome_profile_setup_failed` | `Local State` could not be updated. | Check access to Chrome's profile folder. |
| `system_debugging_not_enabled` | Chrome opened but its approved endpoint did not appear. | Use Chrome 144+, close Chrome once, then retry. |
| `domain_not_allowed`, `url_scheme_not_allowed`, `blocked_host` | The target URL policy refused the URL. | Adjust **Navigation target domains** or use a public http(s) URL. |
| `needs_browser_render` (status) | `browser_fetch` could not read a JavaScript page. | Open it with `new_page` and `take_snapshot`. |
| `tool_timeout` | A browser call took too long. | Narrow the action instead of repeating it. |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Status shows Chrome **Not found**. | Install Google Chrome. Monaw does not use other Chromium browsers on Windows. |
| Chrome opens but nothing happens. | Look for the **Allow remote debugging?** prompt in Chrome and click **Allow**. |
| Every call asks for Allow again. | Expected after reconnections; keep auto-approve on (Windows). |
| First run says to close Chrome. | Close all Chrome windows once so Monaw can prepare the profile, then retry. |
| DevTools connection shows an error. | Click **Reconnect**. If it persists, check that Node.js and `npx` work, since the server runs through `npx`. |
| Wrong profile opens. | Choose the profile in Settings -> Browser, then **Reconnect**. A Chrome that is already running keeps its own profiles open. |
| A page fetch returns `needs_browser_render`. | Expected for JavaScript-heavy pages; the model should switch to the DevTools tools. |

## Safety Notes

Browser automation acts in your logged-in Chrome profile: it can click, type, submit forms, and read authenticated pages. Use **Allowed domains** to limit which sites it may open, and keep sensitive actions behind your permission mode's approval prompts.
