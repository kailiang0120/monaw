---
name: computer-use
description: "Windows computer use through computer_functions tools: app/window discovery, state capture, batched input, clipboard, processes, and diagnostics."
display_name: Windows computer use
summary: Inspect and control native Windows applications with focused, verified actions.
version: 2.1.0
enabled_by_default: true
tier: recommended
os:
  - windows
---

Use these tools for Windows computer-use tasks.

## Computer Use Policy

Preferred flow:
- Call `computer_functions_list_apps` to inspect configured/running apps.
- Resolve exactly one target with `computer_functions_get_window`.
- Activate it with `computer_functions_activate_window` before capturing coordinates, so activation changes are already reflected in the observation.
- Capture state with `computer_functions_get_window_state`; use `include_text=true` when UIA elements may help.
- Pass the captured `state_id` to one short `computer_functions_act` batch. Inspect its per-action verification and `after_state`; capture another state if the follow-up observation is unavailable.

Rules:
- Use browser tools for websites. Do not use desktop control for normal browser automation.
- Pointer and UIA actions require a fresh `state_id`. It belongs to this chat, control session and turn, expires after two minutes, and is consumed by one batch. Every snapshot of an interacted window is invalidated, including other chats' snapshots.
- Coordinates use physical pixels, including negative coordinates on secondary monitors. Window-relative coordinates are the default. If the window, scaling or observed UI changes, capture a new state before acting.
- Batch only inputs that remain valid in the same observed UI. A later coordinate action stops if earlier input changes that UI. Refresh after opening menus, dialogs or another work surface.
- `status=ok` can mean input was delivered. Claim an outcome only when `verification.verified=true` or fresh observation proves it. Use an action's `expect` for `window_title` (exact `equals`), `element_value` or `selected_option` (`element_index` and `equals`), or `file_exists` (`path`). File existence does not prove file contents or that an existing file was saved again.
- A partial error, unknown outcome or cancellation requires inspection. Earlier actions may have completed; never replay the batch or repeat input blindly. Stop is checked between actions and within keyboard, drag and wait loops, but cannot undo inputs already delivered or interrupt an in-flight native provider call.
- Text entry is limited to 4,000 characters per action. Both typing and UIA setters inspect the actual control and reject password/credential fields. Use direct user control for those fields.
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
