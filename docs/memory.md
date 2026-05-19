# Memory

Monaw memory stores durable user context outside normal chat history. Chat history stays in SQLite; long-term memories are human-readable markdown files.

## What Memory Is For

Use memory for durable information that should affect future conversations:

| Category | Use For | Example |
| --- | --- | --- |
| `preference` | User preferences and stable likes or dislikes. | Preferred model, preferred stack, communication preference. |
| `behavior` | How Monaw should behave repeatedly. | "Ask before changing public APIs." |
| `fact` | Stable facts about the user or environment. | User name, usual OS, common local paths. |
| `workflow` | Repeatable process notes. | How to verify a repo, deployment steps. |
| `project` | Project-specific context. | Repo conventions, active architecture decisions. |
| `reflection` | Session summaries and learned high-level notes. | Summary of a closed implementation session. |

Do not save secrets, tokens, passwords, one-off requests, volatile facts, or sensitive personal data.

## User Controls

Memory is configured in Settings -> Memory.

The important settings are:

| Setting | Meaning |
| --- | --- |
| `enabled` | Enables memory retrieval and memory tools. |
| `auto_learn` | Allows automatic learning when memory curation is active. |
| `curate_on_session_close` | Runs curation when a session is closed. |
| `write_policy` | Controls if writes are off, manual only, auto with review, or auto reviewed. |
| `retrieval_limit` | Maximum number of memories injected into a turn. |
| `max_injected_chars` | Character budget for retrieved memory context. |
| `min_confidence` | Minimum confidence for saved memory candidates. |
| `min_relevance_score` | Minimum retrieval score before a memory is used. |
| `maintenance_cooldown_hours` | Minimum time between maintenance passes. |

## Storage Location

By default, durable memory is stored under the Monaw home folder:

```text
%USERPROFILE%\.monaw\memory
```

Set `AGENT_MEMORY_DIR` to use a different memory root. You can also move all packaged Monaw data by setting `MONAW_HOME` or `AGENT_HOME`.

```powershell
$env:AGENT_MEMORY_DIR = "D:\Monaw\memory"
.\start.bat
```

The memory root contains:

```text
long-term\          Active durable memory files by category
archive\            Archived memory files and legacy migration output
short-term\         Short-term memory records
personalities\      Agent and user personality notes
.system\            Schema markers, curation output, and audit support
```

Long-term memory uses one markdown file per category:

```text
long-term\preference.md
long-term\behavior.md
long-term\fact.md
long-term\workflow.md
long-term\project.md
long-term\reflection.md
```

Archived long-term categories use:

```text
archive\preference.md
archive\behavior.md
archive\fact.md
archive\workflow.md
archive\project.md
archive\reflection.md
```

## File Format

The current schema is `sectioned-v1`.

Each category file has frontmatter and level-2 sections. A section has a stable id in the heading and JSON metadata in an HTML comment.

```markdown
---
category: preference
schema: sectioned-v1
updated_at: 2026-05-13T00:00:00+00:00
---

## Communication Style {#communication_style}

<!-- meta: {"id":"communication_style","category":"preference","importance":8,"confidence":0.9,"review_state":"reviewed","status":"active"} -->

The user prefers short, precise implementation updates.
```

The parser expects level-2 section headings after the frontmatter. If a file is malformed, the backend logs a warning and returns an empty list for that file instead of crashing the whole memory panel.

## How Memory Is Used

At the start of a turn, Monaw retrieves relevant memories and injects them as supporting context. Memories do not override system instructions, developer instructions, safety policy, tool contracts, approval requirements, or filesystem permissions.

The runtime can use memory in these ways:

| Action | Trigger |
| --- | --- |
| Retrieval | Automatic at the start of turns when memory is enabled. |
| Manual search | User or agent asks to inspect saved context. |
| Manual save | User explicitly asks Monaw to remember something. |
| Manual forget | User asks Monaw to stop using a saved memory. |
| Session curation | Session close flow extracts durable information when enabled. |

## Manual Editing

Use Settings -> Memory for normal editing. The UI exposes category tabs, section cards, and a whole-file markdown editor.

Safe edit rules:

| Rule | Reason |
| --- | --- |
| Keep headings as `## Title {#id}`. | Section ids are used for updates. |
| Keep metadata comments valid JSON. | The parser reads section state from the comment. |
| Use only known categories. | Unknown categories normalize to `fact`; legacy `style` normalizes to `preference`. |
| Do not paste secrets. | Memory is durable local context and can be injected into future prompts. |
| Prefer editing in the app. | The app validates files before saving. |

## API Surface

The backend exposes memory endpoints under `/api`:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/memories` | List memories with optional query, category, status, review state, and limit. |
| `POST /api/memories` | Create a manual memory. |
| `GET /api/memories/search` | Score memory relevance without marking memories as used. |
| `GET /api/memories/stats` | Return counters and memory root metadata. |
| `GET /api/memories/audit` | Read memory audit events. |
| `POST /api/memories/session/close` | Run session-close curation. |
| `GET /api/memories/files/{category}` | Read one category markdown file plus parsed sections. |
| `PUT /api/memories/files/{category}` | Replace one category markdown file after parsing validation. |
| `PATCH /api/memories/sections/{section_id}` | Update a single section. |
| `GET /api/memories/{memory_id}` | Read one memory by id. |
| `PATCH /api/memories/{memory_id}` | Update one memory by id. |
| `DELETE /api/memories/{memory_id}` | Delete one memory by id. |

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Memory panel looks empty. | Confirm Settings -> Memory is enabled and check the `memory_root` from `/api/memories/stats`. |
| New memory is rejected. | Content may be too short, unsafe, or secret-like. Check the backend error detail. |
| Edited file disappears from UI. | The markdown likely failed section parsing. Restore valid `##` sections and metadata comments. |
| Memory is not used in chat. | Confirm relevance threshold, retrieval limit, and whether the memory is archived. |
| Wrong folder is being used. | Check `AGENT_MEMORY_DIR`; otherwise Monaw uses `%USERPROFILE%\.monaw\memory`. |
| Old `style` memories are missing. | They are normalized into `preference`. |
