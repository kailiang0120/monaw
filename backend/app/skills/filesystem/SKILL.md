---
name: filesystem
description: Windows filesystem operations with policy gating.
version: 1.0.0
enabled_by_default: true
tier: recommended
os:
  - windows
---

Use these tools for file and folder operations.

Rules:
- Prefer `fs_stat`, `fs_list`, `fs_tree`, `fs_read`, `fs_search`, `fs_hash`, `fs_write`, `fs_append`, `fs_patch`, `fs_mkdir`, `fs_copy`, `fs_move`, `fs_rename`, and `fs_delete` for direct file and folder work.
- Use `explorer_open` or `explorer_reveal` only when the user needs Windows Explorer to open or show a path.
- Prefer exact paths over descriptions.
- Delete is high-risk and may require approval.
