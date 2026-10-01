# Permissions And Approvals

Monaw uses multiple safety layers before local actions run.

## Safety Layers

| Layer | Purpose |
| --- | --- |
| Permission profile | Defines broad behavior for confirmations, paths, apps, deletes, and screen fallback. |
| Access grants | Lets the user allow an unknown app or private path once, for session, or always. |
| Approval tickets | Confirms sensitive actions before they execute. |
| Sandbox | Hardens shell execution after permission and approval checks. |
| Tool-specific policy | Individual tools can impose extra rules. |

These layers are additive. Sandbox mode does not replace approvals. Full access does not make blocked roots or blocked processes safe.

## Four permission modes

Settings and the chat dropdown show only four choices. Short cards have an info button for details; the selected mode has a clear list of behavior. There are no per-action switches or folder/app setup forms.

| Mode | Automatic | Approval behavior |
| --- | --- | --- |
| `default` | Read files, browse, open apps, click and type. | File changes, unknown mutating integration tools, commands and stopping processes ask the user. |
| `full_access` | File changes, browser actions, app control, MCP integrations, commands and stopping processes. | No action approval prompt, except an explicitly configured folder/app confirmation or private-path access grant. |
| `auto_review` | The same routine actions as Default. | A separate tool-free AI call reviews pending changes against the current user request. Clearly authorized actions resume; uncertain, malformed, timed-out or unavailable reviews ask the user. |
| `custom` | Defined in `permissions.custom_profile`. | Defined in the config file. |

Default asks before file deletion, Auto Review reviews it, and Full Access permits it without an action approval prompt. Custom can disable deletion entirely. Blocked processes, protected control files, blocked roots and unattended-run restrictions apply in every mode. Full Access never disables the sandbox or turns a strict container failure into host execution. Browser pages and arbitrary shell commands may have effects that action names cannot establish; the permission system is an approval policy, not semantic verification of every click or command.

MCP adapters resolve the active mode on every call, including after registry construction. Read-only hints and tool-name conventions classify actions; unknown tools require approval in Default and Auto Review. Delete and execution classifications take precedence over a read-only hint. Tool risk labels remain visible even when Full Access permits execution. Refreshing or reconnecting an enabled MCP server is routine app startup.

Auto Review receives the user's current request and the exact pending ticket payload in a fresh call with no tools. It cannot grant private-path/app access or override hard blocks. The ticket hash, context, expiry, pending status and selected mode are validated before automatic approval. Human or AI approval resumes the same stored action; changed permissions and MCP schemas are checked again before execution.

## Custom config

The Custom page shows the actual runtime `settings.json` path. Edit `permissions.custom_profile`, including `confirmations`, `path_rules`, `blocked_roots`, `app_rules`, `allow_delete`, `dangerous_actions_require_confirm` and `allow_screen_fallback`. Save the file, then reload Settings before making UI changes; tool permission checks read saved rules on subsequent calls. Preset switches preserve the saved Custom profile.

For example, within an existing `permissions.custom_profile`, set `confirmations.mutate` to `true` to ask before file changes. Set `dangerous_actions_require_confirm` to `true` to ask before shell commands. To permit deletion, set `allow_delete` to `true`; `confirmations.delete` controls whether it also asks. These are JSON booleans. Keep the surrounding existing config fields.

The legacy name `user_config` maps to `custom`.

## Blocked Roots

Default blocked roots:

```text
C:\Windows
C:\Program Files
C:\Program Files (x86)
C:\ProgramData
C:\$Recycle.Bin
%USERPROFILE%\AppData
```

Most blocked roots are hard blocks. User-private AppData roots can request an access grant for a specific task when appropriate.

Trusted Monaw runtime paths are handled separately so the app can use its own runtime folders.

## Blocked Processes

These sensitive processes cannot be allowlisted:

```text
keepass.exe
1password.exe
lastpass.exe
mmc.exe
secpol.msc
gpedit.msc
regedit.exe
taskmgr.exe
procexp.exe
cmd.exe
powershell.exe
pwsh.exe
windowsterminal.exe
wt.exe
codex.exe
monaw.exe
```

Aliases and executable paths are normalized before checking blocked process names.

## Path Rules

Path rules define what Monaw may do under specific roots.

| Field | Meaning |
| --- | --- |
| `path` | Root path. |
| `read` | Allows reads. |
| `write` | Allows writes and some exec/mutate actions. |
| `delete` | Allows deletes when delete behavior is enabled. |
| `launch` | Allows launch behavior from that root. |
| `require_confirmation` | Forces confirmation even when otherwise allowed. |
| `enabled` | Enables or disables the rule. |

When multiple path rules match, the most specific path wins.

## App Rules

App rules define what Monaw may do with applications.

| Field | Meaning |
| --- | --- |
| `alias` | User-facing app name or alias. |
| `display_name` | Friendly display name. |
| `exe_paths` | Optional executable paths. |
| `launch_allowed` | Allows launching the app. |
| `uia_allowed` | Allows UI automation for the app. |
| `screen_fallback_allowed` | Allows screen-based fallback control for the app. |
| `require_confirmation` | Forces confirmation for matching app actions. |
| `enabled` | Enables or disables the rule. |

Unknown app launches require an access grant in Custom. Presets allow routine app launches while enforcing sensitive-process and configured app blocks.

## Access Grants

Access grants are interactive decisions for unknown apps or private paths.

Decision options:

| Decision | Behavior |
| --- | --- |
| `once` | Allow this one action, then consume the grant. |
| `session` | Allow for the current app session. |
| `always` | Persist the app/path rule to runtime settings. |
| `deny` | Block the action. |

Access grant tickets are transient in memory. Session and once grants are stored in SQLite so the running app can consume them correctly.

Permanent app grants are written into settings as app rules. Permanent path grants add a narrowly scoped `PathRule` for the requested action and preserve all existing rule fields, including read/write/delete permissions and confirmation requirements. An `always` grant for a read action does not silently become a writable or deletable root.

Scheduled, Telegram, and other non-interactive runs cannot display an approval prompt. Their pending approval or access-grant request is automatically denied by the execution gate rather than waiting for the ten-minute interactive timeout.

## Approval Tickets

Approval tickets confirm sensitive actions before they execute.

Approval ticket fields include:

| Field | Meaning |
| --- | --- |
| `id` | Ticket id. |
| `conversation_id` | Conversation that requested the action. |
| `action_type` | Type of action. |
| `tool_name` | Tool requesting approval. |
| `target_path` | Path target, if any. |
| `target_app` | App target, if any. |
| `risk_level` | Low, medium, or high risk. |
| `reason` | Why approval is needed. |
| `action_description` | Human-readable action. |
| `payload_hash` | Hash of tool payload. |
| `status` | Ticket lifecycle status. |

Ticket statuses:

| Status | Meaning |
| --- | --- |
| `pending` | Waiting for user decision. |
| `approved` | User approved; execution may resume. |
| `rejected` | User rejected. |
| `cancelled` | Ticket was cancelled. |
| `applied` | Approved action finished. |
| `failed` | Approved action failed during execution. |
| `superseded` | A newer matching ticket replaced this ticket; the original waiter is woken and no action runs. |
| `expired` | The ticket passed its expiry time. |

Approval tickets are persisted as JSONL so pending and historical approvals survive restarts.

Files:

```text
%USERPROFILE%\.monaw\runtime\approvals\tickets.jsonl
%USERPROFILE%\.monaw\runtime\policy\approval_log.md
```

## Settings And Generated Policy Files

The source of truth is:

```text
%USERPROFILE%\.monaw\runtime\settings.json
```

Compatibility and transparency files are generated under:

```text
%USERPROFILE%\.monaw\runtime\policy\controller_policy.json
%USERPROFILE%\.monaw\runtime\policy\controller_policy.md
%USERPROFILE%\.monaw\runtime\policy\allowlisted_apps.md
```

Do not treat generated markdown files as the authoritative settings store. Edits there can be overwritten by the UI or API.

## API Surface

Approvals:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/approvals/pending` | List pending approvals. |
| `POST /api/approvals/{ticket_id}/approve` | Approve a pending ticket. |
| `POST /api/approvals/{ticket_id}/reject` | Reject a pending ticket. |
| `GET /api/approvals/history` | List resolved tickets. |

Access grants:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/access-grants/pending` | List pending access grants. |
| `POST /api/access-grants/{ticket_id}/resolve` | Resolve a grant with `once`, `session`, `always`, or `deny`. |

Controller policy:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/settings/controller-policy` | Read compatibility policy state. |
| `PUT /api/settings/controller-policy` | Update compatibility policy state. |
| `GET /api/settings/allowlisted-apps` | List allowlisted apps. |
| `POST /api/settings/allowlisted-apps` | Add an allowlisted app. |
| `DELETE /api/settings/allowlisted-apps/{alias}` | Remove an allowlisted app. |
| `GET /api/settings/controller-policy-markdown` | Read generated markdown policy files. |

## Relationship To Sandbox

Permission and approval checks happen before shell execution. The sandbox layer is applied after those gates.

See `docs\sandboxing.md` for sandbox modes and backend behavior.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Action keeps asking for access. | Use `session` or `always` instead of `once`, or confirm the exact app/path matches. |
| Approval does not resume. | Check the ticket is still pending, has not been superseded or expired, and that the desktop app is connected to the same conversation. |
| App cannot be allowlisted. | It may match a blocked process name. |
| Delete is blocked. | `allow_delete` is false or delete confirmation was rejected. |
| Path is blocked even in full access. | Blocked roots still apply. |
| Policy markdown changes disappear. | Edit Settings instead; markdown files are generated. |
