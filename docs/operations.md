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
   sharing raw logs publicly.

## Backup And Restore

Close Monaw before making a full backup. Copy the Monaw home directory:

```text
%USERPROFILE%\.monaw\
```

Important data:

```text
runtime\agent.db          conversations, scheduled tasks, and app state
runtime\settings.json     local settings
memory\                   long-term memory markdown files
workspace\                default agent workspace
browser\                  managed browser profile and artifacts
```

To restore, close Monaw, replace the target `.monaw` folder with the backup,
then start Monaw again. For partial restore, prefer copying `memory\`,
`workspace\`, and selected settings while leaving current credentials managed by
the local OS-backed store.

## Retention And Complete Deletion

Monaw enforces runtime retention on startup for observability data, debug
bundles, backend logs, approval/access-grant history, attachment registry data,
browser/MCP diagnostics, and scheduled-task output.

For complete runtime deletion from the app, use:

```text
Settings -> Observability -> Delete Data
```

For manual deletion, close Monaw and remove:

```text
%USERPROFILE%\.monaw\
```

Manual deletion removes runtime data, memory, browser profiles, workspace files,
and local settings. Revoke external provider keys and Telegram tokens separately
because local deletion cannot revoke third-party credentials.

## Generated API Reference

The endpoint table in [api.md](api.md) is generated from FastAPI route
definitions:

```powershell
uv run --project backend python scripts/generate-api-docs.py
uv run --project backend python scripts/generate-api-docs.py --check
```
