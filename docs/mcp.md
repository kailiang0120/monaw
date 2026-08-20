# MCP

Monaw includes an MCP bridge that connects configured MCP servers and exposes their tools to the agent runtime.

## What MCP Adds

MCP lets Monaw use external tools from local or remote servers. Typical examples are filesystem servers, Chrome DevTools servers, database tools, or custom internal tools.

The bridge has two parts:

| Part | Purpose |
| --- | --- |
| MCP control tools | Built-in status, reconnect, refresh, and list commands. |
| Reflected remote tools | Tools discovered from configured MCP servers and exposed as model-safe tool names. |

## Where To Configure

Open Settings -> MCP.

MCP server configuration is saved in runtime settings under `%USERPROFILE%\.monaw\runtime\settings.json`.

## Supported Transports

| Transport | Use For | Required Fields |
| --- | --- | --- |
| `stdio` | Local MCP commands started by Monaw. | `name`, `command`, optional `args`, `env`, `cwd`. |
| `streamable_http` | HTTP MCP endpoints. | `name`, `url`, optional `headers`. |

Server names must match:

```text
^[a-zA-Z0-9_-]{1,32}$
```

Server names must be unique.

## Server Fields

| Field | Meaning |
| --- | --- |
| `name` | Stable local server id. Used in reflected tool names. |
| `enabled` | Whether Monaw should start/connect this server. |
| `transport` | `stdio` or `streamable_http`. |
| `command` | Local executable for `stdio`. |
| `args` | Command arguments for `stdio`. |
| `env` | Extra environment variables for `stdio`. |
| `cwd` | Working directory for `stdio`. |
| `url` | MCP HTTP URL for `streamable_http`. |
| `headers` | Static headers for `streamable_http`. |
| `startup_timeout_ms` | Startup/connect timeout. Default is `8000`. |
| `call_timeout_ms` | Per-tool call timeout. Default is `30000`. |
| `reconnect_on_unhealthy` | Restart unhealthy servers before tool calls. |
| `allow_list` | Optional list of remote tool names to expose. Empty means expose all tools. |
| `trusted_tools` | Explicit remote tool names allowed without the reflected-tool approval prompt. Use sparingly. |
| `tool_risk_overrides` | Per-tool `low`, `medium`, or `high` risk label overrides. Overrides do not by themselves bypass approval. |
| `description` | Human-readable purpose. |

## Example Configs

Example `stdio` server:

```json
{
  "name": "filesystem",
  "enabled": true,
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\KL\\Documents"],
  "env": {},
  "cwd": "",
  "startup_timeout_ms": 8000,
  "call_timeout_ms": 30000,
  "reconnect_on_unhealthy": true,
  "allow_list": [],
  "trusted_tools": ["list_directory"],
  "tool_risk_overrides": {},
  "description": "Filesystem MCP server"
}
```

Example `streamable_http` server:

```json
{
  "name": "internal_tools",
  "enabled": true,
  "transport": "streamable_http",
  "url": "http://127.0.0.1:9000/mcp",
  "headers": {
    "Authorization": "Bearer local-token"
  },
  "startup_timeout_ms": 8000,
  "call_timeout_ms": 30000,
  "reconnect_on_unhealthy": true,
  "allow_list": [],
  "description": "Local internal tools"
}
```

Prefer the Settings UI over manual JSON edits.

## Runtime Lifecycle

When MCP is enabled, Monaw starts enabled servers and lists their tools. For `stdio`, Monaw starts the configured command. For `streamable_http`, Monaw connects to the configured URL.

Runtime states:

| State | Meaning |
| --- | --- |
| `stopped` | Server is not running or not connected. |
| `starting` | Monaw is opening transport and initializing. |
| `connected` | Server is initialized and tools were listed. |
| `unhealthy` | Server was connected but a call, refresh, or stop failed. |
| `failed` | Startup or initialization failed. |

Diagnostics in the authenticated local UI include the configured command/arguments or URL, working directory, resolved executable, PID when available, startup phase, stderr tail, tool counts, reflected names, last error, unhealthy reason, and recent call duration. Environment-variable and HTTP-header values remain redacted and are never returned as diagnostic secrets.

Enabled servers are started during backend startup. A periodic MCP `list_tools` liveness probe detects a dead child or transport and changes the server to `unhealthy`; the UI receives an `mcp.changed` event and refreshes the card. Reconnect is explicit unless a subsequent tool call uses the configured recovery behavior.

## Reflected Tool Names

Remote MCP tool names are converted to safe local names:

```text
mcp__<server>__<tool>
```

Names are sanitized and shortened deterministically when needed. Use `mcp_list_tools` to map a reflected tool name back to its original server and tool.

## Built-In MCP Tools

| Tool | Purpose |
| --- | --- |
| `mcp_status` | Shows configured and active server status. |
| `mcp_list_tools` | Lists reflected tools and original mappings. |
| `mcp_refresh_tools` | Re-lists tools from one or all enabled servers. Requires approval. |
| `mcp_reconnect_server` | Restarts one configured server and refreshes tools. Requires approval. |

Reflected tools require approval by default, including read-like tools. A tool can bypass that prompt only when its original remote tool name is explicitly listed in `trusted_tools`; that explicit exception also applies to a tool the user knowingly trusts despite its risk. Risk overrides alone never bypass approval.

## Approval Behavior

MCP tool annotations are treated as hints, not proof.

Monaw labels a reflected tool as low risk when it has a read-like name such as `get`, `list`, `read`, `search`, `query`, `find`, `inspect`, `status`, or `describe`, or when MCP annotations indicate read-only.

Monaw requires approval for tools that appear destructive, mutating, open-world, or unknown-risk. Keywords such as `delete`, `remove`, `edit`, `patch`, `upload`, `send`, `execute`, `run`, `browser`, `web`, `url`, `request`, and `email` raise the risk.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No MCP tools appear. | Check Settings -> MCP enabled state, server enabled state, and `mcp_status`. |
| `mcp package is not installed`. | Run `uv sync --locked` from the `backend` directory. |
| Server startup times out. | Increase `startup_timeout_ms`, verify `command`, `args`, `cwd`, and `env`. |
| `stdio` command not found. | Use an absolute command path or confirm it is on PATH for the backend Python process. |
| HTTP server fails. | Verify `url`, headers, and that the server supports streamable HTTP MCP. |
| Tool call returns `mcp_timeout`. | Increase `call_timeout_ms`, reconnect the server, or narrow the tool input. |
| Tool list is stale. | Run `mcp_refresh_tools` after changing server capabilities. |
| A server is unhealthy. | Run `mcp_reconnect_server` and check stderr tail in diagnostics. |

## Security Notes

MCP servers can expose powerful tools. Keep the allow list narrow when using servers you do not fully trust.

Do not put permanent secrets in plain `env` fields unless you accept that they are stored in local runtime settings. Prefer short-lived local credentials where possible.

Remote MCP tools can request external network or file actions. Approval prompts are the final safety boundary for mutating and unknown-risk tools.
