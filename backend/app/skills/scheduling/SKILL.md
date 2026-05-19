---
name: scheduling
description: Create and manage local scheduled agent tasks, including cron schedules, from chat.
version: 1.0.0
enabled_by_default: false
tier: optional
---

Use scheduled task tools when the user asks the agent to run a prompt later, repeatedly, or on a cron schedule.

Rules:
- For natural-language scheduling requests, convert the request into a clear title, prompt, schedule kind, timezone, and cron expression or interval.
- Use `scheduled_task_create` to create cron, interval, or one-time tasks.
- If scheduled task tools are not visible, call `tool_search` with a query like `scheduled task cron`.
- Keep confirmations short and include the next run time returned by the tool.
