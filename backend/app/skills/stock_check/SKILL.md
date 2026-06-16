---
name: stock-check
description: 'Stock check: when the user asks about a stock or stock-related query,
  use computer-use to navigate the installed Moomoo desktop app to look up the stock
  info.'
version: 1.0.0
enabled_by_default: false
tier: optional
---

# Stock Check via Moomoo Desktop App

This skill activates when the user asks about stock prices, stock information, or stock-related queries.

## Trigger Phrases
- "check stock"
- "stock price"
- "what is the price of"
- "look up [ticker]"
- any question about a specific stock or market data

## Workflow
1. When the user mentions a ticker symbol or stock name, respond that you will check via Moomoo desktop app.
2. Use `computer_functions_list_apps` to check if Moomoo is running.
3. If Moomoo is not running, use `computer_functions_act` with `launch_app` action and app `moomoo` to launch it, then `computer_functions_get_window` with app `moomoo` to find its window.
4. If Moomoo is already running (check by process name `Moomoo.exe` or `moomoo.exe`), use `computer_functions_get_window` with app `moomoo` to resolve its window.
5. Activate the window with `computer_functions_activate_window` using the hwnd or app name.
6. Use `computer_functions_get_window_state` with `include_screenshot=true` to inspect the Moomoo interface.
7. Type the ticker symbol using `computer_functions_act` with a `type_text` action targeting the search bar. Use coordinates relative to the window based on the screenshot analysis.
8. After typing, press Return/Enter using `press_key` action.
9. Use `computer_functions_get_window_state` again to capture the search results or stock page.
10. Read the stock price and relevant data from the window state screenshot and text.

## Important Notes
- Moomoo is already installed and logged in. No login step needed.
- The user's account is already active.
- Always use the window-relative approach through computer-use tools, never via browser.
- If the window is minimized to system tray, use `activate_window` first.
- Close Moomoo after checking if the user doesn't indicate they want to keep it open for more checks.
