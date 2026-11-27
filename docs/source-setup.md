# Windows source setup and startup

Use this guide when you cloned Monaw and want to run the code in that checkout. Commands below use PowerShell from the repository root unless a different folder is shown. Paths containing spaces are supported.

For the downloadable Windows app, use the [installation guide](installation.md). The installer includes its own runtime and does not use `setup.bat` or `start.bat`.

Jump to: [requirements](#requirements) · [first setup](#first-time-setup) · [daily use and updates](#daily-use-and-code-changes) · [manual setup](#manual-dependency-setup) · [paths and overrides](#paths-and-environment-overrides) · [troubleshooting](#troubleshooting) · [non-interactive use](#non-interactive-use-and-verification).

## Choose the right workflow

| You want to… | Use |
| --- | --- |
| Use a released app without development tools | The Windows installer, then the Start-menu shortcut. |
| Prepare a fresh source checkout | `setup.bat`, then `start.bat`. |
| Open an already prepared source checkout | `start.bat`. |
| Run newer source code after pulling changes | Close Monaw, update the checkout, run `setup.bat`, then `start.bat`. |
| Work on the renderer or backend separately | The [development commands](development.md#development-commands). |
| Make a distributable Windows installer | The [packaging guide](packaging.md). |

`setup.bat` installs dependencies; it does not download newer Monaw source code. `start.bat` starts the checkout; it does not update the checkout or install frontend dependencies.

## Requirements

| Requirement | How Monaw uses it | How it is obtained |
| --- | --- | --- |
| Windows 10 or 11, x64 | Desktop shell, Windows automation, credential encryption | Required before setup. |
| Git | Clone and update the repository | Install [Git for Windows](https://git-scm.com/downloads/win) before cloning. |
| Windows PowerShell 5.1 | Run the batch-file launchers | Included with supported Windows versions. PowerShell 7 is optional. |
| uv | Create and sync the backend environment | Setup checks for it and can install `astral-sh.uv` using winget. |
| Python 3.11 | Run the backend | Pinned in `backend/.python-version`; uv can download it when needed. A separate system Python installation is unnecessary. |
| Supported Node.js and npm | Install frontend dependencies and run Vite/Electron tooling | Setup checks them and can install `OpenJS.NodeJS.LTS` using winget. |
| Internet connection | Download tools, Python, dependencies, and Electron; call model providers | Required for a fresh setup and online model requests. |
| Model connection | Generate replies | Configure a supported connection inside Monaw after launch. |

The accepted Node.js ranges come from `frontend/package.json`: **20.19+ within 20.x**, **22.13+ within 22.x**, or **24.x**. Other major versions and prereleases are rejected. Node 22.13.0 is the frontend CI baseline; a newer major being called LTS does not automatically make it compatible with this checkout. Setup rechecks the executable that actually resolves on `PATH` after any installation.

winget is needed only when setup must install missing prerequisites. If it is unavailable, install uv and a supported Node.js version manually, reopen the terminal, then rerun setup. uv's [installation guide](https://docs.astral.sh/uv/getting-started/installation/) and Microsoft's [winget install reference](https://learn.microsoft.com/windows/package-manager/winget/install) describe the supported installation methods. Node installers are available from the [official Node.js download page](https://nodejs.org/en/download).

Run setup as your normal Windows user. Installing a prerequisite may request elevation depending on its installer and your system policy. Monaw itself does not require an elevated terminal for ordinary source startup.

Chrome and Docker are optional: Chrome is used for [browser automation](browser-automation.md), and Docker can provide [shell isolation](sandboxing.md). Neither is required just to open the chat interface.

## First-time setup

### 1. Clone the repository

Choose a local development folder and run:

```powershell
git clone https://github.com/kailiang0120/monaw.git
Set-Location .\monaw
```

Confirm that this folder contains `setup.bat`, `start.bat`, `backend`, and `frontend`:

```powershell
Get-Item .\setup.bat, .\start.bat, .\backend, .\frontend
```

The scripts resolve the checkout from their own location, so double-clicking them in Explorer also works. Commands such as `npm ci` and `uv sync`, when run manually, still need the correct project directory.

### 2. Run setup

```powershell
.\setup.bat
```

Finish or stop active Monaw tasks first. Setup closes Monaw-related processes whose command lines belong to this checkout before replacing dependencies. It first asks Electron windows to close, then terminates remaining matching process trees if necessary. It excludes the setup process and its ancestors. This can interrupt work in that checkout; it is not a way to pause a task.

Setup performs these stages in order:

1. Opens a fresh setup log and reads the configured backend port, including `MONAW_BACKEND_PORT` when set.
2. Closes running processes associated with this checkout and waits for Vite and backend ports to become available. It reports a remaining port owner instead of stopping an unrelated program.
3. Finds uv through `AGENT_UV_PATH` or `PATH`. If uv is missing and no explicit override is supplied, it attempts a winget installation.
4. Checks the Node.js engine policy and npm. If necessary, it attempts to install Node.js LTS, refreshes `PATH`, and validates the resolved executables again.
5. Runs `uv --version`, then `uv sync --locked` in `backend`. uv creates or updates `backend/.venv` using the pinned Python version and `backend/uv.lock`, including development dependencies.
6. Runs `npm ci` in `frontend`. npm replaces the local dependency tree using `package-lock.json`. The approved Electron install script downloads the desktop executable.
7. Runs `npm run check:electron` to verify that Electron's executable actually exists. A successful npm dependency install by itself is not enough.
8. Prints the completion message and returns exit code `0`. Any failed command stops setup and returns a nonzero exit code.

Command output is written to the console and setup log as each line arrives. Some download tools render progress differently when output is redirected, so a download can still be quiet for a while. A fresh setup takes longer than a cached run; there is no fixed completion time.

By default, the setup log is:

```text
%USERPROFILE%\.monaw\runtime\setup\setup.log
```

It is replaced at the start of each setup run. Save a copy before retrying if you need the previous failure details. Custom data-folder overrides change this location; see [paths and environment overrides](#paths-and-environment-overrides).

The batch wrapper pauses after success or failure so an Explorer-launched window stays readable. Press a key to close that window or return to the calling terminal. Setup does not launch the app automatically.

### 3. Start Monaw

```powershell
.\start.bat
```

The launcher:

1. Resolves Node.js, npm, and uv; validates the explicit uv override if one is supplied.
2. Checks for the backend Python executable, Vite command, and downloaded Electron executable. Missing dependencies produce a `Run setup.bat` error before background services are spawned.
3. Reads the backend host and effective port. The checked-in configuration is `127.0.0.1:8435`. The port must be an integer from `1024` to `65535`.
4. Verifies the runtime directory is writable and is not itself a symlink or junction. It rejects a non-loopback host unless the explicit unsafe-development override is set.
5. Starts Vite in the background on port `5275`, or reuses a responding server already there. It waits up to 60 seconds for Vite to respond. Electron's development renderer and CORS policy also use `5275`; changing only Vite's port is not supported.
6. Starts Electron in the background. Electron creates the authenticated local session and starts Python through `uv run --locked --no-dev uvicorn ...`. The normal desktop backend is not started with Python's `--reload` flag.
7. Waits up to 90 seconds for the backend's `/health` endpoint, while also watching the launched process. Electron has its own startup checks and can report failure earlier.
8. Prints the resolved log folder and exits successfully when the health check responds. An Explorer-launched console is hidden after success. On failure, the batch wrapper shows the error and pauses.

Launcher success means the local backend answered its readiness check. Loading settings and chat history is a separate UI step. Monaw shows its startup screen while those load; it does not insert an artificial delay before opening the chat.

If startup fails, the launcher attempts to terminate only process trees it started during that attempt. A Vite server it reused is left running.

### 4. Configure a first chat

1. Open **Settings > Connections** and configure an OpenAI API key, a ChatGPT account connection, or a Google API key.
2. In a **new chat**, choose a workspace above the message box and select a permission mode. Once the chat starts, its workspace selector is hidden; create a new chat to choose another workspace.
3. Select a model and reasoning effort in the composer.
4. Send a simple read-only request, for example: `Summarize what this project contains without changing files.`

No `.env` file is required for desktop setup. Saved connections use Electron's encrypted credential store. Voice input needs an OpenAI or Google API key even when chat uses a ChatGPT account.

## Daily use and code changes

Close Monaw through its window controls. The launcher has already exited, so closing its old terminal is not the app's shutdown control. The source Vite server may remain running after Electron closes; this is expected, and a later start can reuse it.

| What changed? | What to do |
| --- | --- |
| Nothing; you only want to reopen Monaw | Run `start.bat`. |
| React, CSS, or other renderer source | Vite normally updates the running renderer. Restart if the app still shows stale state. |
| Python backend code or routes | Close and reopen Monaw so Electron starts a fresh backend. |
| Electron main/preload code | Close and reopen Monaw. |
| `backend/pyproject.toml`, `backend/uv.lock`, or the Python pin | Run `setup.bat`, then `start.bat`. |
| `frontend/package.json` or `frontend/package-lock.json` | Run `setup.bat`, then `start.bat`. |
| You pulled a mixed or unfamiliar set of changes | Run `setup.bat`, then `start.bat` to refresh both environments. |
| You imported a portable skill through Settings | No setup run or restart is needed; the installed skill is available for the next message. |
| An imported skill needs extra tools, packages, credentials, or an MCP server | Configure those separately; importing the skill does not install them. See [skills](skills.md). |

The source backend launch uses `--no-dev`, so uv can remove development-only packages from `.venv`. Before running pytest or backend lint tooling, run `uv sync --locked` in `backend` again, or use the repository's test scripts.

The installed app's **Check for updates** does not update a source checkout. To update a clean checkout of `main`:

```powershell
git status --short
git switch main
git pull --ff-only
.\setup.bat
.\start.bat
```

If `git status` lists your work, preserve it before switching branches or pulling. If `git pull --ff-only` reports divergence, resolve the Git history deliberately; the setup script does not reset, stash, or overwrite source changes.

## Manual dependency setup

Use this when uv and a supported Node.js/npm are already installed and you want to run each stage yourself. Close the app first; `npm ci` replaces its local dependency tree.

```powershell
Push-Location .\backend
try {
    uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependency sync failed.' }
} finally {
    Pop-Location
}

Push-Location .\frontend
try {
    npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency install failed.' }
    npm.cmd run check:electron
    if ($LASTEXITCODE -ne 0) { throw 'Electron executable verification failed.' }
} finally {
    Pop-Location
}

.\start.bat
```

If uv is only available through a custom path, replace the manual `uv` command with `& $env:AGENT_UV_PATH`. Setting the override is enough for `setup.bat` and `start.bat` themselves.

`npm ci` follows the lockfile and errors when it does not match `package.json`; it does not repair a mismatched lockfile. See the [npm ci reference](https://docs.npmjs.com/cli/commands/npm-ci/). Do not delete lockfiles as an installation workaround. Dependency changes should update and commit the manifest and its lockfile together.

## Paths and environment overrides

The source checkout and your personal data are separate:

```text
<checkout>\
  setup.bat
  start.bat
  scripts\                      Windows setup and launcher implementation
  backend\.venv\                 Re-creatable Python environment
  frontend\node_modules\         Re-creatable frontend dependencies and Electron
  frontend\dist\                 Generated renderer build; not used by normal Vite startup

%USERPROFILE%\.monaw\
  runtime\agent.db               Conversations, schedules, runs, and app state
  runtime\settings.json          Saved settings
  runtime\electron\              Electron state and encrypted credential storage
  runtime\backend.log            Electron's backend process output
  runtime\setup\setup.log        Most recent setup output
  runtime\launcher\vite.log      Vite output from a launcher-started server
  runtime\launcher\electron.log  Electron output from the launcher
  memory\                        Global and workspace memory
  skills\                        Created or imported skills
  browser\                       Browser artifacts and extension-related files

<Windows Documents>\monaw\YYYY-MM-DD\  Work from chats without a selected project
<selected project folder>\             Work from chats assigned to a project
```

Setup recreates dependencies, not your chat history or workspace files. It does not clear imported skills, settings, or saved connections. Separate checkouts and the installed app share the same Monaw home by default. To test without using your normal data, set a different `MONAW_HOME` before launch. Running two instances at once also requires distinct backend ports; two Vite-based checkouts still share the fixed renderer port, so run one such checkout at a time.

| Variable | Effect |
| --- | --- |
| `MONAW_HOME` | Base folder for Monaw application data. Takes precedence over `AGENT_HOME`. |
| `AGENT_HOME` | Legacy alternative to `MONAW_HOME`. |
| `AGENT_RUNTIME_DIR` | Backend runtime data and setup/launcher log location. Electron user data and its `backend.log` still live under `<Monaw home>\runtime`. Without an explicit home, the backend can also infer its home from the runtime override; set `MONAW_HOME` alongside it to keep all paths unambiguous. Use `MONAW_HOME` alone for a complete profile relocation. |
| `AGENT_WORKSPACE_DIR` | Root for new dated default-chat workspaces. Existing chats retain their saved paths. |
| `AGENT_MEMORY_DIR` | Override the backend memory folder. |
| `AGENT_UV_PATH` | Full path to `uv.exe`. Both batch-file workflows honor it even when uv is not on `PATH`. An invalid explicit path is reported as an error. |
| `MONAW_BACKEND_PORT` | Override the backend port for setup's port checks, the launcher readiness check, and Electron. Must be `1024`–`65535`. |
| `MONAW_RENDERER_MODE` | Advanced: `bundle` uses `frontend/dist` instead of Vite. Build it first; this does not make the source backend a packaged backend. Leave unset for ordinary source development. |

Examples apply only to the current PowerShell session and processes it launches:

```powershell
$env:AGENT_UV_PATH = 'C:\Tools\uv\uv.exe'
.\setup.bat
.\start.bat
```

For a separate development profile:

```powershell
$env:MONAW_HOME = Join-Path $env:LOCALAPPDATA 'Monaw-development'
$env:MONAW_BACKEND_PORT = '18435'
.\setup.bat
.\start.bat
```

Only use an unused port. A new `MONAW_HOME` starts with separate history, settings, skills, and encrypted connection state; it does not copy your existing profile. It does not change the Documents workspace root. Set `AGENT_WORKSPACE_DIR` too if you want separate default work files.

To return to default behavior, open a fresh terminal or remove the overrides you set:

```powershell
Remove-Item Env:MONAW_HOME, Env:MONAW_BACKEND_PORT, Env:AGENT_UV_PATH -ErrorAction SilentlyContinue
```

## Troubleshooting

### Setup fails or appears stuck

Keep the error text and inspect the latest log:

```powershell
Get-Content "$env:USERPROFILE\.monaw\runtime\setup\setup.log" -Tail 100
```

Replace the path when you use a custom home or runtime folder. For live updates in another terminal, append `-Wait`; press Ctrl+C there to stop following the log.

| Symptom | Check and recovery |
| --- | --- |
| `winget` is unavailable | Install uv and a supported Node.js manually, reopen the terminal, and rerun setup. Git must already be installed to clone. |
| uv was installed but cannot be found | Reopen the terminal so its `PATH` is refreshed, or set `AGENT_UV_PATH` to the full executable path. |
| `Unsupported Node.js` even after installation | Use `where.exe node` to find competing installations. The first resolved executable must satisfy `frontend/package.json`; installing another version does not necessarily change that order. |
| `npm ci` reports manifest/lockfile mismatch | Restore the matching files from the intended checkout, or update them together as a deliberate dependency change. Do not use `npm install` just to hide the mismatch. |
| Electron installation is incomplete | Check script settings and download connectivity as described below, then rerun setup. |
| uv or npm reports a network, proxy, or certificate error | Check the network and organization-specific proxy/CA configuration. Preserve certificate verification; configure the tool's documented trust settings instead of bypassing TLS checks. |
| A file in `.venv` or `node_modules` is locked | Close this checkout's app, test runners, and development servers, then rerun setup. Do not close unrelated programs by guessed process name. |
| Setup reports a busy port | Identify the listening program using the commands below. Close it yourself or choose an unused backend port. |
| The log stops during a large download | Check network activity and whether the command is still running. Some tools do not print continuous redirected progress. Save the error if the command eventually fails. |

Useful version and executable checks:

```powershell
git --version
uv --version
node --version
npm.cmd --version
where.exe node
where.exe uv
```

When uv is configured only through `AGENT_UV_PATH`, `where.exe uv` may legitimately find nothing. Check that override instead:

```powershell
& $env:AGENT_UV_PATH --version
```

### Electron was installed as a package but its executable is missing

From `frontend`, inspect the relevant settings:

```powershell
Push-Location .\frontend
try {
    npm.cmd config get ignore-scripts
    Get-Item Env:ELECTRON_SKIP_BINARY_DOWNLOAD -ErrorAction SilentlyContinue
    npm.cmd run check:electron
} finally {
    Pop-Location
}
```

`ignore-scripts=true` prevents Electron's download script from running. `ELECTRON_SKIP_BINARY_DOWNLOAD` can also skip it. If you intentionally set either for this terminal or project, remove or adjust that setting for this installation, then rerun setup. Current npm versions can require dependency scripts to be explicitly allowed; Monaw's `package.json` approves the locked Electron version. A future Electron version bump must keep that approval aligned. Do not enable every dependency script globally as a workaround.

### Start fails or the window does not appear

Read the terminal error first, then the relevant logs:

```powershell
Get-Content "$env:USERPROFILE\.monaw\runtime\launcher\vite.log" -Tail 80
Get-Content "$env:USERPROFILE\.monaw\runtime\launcher\electron.log" -Tail 100
Get-Content "$env:USERPROFILE\.monaw\runtime\backend.log" -Tail 100
```

An early prerequisite error can happen before these log files exist. A reused Vite server may have been started in another terminal, so its output will be there rather than in the current launcher log. Vite and Electron launcher logs are replaced when their corresponding commands are started; `backend.log` is appended and rotated by Electron.

| Symptom | Check and recovery |
| --- | --- |
| `Missing backend/.venv/...`, Vite, or Electron | Run `setup.bat` successfully, then start again. |
| Invalid backend port or unreadable `runtime.json` | Restore valid JSON and a numeric port; check `MONAW_BACKEND_PORT` for an accidental override. |
| `Runtime directory must not be a symlink or junction` | Point `MONAW_HOME` or `AGENT_RUNTIME_DIR` to an ordinary writable directory. |
| Vite exits before becoming ready | Inspect `vite.log`, verify frontend dependencies, and check port `5275`. |
| Backend exits before becoming ready | Inspect `backend.log` and `electron.log`; verify the locked backend environment and writable data folder. |
| A normal source start shows old UI | Confirm the Vite server belongs to this checkout. Another checkout on `5275`, or an intentional `bundle` mode with an old build, can show different code. |
| New backend features return errors after editing source | Close and reopen Monaw; the normal Electron-managed Python process does not auto-reload code. |
| Chat history takes time after the window opens | Let the startup screen finish. A healthy backend is separate from loading chat data; do not delete `agent.db` to speed up startup. |
| Launching again focuses an existing window or exits | Electron enforces one instance for a given Monaw home. Use or close that instance; a second profile needs a separate home and backend port. |

To inspect listeners without terminating anything:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 5275, 8435 -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, OwningProcess

# Replace 12345 with the OwningProcess value you found.
Get-Process -Id 12345
```

Use your overridden backend port in that command when applicable. A backend-only health check is:

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8435/health' -TimeoutSec 5
```

`/health` does not need authentication. A `401` from `/api/...` in an ordinary browser or terminal is expected without an authenticated desktop session; it does not mean installation failed. Do not expose the local backend through a tunnel or non-loopback bind to troubleshoot it. See [operations](operations.md).

## Non-interactive use and verification

The batch wrappers are meant for people: setup always pauses, and start pauses on failure. For a script or CI job, call the PowerShell implementation directly and check its exit code:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-windows.ps1
if ($LASTEXITCODE -ne 0) { throw 'Monaw setup failed.' }

powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-windows.ps1
if ($LASTEXITCODE -ne 0) { throw 'Monaw startup failed.' }
```

The execution-policy option applies to that PowerShell process; it does not change the user's permanent policy. The scripts do not bypass organization-managed restrictions. Full setup is not an isolated test: it can install prerequisites, replace dependencies, and close processes from its checkout. Prefer the isolated launcher regression tests when changing launcher logic:

```powershell
node --test frontend/electron/windows-launchers.test.cjs
```

For broader verification, run `scripts/verify.ps1`; see [development testing](development.md#testing). To report an app problem, prepare and review **Settings > Observability > Support report**. Avoid publishing raw environment dumps, credentials, or unreviewed logs.
