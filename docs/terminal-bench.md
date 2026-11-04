# Terminal-Bench design

Status: design, branch `terminal-bench`. Nothing here is built yet.

Goal: run the shipped Monaw agent, headless and unmodified, inside the Linux task
containers of [Terminal-Bench 2.0](https://www.tbench.ai/) through its official harness,
[Harbor](https://github.com/harbor-framework/harbor), using a ChatGPT sign-in for the
model. Use the scores to compare Monaw commits (for example before and after a turn-loop
change) and model/effort settings.

## What the spike showed

A throwaway `ghcr.io/astral-sh/uv:python3.11-bookworm-slim` container with `backend/`
mounted read-only (2026-11-02, commit `8d5f9d9`):

| Check | Result |
| --- | --- |
| `uv sync --locked --no-dev` | Installs; the Windows-only packages are skipped by their markers. |
| `from app.main import app` | Imports; no Windows module is needed at import time. |
| `monaw_backend.py --port 8435` | Serves `/health`; no errors in the backend log. |
| Control token | `mint_control_token(secret)` with `MONAW_CONTROL_SECRET` set authenticates `/api/settings`, `/api/sandbox/status` and `/api/diagnostics/summary`. |
| Shell | Sandbox `auto` reports `host_execution_approval_required`; host commands default to `bash` on Linux (`sandbox/policy.py`). |

Not yet checked: a real turn with tools on Linux, which tools register without
Windows, and the ChatGPT plan call from a container.

## Shape

```
WSL2 Ubuntu (host for the run)                      Task container (one per trial)
------------------------------                      ------------------------------
harbor run -a                                       /installed-agent/monaw/   bundle: source + .venv + python
  bench.monaw_harbor:MonawAgent                     /installed-agent/home/    MONAW_HOME (DB, logs, settings)
                                                    /run/monaw-bench/token.json  leased access token
MonawAgent (BaseInstalledAgent)                     /logs/agent/              exported results, synced back
  install()   upload + unpack bundle      ------>
  configure() write token, settings       ------>   launch.py   backend on 127.0.0.1, token from file
  run()       exec drive.py               ------>   drive.py    one chat job in the task's workdir,
              refresh token every 20 min  ------>               approves tickets, exports results
  populate_context_post_run()             <------   /logs/agent/result.json, conversation.json

TokenKeeper (one per Harbor process)
  refresh token stays here (~/.monaw-bench), hands out 1-hour access tokens
```

Monaw runs inside the task container because Terminal-Bench grades the container's
final state and Monaw's tools act on the machine they run on. Nothing in `backend/`
changes for the benchmark; all glue lives under `bench/`.

## Pieces

### `bench/headless/` (runs in the task container, Monaw's Python 3.11 venv)

- `launch.py`: replaces `chatgpt_account` with a leased-token reader before importing
  `app.main`, then starts uvicorn on `127.0.0.1:<free port>`. `access_token()` reads
  `/run/monaw-bench/token.json` (`{"access_token", "expires_at"}`); `force_refresh`
  re-reads the file, so `llm_client`'s 401 retry picks up a token the host just wrote.
  `status()` reports connected. Patching at launch keeps the product unchanged; the
  cost is one test that fails if `chatgpt_account`'s interface moves.
- `drive.py`: mints a control token, applies the settings below with `PUT /api/settings`,
  starts one job with `POST /api/chat/jobs` (`message` = the task instruction,
  `workspace_dir` = the container's working directory, usually `/app`), and follows
  `GET /api/chat/jobs/{id}/stream` until `done`. On `approval_required` or
  `access_grant_required` it approves through the API, as the user would, and counts
  it. Afterwards it writes `result.json` (status, reason code, duration, usage, tool-call
  counts, approvals), `conversation.json` (messages with full tool calls) and the
  observability run to `/logs/agent/`.

Settings applied for every trial:

| Setting | Value | Why |
| --- | --- | --- |
| `llm.provider` / `model_name` / `reasoning_effort` | `codex` / from `-m` / from `--ak effort=` | ChatGPT sign-in route. |
| `llm.max_turn_seconds` | Harbor agent timeout minus 90 s | Monaw stops and saves before Harbor kills the container, so results are exported. |
| `llm.max_tokens_per_turn` | default (5M), `--ak` to change | Report pauses; don't hide them. |
| `permissions.mode` | `full_access` | Disposable container; no user to ask. |
| `sandbox.mode` | `host` | No Docker inside the task container; commands must change the task's own machine. |
| Browser, computer use, Telegram, scheduler, MCP | off | Not available on Linux or not part of the task. |
| Memory learning | off | Each trial has a fresh `MONAW_HOME` anyway; keeps prompts identical across trials. |

The backend is started with the container's own `PATH`, calling `.venv/bin/python`
directly, so commands the agent runs see the task's Python and tools, not Monaw's.
`MONAW_HOME` is under `/installed-agent`, outside the workspace, because graders
inspect `/app`.

### `bench/bundle/` (built once per Monaw commit)

`Dockerfile` + `build.sh` produce `monaw-bundle-<commit>.tar.zst`: the backend source,
a `uv`-managed CPython 3.11 and the locked `.venv`, built at the final path
`/installed-agent/monaw` so nothing has to be relocated. Build on an old glibc base
(Debian bullseye, glibc 2.31) so the bundle runs on older task images. `install()`
uploads and unpacks it in seconds instead of running `uv sync` (about a minute and many
downloads) in every trial. Fallback for musl (Alpine) images: install `uv` and run
`uv sync --locked --no-dev` in the container.

The same image doubles as the standalone "Monaw in Docker" for manual runs:
`docker run -it monaw-headless drive.py "task text"`.

### `bench/monaw_harbor/` (Harbor side, Python 3.12+, own `pyproject.toml`)

- `MonawAgent(BaseInstalledAgent)`: `install()` uploads the bundle; `configure()` writes
  the first token and `MONAW_HOME`; `run()` executes `drive.py` with the rendered
  instruction while a background task rewrites `token.json` every 20 minutes with
  `environment.upload_file`; `populate_context_post_run()` fills `n_input_tokens`,
  `n_cache_tokens`, `n_output_tokens` and `metadata` (Monaw reason code, tool calls,
  approvals, bundle commit) from `result.json`. Agent kwargs: `bundle`, `effort`,
  `max_tokens_per_turn`.
- `TokenKeeper`: one per Harbor process. Calls
  `uv run --project backend python -m bench.chatgpt_token` (Monaw's own
  `ChatGPTAccountService`, `MONAW_HOME=~/.monaw-bench`) when the cached token has less
  than 25 minutes left, behind an asyncio lock and a file lock. ChatGPT refresh tokens
  rotate (`refresh_token_reused` exists), so only this process may refresh; copying the
  credentials into containers would make parallel trials revoke each other. Containers
  only ever hold a short-lived access token.

### `bench/chatgpt_login.py` (once)

Signs in to `~/.monaw-bench` from WSL with Monaw's normal login flow. The callback
server listens on `127.0.0.1:1455`; WSL2 forwards localhost, so the Windows browser
completes it. The Windows app's sign-in can't be reused: it is DPAPI-encrypted, and
reusing its refresh token would fight the desktop app over rotation.

## Running it

Run from WSL2 Ubuntu-24.04 with Docker Desktop's WSL integration on. Harbor needs
Python 3.12+ and a Docker daemon; it hasn't been tried on Windows directly.

```bash
uv run python bench/chatgpt_login.py
bench/bundle/build.sh
uvx harbor run -d terminal-bench@2.0 -a oracle -n 4
uvx harbor run -d terminal-bench@2.0 \
  -a bench.monaw_harbor:MonawAgent -m codex/<model> \
  --ak bundle=bench/bundle/out/monaw-bundle-<commit>.tar.zst --ak effort=high \
  -n 2 -i '<task>' --jobs-dir bench/jobs
```

`-a` takes a built-in agent name or an import path (`--agent-import-path` is deprecated).

`oracle` first proves the Docker setup grades correctly before Monaw is involved.

## Stages

1. **Headless Monaw in Docker.** `launch.py`, `drive.py`, the bundle image, and three
   hand-written tasks (edit a file, fix a failing test, install a package and use it)
   run with `docker run`. Done when all three finish with the expected files and
   `result.json` has usage and tool calls.
2. **Harbor adapter.** `MonawAgent`, `TokenKeeper`, `chatgpt_login.py`. Done when the
   oracle run passes and 5 chosen Terminal-Bench tasks run end to end with token
   refresh exercised (one trial forced past an hour, or a short `expires_at`).
3. **Full run.** All tasks, 1 attempt, `-n` set from what the ChatGPT plan's limits
   allow. Record pass rate, failures by Monaw reason code, tokens and wall time.
4. **Comparisons.** Same tasks and model for two bundles (two commits), then 5 attempts
   for the final numbers. A small `bench/report.py` joins Harbor's job results with
   `result.json` into one table.

## Fairness and what counts

- One instruction, no follow-up messages. If Monaw ends its turn asking a question,
  the trial ends there; an optional `--ak continue_nudges=N` exists only for diagnosis
  and is reported separately.
- No task-specific prompts, tools or workspace `AGENTS.md`. Monaw's normal system prompt
  and skills only.
- Harbor's task timeouts are not changed.
- Approvals are counted, not hidden: a run that needed 30 auto-approvals reads
  differently from one that needed none.

## Risks and open questions

| Risk | Plan |
| --- | --- |
| ChatGPT plan usage limits during parallel runs | Start at `-n 2`; `drive.py` reports `plan_error_from_response` codes; back off per job. |
| Tasks with an agent-phase network allowlist | Monaw only needs `api.openai.com` from the container. List such tasks after stage 2; they may need the host added. |
| Bundle doesn't run on a task image (glibc, musl, no `tar`/`zstd`) | Check `ldd --version` and tools in `install()`; fall back to `uv sync`; record which tasks used the fallback. |
| Linux-only gaps in Monaw (tools assuming Windows paths or PowerShell) | Stage 1 finds them. Fixes go to `main` as normal product changes, not benchmark glue. |
| The `chatgpt_account` patch drifts from the product | A test in `bench/headless` imports the real module and checks the methods it replaces. |
| Access token readable by the agent inside the container | Accepted: it expires within an hour and the container is discarded. The refresh token never enters a container. |
| Harbor API changes | Pin the Harbor version in `bench/monaw_harbor/pyproject.toml`. |
