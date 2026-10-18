from __future__ import annotations

import json

from app.agent.conversation_workspace import selected_workspace_dir
from app.agent.database import get_db
from app.agent.memory_store import KINDS, MemoryWriteError, get_memory_store, grounded_in
from app.agent.run_context import current_execution_principal, current_interactive

_MAX_MEMORY_QUERY_CHARS = 500
_MAX_MEMORY_RETRIEVAL_BATCH = 12
_SEARCH_SCOPES = ["all", "global", "workspace"]
_WRITE_SCOPES = ["global", "workspace"]


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _blocked(reason_code: str, error: str) -> str:
    return _json({"status": "blocked", "reason_code": reason_code, "error": error})


def _current_user_request() -> str:
    """The user's message for the running turn (the turn loop stores it as the task goal)."""
    conversation_id = current_execution_principal().conversation_id
    if not conversation_id:
        return ""
    row = get_db().get_conversation(conversation_id) or {}
    return str(row.get("task_goal") or "")


def _write_guard(content: str | None = None) -> str | None:
    if not current_interactive():
        return _blocked("memory_mutation_non_interactive_restricted", "Memory changes require an interactive session.")
    if not get_memory_store().enabled:
        return _blocked("memory_disabled", "Memory is turned off in Settings.")
    # Text the agent chooses to remember may come from a web page or file it read.
    # Saved memories reach every future prompt, so only keep what the user said.
    if content is not None and not grounded_in(content, _current_user_request()):
        return _blocked(
            "memory_not_user_stated",
            "Only save what the user asked you to remember in this turn, phrased in their own words.",
        )
    return None


def _memory_search(query: str, scope: str = "all", limit: int = 6) -> str:
    bounded_query = str(query or "").strip()[:_MAX_MEMORY_QUERY_CHARS]
    bounded_limit = max(1, min(_MAX_MEMORY_RETRIEVAL_BATCH, int(limit)))
    memories = get_memory_store().search(
        bounded_query,
        workspace=selected_workspace_dir(),
        scope="" if scope == "all" else scope,
        limit=bounded_limit,
    )
    return _json({"status": "ok", "memories": memories, "count": len(memories)})


def _memory_remember(content: str, scope: str = "global", kind: str = "fact", importance: int = 5) -> str:
    blocked = _write_guard(content)
    if blocked:
        return blocked
    try:
        entry = get_memory_store().add(
            content,
            scope=scope,
            kind=kind,
            importance=importance,
            workspace=selected_workspace_dir(),
            source="user",
        )
    except MemoryWriteError as exc:
        return _json({"status": "error", "error": str(exc)})
    return _json({"status": "ok", "memory": entry.public()})


def _memory_update(id: str, content: str) -> str:
    blocked = _write_guard(content)
    if blocked:
        return blocked
    try:
        entry = get_memory_store().update(str(id), workspace=selected_workspace_dir(), content=content)
    except MemoryWriteError as exc:
        return _json({"status": "error", "error": str(exc)})
    if entry is None:
        return _json({"status": "error", "error": f"Memory {id} not found."})
    return _json({"status": "ok", "memory": entry.public()})


def _memory_forget(id: str) -> str:
    blocked = _write_guard()
    if blocked:
        return blocked
    if not get_memory_store().delete(str(id), workspace=selected_workspace_dir()):
        return _json({"status": "error", "error": f"Memory {id} not found."})
    return _json({"status": "ok", "deleted": True, "id": str(id)})


def register_tools(registry, _settings=None) -> None:
    read_metadata = {"parallel_safe": True, "resource_locks": [], "mutates_state": False, "risk_level": "low"}
    write_metadata = {"parallel_safe": False, "resource_locks": ["memory"], "mutates_state": True, "risk_level": "low"}
    registry.extend(
        [
            {
                "name": "memory_search",
                "description": (
                    "Search saved memories. Rules, preferences, and relevant facts are already in your context each turn; "
                    "search only when you need something that is not there. Returns ids for memory_update and memory_forget."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to look for."},
                        "scope": {
                            "type": "string",
                            "enum": _SEARCH_SCOPES,
                            "default": "all",
                            "description": "global = about the user; workspace = the current folder; all = both.",
                        },
                        "limit": {"type": "integer", "default": 6},
                    },
                    "required": ["query"],
                },
                "callable": _memory_search,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": read_metadata,
            },
            {
                "name": "memory_remember",
                "description": (
                    "Save something the user asked you to remember. Use scope 'global' for things about the user or that "
                    "apply in every folder, and 'workspace' for conventions, commands, and decisions that belong to the "
                    "current folder. Use kind 'rule' for how you must behave, 'preference' for likes and dislikes, and "
                    "'fact' for anything else. Phrase it as one short sentence in the user's own words."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "One short, self-contained sentence."},
                        "scope": {"type": "string", "enum": _WRITE_SCOPES, "default": "global"},
                        "kind": {"type": "string", "enum": list(KINDS), "default": "fact"},
                        "importance": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    },
                    "required": ["content", "scope", "kind"],
                },
                "callable": _memory_remember,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": write_metadata,
            },
            {
                "name": "memory_update",
                "description": "Correct a saved memory when the user changes or contradicts it. Find the id with memory_search.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Memory id."},
                        "content": {"type": "string", "description": "The corrected sentence, in the user's words."},
                    },
                    "required": ["id", "content"],
                },
                "callable": _memory_update,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": write_metadata,
            },
            {
                "name": "memory_forget",
                "description": "Delete a saved memory when the user asks you to forget it. Find the id with memory_search.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "description": "Memory id."}},
                    "required": ["id"],
                },
                "callable": _memory_forget,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": write_metadata,
            },
        ]
    )
