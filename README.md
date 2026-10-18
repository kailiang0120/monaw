# Monaw Agent: AI Desktop Assistant for Windows

<p align="center">
  <img src="docs/assets/monaw-agent-hero-v3.png" alt="Monaw Agent AI desktop assistant for Windows with chat and automation tools" width="100%" />
</p>

**Monaw Agent is an open-source AI desktop assistant for Windows with computer use, browser automation, file editing, and shell commands in one chat interface.**

Work in a folder you choose, keep useful context in local memory, schedule recurring tasks, and connect tools through the **Model Context Protocol (MCP)**. Sign in with ChatGPT through the Codex SDK, or bring your own OpenAI or Google Gemini API key. An optional Telegram bot lets you send Monaw tasks remotely.

Monaw runs on your PC and stores settings, chat history, memory, logs, and its default workspace locally. Model requests use your selected provider's service. Choose a permission mode, review tool access, and let the agent work with your Windows apps, Chrome browser, and project files.

Monaw currently targets Windows. macOS has not been tested yet.

## Why Monaw

- **Desktop-first workflow.** Work with files, browser pages, local commands, app windows, and scheduled follow-ups from one agent UI.
- **Local control.** Runtime data, memory files, logs, browser artifacts, and the default workspace stay under your local Monaw folder.
- **Project workspaces.** Choose a folder for each conversation so file tools, commands, and project instructions use the right workspace.
- **Choose your model connection.** Sign in with a ChatGPT account through the Codex SDK, or use OpenAI or Google Gemini through your own API keys.
- **Context discipline.** Memory, context tracking, and compression help keep useful knowledge available without blindly stuffing every conversation.
- **Four permission modes.** Use Default, Full Access, Auto Review, or a Custom config. Routine browsing works without repeated approvals.
- **Windows-friendly setup.** The one-click setup prepares Python, Node, backend packages, and frontend packages for normal Windows users.

## Example Tasks

- **File organization:** "Organize the files in this folder by type and show me what changed."
- **Browser research:** "Open this website in Chrome, collect the information I need, and save it as a CSV in my workspace."
- **Coding assistance:** "Use this project folder, inspect the code, and run its tests."
- **Windows app automation:** "Open this app, inspect its controls, and help me complete this form."
- **Scheduled checks:** "Every weekday at 9 AM, check this website and summarize any updates."

## What Monaw Can Do

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="docs/assets/use-cases/multi-model-chat.png" alt="Monaw mascot chatting with multiple model options" width="100%" />
      <br />
      <strong>Choose Your Model</strong>
      <br />
      Sign in with ChatGPT or use OpenAI or Gemini API keys. Switch models and reasoning effort from the chat composer.
    </td>
    <td width="50%" valign="top">
      <img src="docs/assets/use-cases/desktop-automation.png" alt="Monaw mascot automating browser files and local commands" width="100%" />
      <br />
      <strong>Work With Files, Apps, and Chrome</strong>
      <br />
      Read and edit files, run commands, and control Windows apps. Monaw launches your local Chrome profile with your existing logins and bookmarks.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="docs/assets/use-cases/memory-context.png" alt="Monaw mascot organizing memory and compressed context" width="100%" />
      <br />
      <strong>Keep Useful Context</strong>
      <br />
      Local memory, context tracking, and conversation compression help Monaw keep useful information available during longer tasks.
    </td>
    <td width="50%" valign="top">
      <img src="docs/assets/use-cases/scheduled-tasks.png" alt="Monaw mascot setting up scheduled task automation" width="100%" />
      <br />
      <strong>Schedule Tasks</strong>
      <br />
      Set reminders, recurring checks, and tasks for later. View scheduled runs and their results in the app.
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <img src="docs/assets/use-cases/mcp-integrations.png" alt="Monaw mascot connecting MCP and external tool blocks" width="100%" />
      <br />
      <strong>Connect More Tools</strong>
      <br />
      Browser control includes Chrome DevTools on your own Chrome profile, and you can add your own MCP servers. An optional Telegram bridge lets you send Monaw tasks remotely.
    </td>
    <td width="50%" valign="top">
      <img src="docs/assets/use-cases/permissions-local-control.png" alt="Monaw mascot guarding local permissions and sandbox controls" width="100%" />
      <br />
      <strong>Choose How Actions Are Approved</strong>
      <br />
      Default asks before changes and commands. Full Access runs without action prompts. Auto Review uses AI to review actions. Custom reads your config file. Optional Docker isolation contains shell commands.
    </td>
  </tr>
</table>

## Architecture

The system view shows where Monaw's components run. The agent view shows the repeating model-and-tools cycle. Both diagrams are native Mermaid, with editable source directly in this README.

### System Architecture

```mermaid
%%{init: {"theme": "base", "fontFamily": "Arial", "themeVariables": {"fontSize": "16px", "primaryTextColor": "#172c40", "lineColor": "#64748b", "edgeLabelBackground": "#ffffff", "clusterBkg": "#f8fafc", "clusterBorder": "#cbd5e1"}, "flowchart": {"curve": "basis", "nodeSpacing": 28, "rankSpacing": 55, "padding": 18}}}%%
flowchart TB
  accTitle: Monaw system architecture
  accDescr: Electron manages a local FastAPI backend. The shared agent runtime uses context management, permission-controlled skills, MCP tools and local persistence. Configured model providers, web services and Telegram are external connections.

  subgraph PC["WINDOWS PC / LOCAL APPLICATION"]
    direction TB
    subgraph DESKTOP["DESKTOP EXPERIENCE"]
      direction LR
      UI("<b>React + TypeScript</b><br/>Chat / attachments / steering<br/>Settings / schedules / workspaces")
      MAIN("<b>Electron main + preload</b><br/>Backend lifecycle / session tokens<br/>OS-encrypted credential vault")
      UI <-->|IPC| MAIN
    end

    subgraph BACKEND["PYTHON BACKEND / SHARED CONTROL PLANE"]
      direction TB
      API("<b>FastAPI</b><br/>Authenticated localhost API<br/>HTTP requests + SSE events")
      RUN("<b>Agent runtime</b><br/>Turn loop / model client / run state<br/>Conversation workspace + AGENTS.md")
      CONTEXT("<b>Memory + context</b><br/>History / retrieval / compaction<br/>Live context-budget management")
      GATE("<b>Execution controls</b><br/>Permissions / approvals<br/>Access grants / tool policies")
      REGISTRY("<b>Skill + tool registry</b><br/>Built-in and user skills<br/>Tool discovery + reflected MCP schemas")
      JOBS("<b>Task scheduler</b><br/>Recurring + one-time runs")
      TG("<b>Telegram bridge</b><br/>Optional remote task entry")
      STORE[("<b>Local Monaw data</b><br/>SQLite conversations / runs / traces<br/>Settings / Markdown memory / artifacts")]

      API --> RUN
      JOBS --> RUN
      TG --> RUN
      RUN <--> CONTEXT
      RUN -->|Requested actions| GATE
      GATE -->|Authorized calls| REGISTRY
    end

    subgraph TOOLS["EXECUTION / AUTOMATION"]
      direction LR
      FILES("<b>Files + shell</b><br/>Selected project workspace<br/>Shell sandbox / optional Docker")
      APPS("<b>Windows computer use</b><br/>Screenshots / accessibility<br/>Mouse + keyboard control")
      MCP("<b>MCP bridge</b><br/>Built-in Chrome DevTools<br/>Custom stdio + HTTP servers")
      WEB("<b>Web tools</b><br/>Scrapling page fetch<br/>Tavily search")
      CHROME[("<b>Your Chrome profile</b><br/>Existing logins + bookmarks")]
      MCP --> CHROME
    end

    DESKTOP <-->|Authenticated HTTP / SSE| BACKEND
    BACKEND -->|Authorized tool dispatch| TOOLS
    FILES ~~~ APPS ~~~ MCP ~~~ WEB
    CONTEXT <--> STORE
    RUN -.->|History + checkpoints + tool ledger| STORE
    JOBS <--> STORE
  end

  subgraph SERVICES["CONFIGURED EXTERNAL CONNECTIONS"]
    direction LR
    MODELS("<b>Model providers</b><br/>OpenAI API / ChatGPT via Codex<br/>Google Gemini API")
    REMOTE("<b>Connected services</b><br/>Websites / search / remote MCP")
    BOT("<b>Telegram Bot API</b><br/>Messages + run results")
  end

  PC <-->|Model and integration requests / responses| SERVICES
  MODELS ~~~ REMOTE ~~~ BOT

  classDef desktop fill:#eff6ff,stroke:#3b82f6,color:#1e3a8a,stroke-width:2px
  classDef agent fill:#172c40,stroke:#172c40,color:#ffffff,stroke-width:2px
  classDef runtime fill:#ecfdf5,stroke:#10b981,color:#064e3b,stroke-width:2px
  classDef tools fill:#f5f3ff,stroke:#8b5cf6,color:#4c1d95,stroke-width:2px
  classDef local fill:#fff7ed,stroke:#f59e0b,color:#78350f,stroke-width:2px
  classDef external fill:#f1f5f9,stroke:#94a3b8,color:#334155,stroke-width:1.5px
  class UI,MAIN desktop
  class RUN agent
  class API,CONTEXT,GATE,REGISTRY,JOBS,TG runtime
  class FILES,APPS,MCP,WEB tools
  class STORE,CHROME local
  class MODELS,REMOTE,BOT external
  style PC fill:#f8fafc,stroke:#cbd5e1,stroke-width:2px
  style DESKTOP fill:#f0f6ff,stroke:#bfdbfe
  style BACKEND fill:#f0fdf8,stroke:#a7f3d0
  style TOOLS fill:#faf7ff,stroke:#ddd6fe
  style SERVICES fill:#f8fafc,stroke:#cbd5e1,stroke-dasharray:6 4
```

Electron handles startup, credentials, and the authentication bridge; the React renderer calls FastAPI directly. Desktop chat, scheduled runs, and Telegram all enter the same agent runtime. Solid arrows show calls or data exchange; dashed arrows show lifecycle and persistence.

### Agent Design

Follow the arrows clockwise: **context → budget → model → authorization → execution → observation → context**. Tool results close the loop and inform the next model call. A completed answer exits from the model stage.

```mermaid
%%{init: {"theme": "base", "fontFamily": "Arial", "block": {"padding": 22}, "themeVariables": {"fontSize": "18px", "primaryColor": "#eff6ff", "primaryBorderColor": "#3b82f6", "primaryTextColor": "#172c40", "lineColor": "#0f766e"}}}%%
block-beta
  columns 3

  Entry["<b>NEW REQUEST</b><br/>Chat / scheduler / Telegram"] space Done(["<b>COMPLETE</b><br/>Answer + attachments"])
  space:3

  Context["<b>01 / CONTEXT</b><br/>History + memory<br/>Workspace instructions"] Budget["<b>02 / BUDGET</b><br/>Trim observations<br/>Compact older rounds"] Model["<b>03 / MODEL</b><br/>Context + visible tools<br/>Selected provider"]
  space:3

  Observe["<b>06 / OBSERVE</b><br/>Results + errors<br/>Update history"] Cycle["<b>THE AGENT LOOP</b><br/>Repeat until complete"] Gate["<b>04 / AUTHORIZE</b><br/>Permission policies<br/>Approvals + access grants"]
  space:3

  space Execute["<b>05 / EXECUTE</b><br/>Skills + MCP tools<br/>Shell sandbox policy"] space

  Entry --> Context
  Context ==> Budget
  Budget ==> Model
  Model ==> Gate
  Gate ==> Execute
  Execute ==> Observe
  Observe ==> Context
  Model --> Done

  classDef input fill:#172c40,color:#ffffff,stroke:#172c40,stroke-width:2px
  classDef thinking fill:#eff6ff,color:#1e3a8a,stroke:#3b82f6,stroke-width:2px,rx:14,ry:14
  classDef action fill:#ecfdf5,color:#064e3b,stroke:#10b981,stroke-width:2px,rx:14,ry:14
  classDef center fill:transparent,color:#0f766e,stroke:none,font-size:23px,font-weight:bold
  class Entry input
  class Context,Budget,Model thinking
  class Gate,Execute,Observe,Done action
  class Cycle center
```

| Alongside the loop | What happens |
| --- | --- |
| **Context and tool discovery** | The runtime combines workspace `AGENTS.md`, skill instructions, history, attachments, and retrieved memory. Tool search can expose deferred schemas for later model calls. |
| **Authorization** | Default, Full Access, Auto Review, and Custom modes determine action handling. Pending approvals or access grants wait for resolution; blocked actions return feedback, and repeated denials can pause the turn. |
| **Execution** | Compatible tool calls can overlap. Workers enforce timeouts, cooperative cancellation, and resource conflict checks. Shell commands use the configured sandbox policy; Chrome automation uses Chrome DevTools MCP on the user's own profile. |
| **User control and recovery** | Steering enters at model or tool boundaries. Stop, exhausted budgets, or failures can end or pause a turn; history, checkpoints, and the tool ledger support recovery. |
| **Persistence and observability** | Conversation history, results, run status, usage, latency, and failure evidence are recorded locally. Progress and tool events stream back to the caller. |

See [docs/development.md](docs/development.md) for the module map and implementation details.

## Requirements

- Windows 10 or 11. macOS is not tested yet.
- Git.
- Node.js 18+ and npm. The one-click setup can install this for you.
- `uv`. The one-click setup can install it for you, and `uv` installs the pinned Python 3.11 runtime automatically.
- Optional: Docker Desktop for stronger shell-command sandboxing.

## Install

Clone the repo:

```powershell
git clone https://github.com/kailiang0120/monaw.git
cd monaw
```

### Option A: One-Click Windows Setup

For normal Windows users, double-click [setup.bat](setup.bat):

```text
setup.bat
```

The setup launcher will:

- Close Monaw first if it is running. It only closes processes started from this folder. If another program holds one of Monaw's ports (Vite or the backend), setup stops and names it.
- Install `uv` with Windows Package Manager if it is missing.
- Install Node.js LTS with Windows Package Manager if Node/npm is missing.
- Install the Python version pinned in `backend\.python-version`.
- Create `backend\.venv` and sync the dependencies locked in `backend\uv.lock`.
- Run `npm ci` in `frontend`.

When setup finishes, double-click [start.bat](start.bat):

```text
start.bat
```

If setup fails, check:

```text
%USERPROFILE%\.monaw\runtime\setup\setup.log
```

### Option B: Manual Developer Setup

Install `uv`, then sync the locked backend environment:

```powershell
winget install --id=astral-sh.uv -e
cd backend
uv sync --locked
cd ..
```

Install the frontend packages:

```powershell
cd frontend
npm ci
cd ..
```

Start Monaw:

```powershell
.\start.bat
```

## First-Run Setup

Open Settings in Monaw Agent and configure only the services you use:

- OpenAI account sign-in or an OpenAI API key. In Settings > Connections, choose **Sign in with ChatGPT** for the account route. The API key route remains available separately.
- Google API key, if using Gemini.
- Tavily API key, if using web search tools.
- Telegram bot token and allowlist, if using Telegram.
- Model provider, model, and reasoning effort in the chat composer.
- Permission mode: Default, Full Access, Auto Review, or Custom. Custom rules are edited in the config file shown in Settings.
- Sandbox mode.
- Browser: the Chrome profile to use and allowed domains. Monaw drives your own Chrome through Chrome DevTools and handles launch and connection.
- MCP servers.

Normal desktop use does not require editing `.env`. Settings saves runtime configuration locally. Advanced Custom permission rules use the config file rather than individual setup switches.

## Daily Use

Start the app from the repo root:

```powershell
.\start.bat
```

The launcher starts the desktop app and exits after startup succeeds.

`uv` selects the pinned Python runtime and keeps `backend\.venv` in sync. If
`uv` is installed at a custom path, set `AGENT_UV_PATH` before launching:

```powershell
$env:AGENT_UV_PATH = "C:\path\to\uv.exe"
.\start.bat
```

## Local Data

Runtime app data is local. Monaw keeps user-visible data under:

```text
%USERPROFILE%\.monaw\
```

Main folders:

```text
%USERPROFILE%\.monaw\runtime\        settings, database, logs, Electron store
%USERPROFILE%\.monaw\workspace\      default agent working directory
%USERPROFILE%\.monaw\browser\        browser screenshots
%USERPROFILE%\.monaw\memory\         global and workspace memory markdown files
```

Settings, config, logs, browser screenshots, memory, and the default agent workspace all stay under this folder. Your Chrome profile stays in Chrome's own folder.

You can change the main data folder with `MONAW_HOME` or `AGENT_HOME`, the runtime folder with `AGENT_RUNTIME_DIR`, the default agent workspace with `AGENT_WORKSPACE_DIR`, and the memory folder with `AGENT_MEMORY_DIR`.

Chats open immediately in the default workspace. To work on a project, click **Default workspace** above the message box and choose a folder. The selection is saved for that conversation and applies to commands, relative file paths, and project `AGENTS.md` instructions. Use the reset button beside the folder to return to the default workspace. Folder changes are available while the agent is idle; existing permission and sandbox rules still apply. Chat history stays in the runtime database.

## Feature Docs

While the agent is working, type a new instruction and press Enter or the send arrow to steer the same task. Stop remains available beside it. Steering takes effect after the current model or tool step, and the message shows whether it is waiting or applied. New text, pasted images, and attached files stay in the same chat history. If the turn finishes before delivery, the draft stays in the composer for sending as a new turn.

Memory works in the background; turn it on or off with the Memory switch under Settings -> Identity. Memory files are local markdown files. See [docs/memory.md](docs/memory.md) for advanced configuration.

Browser automation is configured in Settings -> Browser. See [docs/browser-automation.md](docs/browser-automation.md). Planned improvements, such as fuller control of your own Chrome profile, are listed in [docs/roadmap.md](docs/roadmap.md).

Windows computer use combines screenshots and accessibility controls for desktop automation. See [docs/computer-use.md](docs/computer-use.md).

MCP servers are configured in Settings -> Tools -> MCP. See [docs/mcp.md](docs/mcp.md).

Scheduled tasks are configured in the app when scheduling is enabled. See [docs/scheduling.md](docs/scheduling.md).

Telegram is optional. Keep the bot token private and keep the allowlist narrow. See [docs/telegram.md](docs/telegram.md).

Choose one of four permission modes in Settings or the chat composer. Custom rules are configured only in the runtime config file; sandbox options remain in Settings. Auto Review uses conversation context to approve authorized actions, deny clear violations or ask you when uncertain. Denial reasons are visible, unchanged retries are blocked within the turn, and repeated denials pause it. Default, Full Access, and Auto Review still enforce protected-path and sensitive-app restrictions. See [docs/permissions.md](docs/permissions.md) and [docs/sandboxing.md](docs/sandboxing.md).

The backend API is a privileged local control plane. The desktop application
authenticates every `/api` request with a short-lived session and binds the
backend to loopback by default. Do not expose the backend through port
forwarding, reverse proxies, tunnels, permissive firewall rules, or non-loopback
binds. See [docs/operations.md](docs/operations.md).

Provider keys and the Telegram bot token are encrypted through Electron's
OS-backed `safeStorage`. Saved credential values are write-only from the
renderer: the UI can see whether a value exists, but cannot read it back.

The generated endpoint reference is [docs/api.md](docs/api.md).

## Troubleshooting

If `start.bat` fails before the app opens, check:

```text
%USERPROFILE%\.monaw\runtime\launcher\vite.log
%USERPROFILE%\.monaw\runtime\launcher\electron.log
```

If the app opens but cannot connect to the backend, check:

```text
%USERPROFILE%\.monaw\runtime\
```

If backend imports fail, restore the locked backend environment:

```powershell
cd backend
uv sync --locked
```

If frontend startup fails, reinstall frontend packages:

```powershell
cd frontend
npm ci
```

If Vite reports port `5275` is already in use, stop the existing Vite process or reuse it intentionally.

## Developer Docs

Developer setup, architecture, module map, testing, and packaging notes are in [docs/development.md](docs/development.md).

## Repository Status

This public repository is currently intended for cloning, local use, and code review. External contributions are not being accepted yet; issues, pull requests, and Discussions may stay disabled until the project is ready for community maintenance.

## License

Apache License 2.0. See [LICENSE](LICENSE).
