---
name: mcp-bridge
description: Built-in MCP bridge feature that connects configured servers and exposes local MCP control tools plus reflected remote tools.
enabled_by_default: true
tier: internal
---

This built-in feature provides a local MCP control surface even when configured remote MCP servers are down.

Always start MCP troubleshooting with `mcp_status`. Use `mcp_reconnect_server` when a server is disconnected or unhealthy, and `mcp_refresh_tools` after reconnecting or changing remote server capabilities. Use `mcp_list_tools` to see the reflected model-safe tool names and their original `{server, tool}` mapping.

Reflected remote tool names follow the pattern `mcp__<server>__<tool>`, with sanitization and deterministic shortening when needed. Prefer the structured JSON returned by MCP tools; if a call returns `mcp_timeout`, note the server/tool that timed out and reconnect or refresh before retrying.

Read-only MCP tools can run directly. Destructive, mutating, unknown-risk, or open-world MCP tools require user approval. MCP ToolAnnotations are treated as hints only, not as proof that a tool is safe.

Supported transports:
- `stdio`: local command, args, env, and cwd.
- `streamable_http`: MCP URL plus optional static headers.

Do not assume OAuth, persisted per-tool approval memories, resources, or prompts are available through this bridge.
