# Windows computer use

Discover the app and exact window, capture `computer_functions_get_window_state`, and pass its `state_id` to `computer_functions_act`. Use `include_text=true` for UIA element indexes. Coordinate and UIA input require a snapshot; app launches and keyboard shortcuts can target a resolved window without one.

Snapshots expire after 120 seconds and belong to the execution principal, chat, control session and turn. A batch consumes the snapshot and invalidates all snapshots of that window. Before input, Monaw checks the live HWND, PID, process creation time, title, physical bounds and DPI, plus any captured accessibility tree and a small grayscale image sample. These image checks detect material changes and are a heuristic, not a semantic guarantee. Pointer input also checks focus and the actual window under the pointer. After earlier input changes the UI, later coordinate actions require another observation.

Stop cooperatively interrupts action boundaries, character/modifier loops, drag steps, verification polling and launch waits. Held keys and mouse buttons are released in cleanup. Approved replays use the same tool executor, cancellation scope and desktop lock as initial calls. Native UIA provider calls already in progress cannot be forcibly interrupted. Completed input is not undone; the desktop remains locked while a worker stops.

Each batch result includes per-action results, `completed_actions`, a `verified` flag, and `after_state` when a follow-up capture succeeds. A click or shortcut returning `ok` confirms input delivery. Text setters and selections check the actual resulting value. Unreadable values produce `verification_unavailable`; mismatches produce `verification_failed`. Uncertain invocation/setter failures never automatically repeat input. Partial failures carry `retryable=false` and an instruction to inspect before continuing.

For other outcomes, attach an explicit check to an action:

```json
{
  "state_id": "cfs_…",
  "actions": [{
    "type": "invoke",
    "element_index": 7,
    "expect": {"type": "file_exists", "path": "exports/report.txt"}
  }]
}
```

Supported checks are `file_exists` with `path`, `window_title` with exact `equals`, and `element_value`/`selected_option` with `element_index` and `equals`. Relative file paths resolve in the chat workspace and are pinned before approval; read permission is checked before input and during verification. File existence alone does not prove file contents or that an existing file was saved again. Checks poll for up to one second; a slow application may need a fresh observation afterward.

Typing and UIA setters share the 4,000-character limit and credential checks. Native password flags and control labels are inspected before text entry. Keyboard layout support is checked for the whole string before any characters are sent, preventing duplicated prefixes when Unicode needs clipboard paste. Paste restores previous text clipboard content unless the user copied something new meanwhile; sensitive or non-text clipboard data is left intact.

Regression tests use isolated desktop and UIA adapters. They cover ownership, window reuse/movement/scaling, changed accessibility/pixels, partial cancellation, held-input cleanup, Unicode fallback, clipboard races, revoked permissions, verified values/selections/files and approved replay locking. Real display scaling, multiple physical monitors and third-party UIA providers still require Windows acceptance testing.
