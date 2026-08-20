---
name: exec
description: Unified shell execution with policy, approval, and audit hooks.
display_name: Command execution
summary: Run local commands through Monaw's policy, approval, and sandbox controls.
version: 1.0.0
enabled_by_default: true
always: true
tier: internal
---

Use `exec` when the task requires local command execution.

Execution and safety:
- Choose the smallest shell that fits the task and set `workdir` when the command depends on repo context.
- Every command is classified and checked against permission, access-grant, approval, and sandbox policy.
- The default workspace is trusted; blocked disk-formatting commands and blocked system paths/processes remain denied.
- Elevated execution is intentionally unavailable. An approval request does not bypass a hard policy block.
- `auto` prefers Docker strong isolation for untrusted work and reports an actionable fallback when Docker is unavailable. `direct_rw` writes to the allowed workspace, `copy_out` runs from a temporary copy and collects outputs, and `discard` keeps command filesystem changes ephemeral.
- Docker uses the configured pinned image, no network by default, a read-only container root, dropped capabilities, process/memory/CPU limits, and `/workspace` mounts only under configured bind roots.

Output and sessions:
- Use `return_mode` (`full`, `head_tail`, `tail`, or `summary`) with `max_stdout`, `max_stderr`, and `tail_lines` to bound responses.
- Long-running commands use `exec_start`, `exec_poll`, `exec_write_stdin`, and `exec_stop`; stdin is rechecked by policy for every write.
- Sessions expire after a bounded lifetime and completed output remains pollable until the session is cleaned up.
- Large or truncated output is written under the runtime artifact directory and returned with artifact/manifest paths. Those artifacts follow the configured retention policy.
