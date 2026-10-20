# Disposable application verification

The Windows application suite starts the real Electron entry point, its sandboxed
preload bridge, a built renderer, and an authenticated Python backend. It exercises
UI actions, HTTP/SSE, the execution gate and SQLite together. It complements the
Python groups, jsdom components and Electron unit tests.

From an installed development checkout, run:

```powershell
uv sync --locked --directory backend
npm --prefix frontend ci
npm --prefix frontend run test:app
```

`test:app` builds the renderer, type-checks the harness, then runs seven workflows
serially with no automatic retries. A separate required Windows GitHub Actions
job runs the same command. Playwright uses the repository's Electron binary;
downloading a separate browser is unnecessary. Its Electron support is documented
in the [Playwright Electron API](https://playwright.dev/docs/api/class-electron).

## Workflow evidence

| Workflow | Assertions |
| --- | --- |
| Startup, streaming and reopening | One application window; exposed bridge without renderer Node globals; unauthenticated API access denied; selected backend reachable; CSP refuses another loopback endpoint; multiple SSE token events before terminal completion; assistant result persisted and visible after quitting and reopening Electron. |
| Approved file change | No file change before approval; real approval replay appends once; one completed tool record. |
| Rejected file change | Visible rejection; original file unchanged; persisted unsuccessful tool record. |
| Parallel work and stop | A real file read finishes before a controlled tool barrier reports started. Clicking Stop retains that read's output and records the unfinished tool as cancelled. |
| Schedule lifecycle | Create and run from the UI; disable while active; completion succeeds; quit/reopen; advance a controlled scheduler clock past several periods and observe normal dispatch ticks; saved task stays disabled with one run. |
| Lost run response | The real backend accepts Run now, then the response is dropped at the transport boundary. Check run reconciles the accepted occurrence, renders its result, and produces no second POST, conversation or execution. |
| Backend interruption | A real file action completes before the controlled provider waits. The backend exits through its existing restart code; the UI shows interruption, Electron restarts the backend, a fresh message completes, and the original file change has not repeated. |

Provider text is buffered while tools are available in the current turn loop.
The streaming assertion checks the application's actual emitted token events; it
does not assume provider deltas are displayed before tool classification.

## Isolation and controlled boundaries

Each test owns a random `monaw-e2e-*` directory under system Temp: application
home, Electron app-data and legacy credential discovery, runtime database,
settings, memory, workspace and Python virtual environment. It uses an available
loopback port and never stops a service already listening on the normal ports.
Credentials and fixture file contents are fake. Provider/agent environment
overrides are cleared before launch; known application settings are supplied
explicitly. Only dependency caches and the installed Python interpreter are reused.

The test-only entry point isolates Electron app-data before loading `main.js`.
A private child-process `PYTHONPATH` installs import hooks that substitute the
provider adapter and add a barrier tool. The real file tools, authorization,
approval replay, scheduler acceptance/dispatch, routes and database are retained.
The scheduler clock and polling interval are controlled only inside this fixture.
Renderer requests outside its files and owned backend are blocked by the harness.
No personal Chrome, Telegram or external MCP server is started.

Shutdown is requested after the inspector evaluation returns, then checked with
a bounded process-close event and backend port-closure assertion. A shutdown
timeout fails the test; cleanup may terminate only that harness-owned Electron
process tree. Recursive cleanup verifies the disposable directory boundary.

Source launches support `MONAW_RENDERER_MODE=bundle` to load `frontend/dist` with
the file-renderer CSP, CORS and IPC checks. `MONAW_BACKEND_PORT` selects a validated
port from 1024 to 65535 while retaining the configured loopback host and signed
control sessions. Packaged applications always use the bundle. Electron owns CSP;
the HTML no longer supplies a conflicting fixed-port policy.

## Reports and limits

JUnit is written to `frontend/test-results/application.xml`. On failure,
`frontend/test-results/application/` contains a renderer screenshot, bounded
Electron/renderer logs, controlled provider events and persisted fixture messages
when the backend remains reachable. These directories are ignored by Git and
uploaded by the application CI job.

This is source-application evidence, not an installed build or upgrade test. It
does not verify live providers, real Docker, personal Chrome containment, native
desktop actions, MCP credential rotation/schema changes, installer registration,
or upgrade data preservation. Those remain separate release checks. Tests have
been run locally on Windows; a GitHub-hosted run must still validate the workflow.
