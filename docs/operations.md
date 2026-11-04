# Operations And Security

Monaw is a local desktop agent. Its backend API is a privileged local control
plane that can start agent runs, read local files allowed by policy, resolve
approvals, inspect diagnostics, and update settings. Treat access to the API as
access to the local agent.

## Local Control Plane

The desktop app starts the backend on loopback and authenticates `/api` requests
with short-lived bearer sessions minted by Electron. `/health` is the only
unauthenticated runtime endpoint.

Do not expose the backend through:

- port forwarding;
- reverse proxies;
- public or private tunnels;
- permissive firewall rules;
- non-loopback binds such as `0.0.0.0`.

Development browser sessions must opt in explicitly with
`MONAW_ALLOW_DEVELOPMENT_TOKEN=1` and a local `VITE_MONAW_CONTROL_TOKEN`. Do not
use that mode on shared machines.

## Startup Requirements

Startup must fail closed when security configuration is missing. The backend
requires `MONAW_CONTROL_SECRET` to be at least 32 characters, validates the
runtime directory, and rejects symlinked runtime roots. Electron generates the
control secret for normal desktop launches and passes it only through the
backend process environment.

The Windows launcher and Electron both default to `127.0.0.1`. A non-loopback
backend host is rejected unless `MONAW_ALLOW_UNSAFE_BACKEND_HOST=1` is set for a
deliberate local development experiment.

Launcher logs are stored under:

```text
%USERPROFILE%\.monaw\runtime\launcher\
```

Backend logs are stored under:

```text
%USERPROFILE%\.monaw\runtime\backend.log
```

Logs must not print control-plane secrets, provider keys, Telegram bot tokens,
or full environment dumps.

## Source Restrictions

Desktop sessions are interactive and can resolve approvals in the app. Telegram
and scheduled tasks are non-local entry points and run with source-specific
principals and restricted saved permission profiles.

Telegram:

- require a narrow user/chat allowlist;
- keep `telegram_allow_all` disabled except for private testing;
- keep risky action approvals enabled;
- avoid sending secrets through Telegram messages.

Scheduled tasks:

- store an owner principal and permission profile snapshot;
- use restricted scheduled-task permissions by default;
- recover expired leases on restart;
- should not receive broad persistent grants unless reviewed locally.

## Credentials

Provider API keys and the Telegram bot token are stored by Electron using
OS-backed `safeStorage`. Renderer code can set, delete, apply, and view boolean
status for credentials, but cannot read saved plaintext values.

On upgrade, legacy plaintext credential fields are migrated into the encrypted
store and verified before plaintext values are removed. If OS encryption is
unavailable, credential operations fail closed.

## Incident Response

If a secret or grant may be compromised:

1. Revoke provider API keys in the provider dashboard.
2. Rotate the Telegram bot token with BotFather.
3. Restart Monaw so a fresh control-plane secret is generated.
4. Delete persistent access grants or reset relevant permission settings.
5. Reject or clear pending approvals.
6. Use Settings -> Observability -> Delete Data to clear retained runtime
   diagnostics, logs, observability artifacts, memory files, approval history,
   attachment registry entries, browser/MCP diagnostics, and scheduled-task
   output.
7. Review `%USERPROFILE%\.monaw\runtime\backend.log` for local errors without
   sharing raw logs publicly. To share diagnostics, prepare a report in
   Settings -> Observability -> Support report. It is redacted and bounded, and
   it is shown in full before it can be saved.

## Backup And Restore

Close Monaw before making a full backup. Copy the Monaw home directory:

```text
%USERPROFILE%\.monaw\
```

Important data:

```text
runtime\agent.db          conversations, scheduled tasks, and app state
runtime\settings.json     local settings
runtime\file_mutation\snapshots\  private before-images for supported file Undo/recovery
memory\                   long-term memory markdown files
workspace\                default agent workspace
skills\                   skills you created with the skill creator
browser\                  browser screenshots
```

To restore, close Monaw, replace the target `.monaw` folder with the backup,
then start Monaw again. For partial restore, prefer copying `memory\`,
`workspace\`, and selected settings while leaving current credentials managed by
the local OS-backed store.

Windows file snapshots use DPAPI for the current Windows user. A full backup
does not make them portable to another Windows account or machine. Treat them
as private file contents when storing or sharing a backup.

## Run Recovery And File Snapshots

The database migration is forward-only. An existing schema-17 database keeps
its conversations, message history, workspace assignments, checkpoints, and
scheduled tasks as schema versions 18–22 are added. The migration does not
create durable Runs for historical conversations, so old chats have no Run
recovery timeline. Runs created by the new version are available to their
signed desktop profile only; the current profile and workspace are checked
again before reading recovery details, continuing, verifying, or undoing work.

After an unexpected exit, Monaw first checks that the previous Run owner is no
longer active. It then reads the journal and verifies supported file effects
without replaying tool calls. Continue is offered only when the checkpoint and
current scope are safe to restore. Shell, Browser, MCP, and other opaque effects
may remain `needs_review`; inspect their external state before starting another
action that could repeat them. A file operation can be verified only while its
private evidence is valid and the current file still matches the expected
identity and fingerprint.

Undo is limited to verified native operations on ordinary files: creating or
overwriting a file, deleting a file, or moving a file on the same volume with
`fs_move`. It is not a general file restore. Copy, the separate `fs_rename`
tool, folders and folder trees, links, cross-volume moves, shell commands,
browser actions, MCP calls, and unsupported metadata changes do not have Undo
through the current API. Undo checks the current read/write/delete permissions
and the file's current identity, contents, and parent scope. If the file was
edited, replaced, moved, or its permissions changed, Monaw leaves it untouched
and reports a conflict or unavailable Undo. Restored bytes and supported
modified time do not guarantee the original file identity, creation time, or
all metadata.

The current permission decision must allow the inverse directly, without a
confirmation or access-grant prompt. Undo does not start a second approval
request and does not reuse `once` or `session` grants from the original file
change. In Default mode, where MUTATE or DELETE requires confirmation, Undo is
unavailable even if the forward change was approved. A policy decision that
allows the inverse without confirmation or a grant is required. On platforms
other than Windows, Undo that would delete a newly created file is unavailable;
the filesystem does not provide the conditional delete-by-identity operation
Monaw requires for that case.

Manual preview and Apply currently handle one ordinary file write in a live
desktop Run. Preview state is kept in backend memory for 15 minutes and is lost
on restart. Durable operation evidence and eligible Undo snapshots are kept
separately in the database and snapshot store.

The private snapshot store is:

```text
%USERPROFILE%\.monaw\runtime\file_mutation\snapshots\
```

The current limits are 4 MiB of before-image data per file, 8 MiB total
before-image data per operation, and 512 MiB for the snapshot store. A snapshot
expires for Undo after 30 days. Cleanup preserves snapshots referenced by active
or unresolved Runs; old unreferenced files are removed after the 30-day orphan
grace period. A missing, expired, unreadable, or quota-rejected snapshot does
not permit best-effort Undo. File changes are still subject to path policy and
approval before they run.

Run state, operation evidence, and events are stored in `runtime\agent.db`.
Events currently have no automatic age-based cleanup. The `410 events_expired`
response indicates a sequence gap in the rows that remain, not a configured
retention period. Deleting a conversation cascades its Run, operation, and
event rows from the database. Snapshot files are separate: they are not removed
immediately when a conversation is deleted and remain until the orphan cleanup
can safely reclaim files at least 30 days old.

`Settings -> Observability -> Delete Data` clears diagnostics, logs,
observability artifacts, memory files, approval/access-grant history,
attachment registry entries, browser/MCP diagnostics, and scheduled-task
outputs. It does not delete conversation messages, Run/operation/event history,
workspace files, saved settings or credentials, or file-mutation snapshots.
Deleting the `.monaw` folder manually also removes the default workspace and
snapshots; workspace folders outside `.monaw` are not deleted by that action.

## Retention And Full Local Deletion

Monaw enforces runtime retention on startup for observability data, debug
bundles, backend logs, approval/access-grant history, attachment registry data,
browser/MCP diagnostics, scheduled-task output, and old unreferenced file
snapshots. Run events and active/recoverable snapshots do not have an age-based
cleanup policy.

For app-managed deletion of retained diagnostics and support data, use:

```text
Settings -> Observability -> Delete Data
```

For manual deletion, close Monaw and remove:

```text
%USERPROFILE%\.monaw\
```

Manual deletion removes runtime data, memory, browser screenshots, the default
workspace files, and local settings. Workspaces outside `.monaw` remain on
their own paths. Your Chrome profile lives in Chrome's own folder and is not
affected. Revoke external provider keys and Telegram tokens separately because
local deletion cannot revoke third-party credentials.

## Generated API Reference

The endpoint table in [api.md](api.md) is generated from FastAPI route
definitions:

```powershell
uv run --project backend python scripts/generate-api-docs.py
uv run --project backend python scripts/generate-api-docs.py --check
```
