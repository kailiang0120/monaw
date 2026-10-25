"""Open Chrome normally when the extension is not connected yet.

No debugging flags are used: the extension connects on its own once Chrome
starts. Chrome's service worker alarm runs every 30 seconds, so allow for that
when Chrome was already open.
"""

from __future__ import annotations

import asyncio

from ..chrome_launch import (
    chrome_running,
    find_chrome_executable,
    launch_chrome_detached,
    system_chrome_user_data_dir,
)
from .bridge import ExtensionBridge
from .common import read_pairing
from .session import ToolError

CONNECT_WAIT_S = 35.0

NOT_SET_UP = (
    "The Monaw Chrome extension is not set up. Ask the user to open Monaw Settings > Browser "
    "and click Set up extension."
)
NOT_CONNECTED = (
    "Chrome is open but the Monaw extension has not connected. Ask the user to check that the "
    "Monaw extension is turned on at chrome://extensions in the Chrome profile they want Monaw to use."
)


async def ensure_extension_connected(bridge: ExtensionBridge) -> None:
    if bridge.connected:
        return
    if read_pairing() is None:
        raise ToolError(NOT_SET_UP)
    executable = find_chrome_executable()
    user_data_dir = system_chrome_user_data_dir()
    if not executable:
        raise ToolError("Google Chrome was not found. Install Chrome to use browser automation.")
    running = await asyncio.to_thread(chrome_running, user_data_dir) if user_data_dir else False
    if not running:
        # Chrome opens its last-used profile, where the extension normally lives.
        await asyncio.to_thread(launch_chrome_detached, [executable])
    if not await bridge.wait_connected(CONNECT_WAIT_S):
        raise ToolError(NOT_CONNECTED)
