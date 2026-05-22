---
name: skill-creator
description: Create optional Monaw runtime skills from inside the agent. Use when the user asks the agent to add a new skill, scaffold SKILL.md instructions, add optional tools.py tool code, reload skills, or restart the backend after a skill update.
version: 1.0.0
enabled_by_default: true
tier: recommended
---

# Skill Creator

Use this skill to add or refresh Monaw runtime skills under `backend/app/skills`.

Default created skills to optional:

- `enabled_by_default: false`
- `tier: optional`
- concise `SKILL.md` instructions
- optional `tools.py` only when deterministic tool code is actually useful

Prefer `skill_create` for new skills and `skill_reload` after editing an existing skill by file tools. Use `backend_restart` only when a Python process restart is required, such as dependency or import-state changes that runtime reload cannot pick up.

When creating a skill with tools:

1. Keep the tool API narrow and task-specific.
2. Validate paths before file writes.
3. Return JSON strings with `status`, useful paths, and next action.
4. Mark mutating tools as not parallel-safe in metadata.
5. Keep generated skills disabled unless the user asked to enable them now.
