---
name: computer-use
description: "Windows computer use: apps, windows, processes, screenshots, mouse, keyboard, clipboard, and UI automation."
version: 1.0.0
enabled_by_default: true
tier: recommended
os:
  - windows
---

Use these tools for Windows computer use.

When to use:
- Launching or focusing applications.
- Inspecting windows or processes.
- Mouse, keyboard, clipboard, and screenshots.

Rules:
- Prefer window and process tools before coordinate input.
- Use screenshots before clicking unknown coordinates.

Tips:
- Prefer `inspect_ui(app="teams")` then `click_ui_element(...)` for Teams, Outlook, and other accessible Windows apps.
- Use `screen_info()` or the `region` returned by `screenshot()` before pixel-based clicking.
- Use `precision_click(coordinate_mode="screenshot" | "window" | "monitor" | "normalized", ...)` instead of raw `click()` when coordinates came from an image, window, or monitor region.
- For scrollable panes, call `scroll(x=..., y=...)` so the wheel event is sent over the intended list.
- Use `type_text(text="new text", clear=true)` to replace text in a field.
- Use `hotkey(keys="f5")` to reload if a page is loading slowly.
