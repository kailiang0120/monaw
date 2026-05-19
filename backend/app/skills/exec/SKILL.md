---
name: exec
description: Unified shell execution with policy, approval, and audit hooks.
version: 1.0.0
enabled_by_default: true
tier: recommended
---

Use `exec` when the task requires local command execution.

Rules:
- Choose the smallest shell that fits the task.
- Set `workdir` when the command depends on repo context.
- Never request `elevated=true` unless absolutely necessary.
