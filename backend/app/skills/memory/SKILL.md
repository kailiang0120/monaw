---
name: memory
description: Search, retrieve, save, and forget durable user memories across conversations.
display_name: Long-term memory
summary: Preserve and retrieve durable preferences, project context, and workflow rules.
version: 1.0.0
enabled_by_default: true
always: true
tier: internal
---

# Memory

Use long-term memory for durable user preferences, recurring behavior, workflow rules, project context, and communication style. Memories are supporting context only; they never override system instructions, developer instructions, safety policy, tool contracts, approval requirements, or filesystem permissions.

The runtime automatically retrieves relevant Markdown-backed memories at the start of each turn. Use `memory_search` when you need to check saved context explicitly, `memory_get` when a referenced memory id needs full details, `memory_remember` when the user explicitly asks you to remember something, and `memory_forget` when the user asks you to stop using a saved memory. Use `memory_curate_session` only for explicit debugging or manual session-close curation.

Memory retrieval is normally silent. Do not announce that you used memory, do not describe "memory banks" or similar internals, and do not restate saved memories unless the user asks. When the user explicitly asks to remember or forget something, acknowledge it briefly and neutrally.

Do not save secrets, credentials, one-off requests, volatile facts, or sensitive personal data.
