"""Stdio MCP server for the Monaw Chrome extension.

Started by the backend as the built-in ``chrome-devtools`` server when the
browser connection is set to the extension::

    python -m app.skills.browser_use.extension

Stdout carries MCP messages, so logging goes to stderr.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import mcp.types as types
import jsonschema
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .bridge import ExtensionBridge, ExtensionError
from .common import configured_port, installed_extension_dir, manifest_version, read_pairing
from .install import ensure_pairing_port, write_connection_status
from .session import ToolError
from .tools_schema import TOOL_DEFINITIONS
from .toolset import BrowserTools, ToolOutput

logger = logging.getLogger("monaw.browser_extension")


def _tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=tool["name"],
            description=tool["description"],
            inputSchema=tool["inputSchema"],
            annotations=types.ToolAnnotations(
                readOnlyHint=bool(tool.get("readOnly")),
                **({"destructiveHint": True, "openWorldHint": True, "idempotentHint": False}
                   if tool["name"] == "evaluate_script" else {}),
            ),
        )
        for tool in TOOL_DEFINITIONS
    ]


def _result(output: ToolOutput) -> types.CallToolResult:
    content: list[Any] = [types.TextContent(type="text", text="\n".join(output.lines) or "Done.")]
    content += [types.ImageContent(type="image", data=image["data"], mimeType=image["mimeType"])
                for image in output.images]
    structured = dict(output.structured_content)
    if output.action_outcome is not None:
        structured["action_outcome"] = output.action_outcome
        import json
        content.append(types.TextContent(type="text", text=json.dumps({"action_outcome":output.action_outcome})))
    return types.CallToolResult(content=content, isError=output.is_error, structuredContent=structured or None)


def _error(message: str, action_id: str = "") -> types.CallToolResult:
    structured = None
    if action_id:
        structured = {"action_outcome":{"status":"failed", "action_id":action_id, "action_executed":False,
            "verification":{"type":"not_dispatched", "passed":False, "reason":"The browser action stopped before input dispatch."}}}
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True, structuredContent=structured)


def build_server(tools: BrowserTools, ensure_listening) -> Server:
    server: Server = Server("monaw-chrome-extension")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return _tools()

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            # Authorization metadata is added by the trusted parent adapter and
            # deliberately absent from the model's public input schema.
            checked_name = str(arguments.get("tool_name") or "") if name == "__monaw_action_context" else name
            checked_args = arguments.get("arguments") or {} if name == "__monaw_action_context" else {key:value for key,value in arguments.items() if key != "__monaw_authorization"}
            definition = next((tool for tool in TOOL_DEFINITIONS if tool["name"] == checked_name), None)
            if definition is None:
                raise ToolError("Unknown browser tool.")
            jsonschema.validate(checked_args, definition["inputSchema"])
            await ensure_listening()
            return _result(await tools.call(name, arguments or {}))
        except jsonschema.ValidationError:
            return _error("Browser arguments do not match the tool schema.")
        except ToolError as exc:
            return _error(str(exc), str((arguments.get("__monaw_authorization") or {}).get("action_id") or ""))
        except ExtensionError as exc:
            return _error(str(exc), str((arguments.get("__monaw_authorization") or {}).get("action_id") or ""))

    return server


async def main() -> None:
    port = configured_port()
    try:
        ensure_pairing_port(port)
    except OSError:
        logger.warning("Could not update the extension's pairing port")
    bridge = ExtensionBridge(
        port,
        token_provider=lambda: (read_pairing() or {}).get("token"),
        expected_version_provider=lambda: manifest_version(installed_extension_dir()),
        on_status=lambda status: write_connection_status(**status),
    )
    lock = asyncio.Lock()

    async def ensure_listening() -> None:
        async with lock:
            if not bridge.listening:
                await bridge.start()

    try:
        await ensure_listening()
    except ExtensionError as exc:
        logger.warning("%s", exc)
    server = build_server(BrowserTools(bridge, require_authorization=True), ensure_listening)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await bridge.close()
        write_connection_status(connected=False, version="", user_agent="", port=port, error="")


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(name)s: %(message)s")
    asyncio.run(main())
