"""A turn-local planning tool that cannot execute external actions."""

import json

from app.agent.tool_registry import ToolRegistry

TASK_PROTOCOL = """## Task outcomes
For a task with multiple requested outcomes or file changes, call task_plan before acting.
List the user's requested outcomes, including checks and delivery, with stable IDs and clear criteria.
Use kind=file_change and all target paths for native file edits; kind=answer for the final explanation;
use kind=general for reads, checks, and other work. Keep pending and failed outcomes until resolved.
Shell, MCP, or other effects without native file verification need kind=general with independent checks.
Tool results include task_evidence IDs. Mark outcomes done only with matching successful evidence;
file changes require verified_file_change evidence for every target. Do not invent evidence IDs.
Existing outcomes can be updated using only id, status, evidence_ids, and reason. Use steps=[] to inspect saved evidence.
Correct an unfinished outcome's mistaken kind with a reason, preserving its criteria and targets.
Reuse recorded evidence. Never repeat or alter completed file edits just to fix task bookkeeping.
Use status=waiting_user or blocked with a specific reason when information or permission is missing,
then explain it to the user. Continue preserves the existing task and its criteria.
Only set new_task=true when the user has explicitly replaced the task with a new request.
"""


class TaskToolRegistry(ToolRegistry):
    """Overlay a private tool while retaining live discovery on the base registry."""

    def __init__(self, base, progress):
        """Bind task updates to this conversation without mutating shared tools."""
        self.base = base

        async def update(steps, status=None, reason=None, new_task=False):
            """Return a bounded validation error without discarding current work."""
            try:
                return json.dumps(
                    {
                        "status": "ok",
                        "task": progress.update(steps, status=status, reason=reason, new_task=new_task),
                    }
                )
            except ValueError as exc:
                return json.dumps(
                    {
                        "status": "error",
                        "reason_code": "invalid_task_progress",
                        "error": str(exc),
                        "task": progress.snapshot(),
                    }
                )

        super().__init__(
            [
                {
                    "name": "task_plan",
                    "description": "Record or update task outcomes and their execution evidence. Omitted outcomes are retained.",
                    "callable": update,
                    "execution_mode": "async",
                    "domain": "harness",
                    "metadata": {
                        "parallel_safe": False,
                        "risk_level": "low",
                        "repeat_safe": True,
                        "internal_task_progress": True,
                    },
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "steps": {
                                "type": "array",
                                "maxItems": 32,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,64}$"},
                                        "description": {"type": "string", "minLength": 1, "maxLength": 500},
                                        "criteria": {"type": "string", "minLength": 1, "maxLength": 1000},
                                        "kind": {
                                            "type": "string",
                                            "enum": ["file_change", "general", "answer"],
                                        },
                                        "targets": {
                                            "type": "array",
                                            "maxItems": 32,
                                            "items": {"type": "string", "maxLength": 1024},
                                        },
                                        "status": {
                                            "type": "string",
                                            "enum": ["pending", "active", "done", "failed"],
                                        },
                                        "evidence_ids": {
                                            "type": "array",
                                            "maxItems": 32,
                                            "items": {"type": "string"},
                                        },
                                        "reason": {"type": "string", "maxLength": 1000},
                                    },
                                    "required": ["id"],
                                },
                            },
                            "status": {"type": "string", "enum": ["working", "waiting_user", "blocked"]},
                            "reason": {"type": "string", "maxLength": 1000},
                            "new_task": {"type": "boolean"},
                        },
                        "required": ["steps"],
                    },
                }
            ]
        )

    def get_tool(self, name):
        """Resolve the private planning tool before delegated tools."""
        return super().get_tool(name) or self.base.get_tool(name)

    def get_all_tools(self, *, visible_only=False):
        """See tools exposed by discovery during this turn immediately."""
        return [
            *[
                tool
                for tool in self.base.get_all_tools(visible_only=visible_only)
                if tool["name"] != "task_plan"
            ],
            *super().get_all_tools(visible_only=visible_only),
        ]
