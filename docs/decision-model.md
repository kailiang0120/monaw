# Decision Model (Shadow Mode)

Monaw can ask a small local decision model, [Laya](https://huggingface.co/convaiinnovations/laya), which skills a request needs before the LLM picks tools. Only shadow mode exists today. Laya's answer is logged next to the tools the LLM actually called, and the LLM still receives every visible tool. The log data is for tuning a selection policy before Monaw lets it narrow the tool list.

Laya runs alongside each turn and never delays the reply. A slow or unreachable server only produces a `timeout`, `error` or `unavailable` record. After a connection failure Monaw waits 30 seconds before it calls the server again.

## Setup

`setup.bat` installs Laya for you:

- It installs `laya[serve]` into its own virtualenv at `%USERPROFILE%\.monaw\laya\.venv`. Laya brings torch and transformers, which do not belong in the backend environment.
- It downloads the English checkpoint, about 800 MB the first time, into the Hugging Face cache.

If this step fails, setup prints a warning and finishes anyway, since Monaw works without Laya. Run `setup.bat` again to retry. To skip it, set `MONAW_SKIP_LAYA=1` before running setup.

`start.bat` needs no extra step. When the backend starts, it launches the installed `laya-serve` hidden on `127.0.0.1:8421` and stops it on shutdown. The server log is `%USERPROFILE%\.monaw\runtime\launcher\laya-serve.log`. Loading the checkpoint takes up to a minute on CPU, so the first turns after a start may be recorded as `unavailable`.

## Settings

The `decision_model` block in `settings.json` (in the runtime directory) or `PUT /api/settings` controls it:

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Ask the decision model on every turn. |
| `managed` | `true` | Run the Laya server that setup installed. When it is not installed, the selector stays off. |
| `mode` | `shadow` | Only `shadow` is supported. |
| `endpoint` | `http://127.0.0.1:8421` | `laya-serve` base URL. A system proxy is bypassed for loopback hosts. |
| `model` | `""` | Laya checkpoint name. Blank uses the server default. |
| `timeout_ms` | `3000` | Per-request timeout. |
| `coverage` | `0.9` | Keep the top domains until their probability adds up to this. |
| `max_domains` | `4` | Most domains to keep. |
| `min_confidence` | `0.4` | Below this top probability, keep every tool. |
| `pinned_domains` | `core`, `memory`, `tool-search` | Always kept and never asked about. |

To use a Laya server you run yourself, set `managed` to `false` and point `endpoint` at it. If that server sets `LAYA_API_KEY`, put the same `LAYA_API_KEY` in the backend `.env`. The sandbox strips `LAYA_*` variables from commands it runs.

Settings changes take effect on the next backend start.

## How A Decision Is Made

Each skill is one domain, named after the skill (`exec`, `filesystem`, `web-search`, and so on). Each MCP server is its own domain, `mcp:<server>`. Laya gets the latest request plus one `choice` question whose options are the unpinned domains, each described by its SKILL.md summary or its MCP server description. Options include domains whose tools stay hidden until tool search loads them, such as `scheduling`.

The previous exchange is sent only for short follow-ups of six words or fewer, such as "yes, do it". For a full request it pulled the choice toward the last topic: after a scheduling turn, an exchange-rate request scored 97% `scheduling`.

There is no "no tool needed" option. In testing, Laya over-picked it and confidently dropped domains the request needed. Keeping a domain the turn did not use only costs prompt tokens.

## Read The Results

Each turn writes a `tool_selection_shadow` event to the observability database. Its metadata holds the full probability distribution, the kept domains, the domains the LLM used, `covered`, and the tool and schema savings. It stores no request text. To replay other thresholds against stored decisions, run this from `backend`:

```bash
.venv/Scripts/python -m app.agent.tool_selection_report
```

Only consider letting the selector narrow the tool list once a coverage and confidence setting covers nearly every turn on your own traffic.
