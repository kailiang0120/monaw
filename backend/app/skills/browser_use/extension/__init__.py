"""Browser automation through the Monaw Chrome extension.

The extension (``browser-extension/`` in the repository) relays Chrome DevTools
Protocol commands through ``chrome.debugger``. This package runs the local MCP
server that talks to it. The server keeps the name and tool names of the
built-in ``chrome-devtools`` server, so permissions, URL checks, the stall
guard and the UI treat both connection methods the same way.
"""
