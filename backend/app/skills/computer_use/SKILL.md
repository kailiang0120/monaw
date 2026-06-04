---
name: computer-use
description: "Windows computer use through computer_functions tools: app/window discovery, state capture, batched input, clipboard, processes, and diagnostics."
version: 2.0.0
enabled_by_default: true
tier: recommended
os:
  - windows
---

Use these tools for Windows computer-use tasks.

Preferred flow:
- Call `computer_functions_list_apps` to inspect configured/running apps.
- Resolve exactly one target with `computer_functions_get_window`.
- Capture state with `computer_functions_get_window_state`; use `include_text=true` when UIA elements may help.
- Act with one `computer_functions_act` batch, then verify with another `computer_functions_get_window_state`.

Rules:
- Use browser tools for websites. Do not use desktop control for normal browser automation.
- Prefer window-relative coordinates and UIA element indexes from a fresh `state_id`.
- Batch related click/type/key/scroll/drag actions in `computer_functions_act` instead of calling one tool per input.
- Do not interact with terminal apps, Codex, Monaw, password managers, security tools, or system security/privacy dialogs.
- If a window is ambiguous, inspect apps/windows again and choose one exact target before acting.

Action types supported by `computer_functions_act`:
- `launch_app`: launch an app by configured alias.
- `click`: click window-relative coordinates by default.
- `type_text`: type text into the focused target window; use `clear=true` to replace focused text.
- `press_key`: send a key or key chord such as `Return`, `Escape`, `Control_L+a`, or `Alt+f4`.
- `scroll`: scroll from a window-relative point.
- `drag`: drag between window-relative points.
- `set_value`, `select_option`, `invoke`, `secondary_action`: use `element_index` from the latest `state_id`.
