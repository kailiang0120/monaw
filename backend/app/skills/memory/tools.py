from __future__ import annotations

import json

from app.agent.long_term_memory import get_long_term_memory
from app.agent.run_context import current_execution_principal, current_interactive

_MAX_MEMORY_QUERY_CHARS = 500
_MAX_MEMORY_RETRIEVAL_BATCH = 12


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _bounded_memory_query(query: str) -> tuple[str, bool]:
    value = str(query or "").strip()
    if len(value) <= _MAX_MEMORY_QUERY_CHARS:
        return value, False
    return value[:_MAX_MEMORY_QUERY_CHARS], True


def _bounded_memory_limit(limit: int) -> int:
    return max(1, min(_MAX_MEMORY_RETRIEVAL_BATCH, int(limit)))


def _memory_search(query: str, category: str = "", limit: int = 6) -> str:
    store = get_long_term_memory()
    bounded_query, query_truncated = _bounded_memory_query(query)
    bounded_limit = _bounded_memory_limit(limit)
    memories = store.search(
        bounded_query,
        category=category,
        limit=bounded_limit,
        mark_used=False,
    )
    return _json({
        "status": "ok",
        "memories": memories,
        "count": len(memories),
        "budget": {
            "query_chars": len(bounded_query),
            "query_truncated": query_truncated,
            "limit": bounded_limit,
            "max_limit": _MAX_MEMORY_RETRIEVAL_BATCH,
        },
    })


def _recall_memory(query: str, category: str = "", limit: int = 6) -> str:
    store = get_long_term_memory()
    payload = _memory_search(query=query, category=category, limit=limit)
    store._audit(
        action="RECALL",
        reason="recall_memory tool called",
        candidate_content=json.dumps(
            {
                "recall_memory_called": True,
                "query": _bounded_memory_query(query)[0][:200],
                "category": str(category or ""),
                "limit": _bounded_memory_limit(limit),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    return payload


def _memory_get(id: str) -> str:
    memory = get_long_term_memory().get(str(id))
    if memory is None:
        return _json({"status": "error", "error": f"Memory {id} not found."})
    return _json({"status": "ok", "memory": memory})


def _memory_remember(content: str, category: str = "fact") -> str:
    if not current_interactive():
        return _json({"status": "blocked", "reason_code": "memory_mutation_non_interactive_restricted", "error": "Memory writes require an interactive session."})
    principal = current_execution_principal()
    memory = get_long_term_memory().remember(
        content,
        category=category,
        confidence=1.0,
        review_state="reviewed",
        source="tool",
        source_conversation_id=principal.conversation_id,
        source_principal_id=principal.principal_id,
        source_permission_profile_id=principal.permission_profile_id,
        source_execution_source=principal.source,
    )
    if memory is None:
        return _json({"status": "error", "error": "Memory was rejected by safety filters."})
    return _json({"status": "ok", "memory": memory})


def _memory_forget(id: str) -> str:
    if not current_interactive():
        return _json({"status": "blocked", "reason_code": "memory_mutation_non_interactive_restricted", "error": "Memory deletes require an interactive session."})
    memory = get_long_term_memory().archive(str(id))
    if memory is None:
        return _json({"status": "error", "error": f"Memory {id} not found."})
    return _json({"status": "ok", "memory": memory})


def _memory_delete(id: str) -> str:
    if not current_interactive():
        return _json({"status": "blocked", "reason_code": "memory_mutation_non_interactive_restricted", "error": "Permanent memory deletion requires an interactive session."})
    deleted = get_long_term_memory().delete(str(id))
    if not deleted:
        return _json({"status": "error", "error": f"Memory {id} not found."})
    return _json({"status": "ok", "deleted": True, "id": str(id)})


def _memory_curate_session(conversation_id: str) -> str:
    if not current_interactive():
        return _json({"status": "blocked", "reason_code": "memory_mutation_non_interactive_restricted", "error": "Memory curation requires an interactive session."})
    result = get_long_term_memory().reconcile_session(str(conversation_id))
    return _json({"status": "ok", "conversation_id": conversation_id, "result": result})


def register_tools(registry, _settings=None) -> None:
    searchable_categories = ["", "preference", "behavior", "fact", "workflow", "project", "reflection"]
    writable_categories = ["preference", "behavior", "fact", "workflow", "project", "reflection"]
    registry.extend(
        [
            {
                "name": "memory_search",
                "description": "Search durable long-term user memories by natural-language query.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query."},
                        "category": {
                            "type": "string",
                            "enum": searchable_categories,
                            "default": "",
                        },
                        "limit": {"type": "integer", "default": 6},
                    },
                    "required": ["query"],
                },
                "callable": _memory_search,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {"parallel_safe": True, "resource_locks": [], "mutates_state": False, "risk_level": "low"},
            },
            {
                "name": "recall_memory",
                "description": (
                    "Recall durable long-term user memories on demand. Call this when the current task "
                    "requires user context that is not already in the injected memory block, such as project "
                    "specifics, past decisions, or preferences not covered by the identity tier. Skip it if "
                    "the injected memories already answer the question."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural-language memory query."},
                        "category": {
                            "type": "string",
                            "enum": searchable_categories,
                            "default": "",
                        },
                        "limit": {"type": "integer", "default": 6},
                    },
                    "required": ["query"],
                },
                "callable": _recall_memory,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {"parallel_safe": True, "resource_locks": [], "mutates_state": False, "risk_level": "low"},
            },
            {
                "name": "memory_get",
                "description": "Get a durable Markdown-backed long-term memory by id.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "description": "Memory id."}},
                    "required": ["id"],
                },
                "callable": _memory_get,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {"parallel_safe": True, "resource_locks": [], "mutates_state": False, "risk_level": "low"},
            },
            {
                "name": "memory_remember",
                "description": "Save an explicit durable user memory. Use only when the user asks to remember something or gives a stable preference. If you mention the save afterward, keep it brief and neutral.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Self-contained memory content."},
                        "category": {
                            "type": "string",
                            "enum": writable_categories,
                            "default": "fact",
                        },
                    },
                    "required": ["content"],
                },
                "callable": _memory_remember,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {"parallel_safe": False, "resource_locks": ["long_term_memory"], "mutates_state": True, "risk_level": "low"},
            },
            {
                "name": "memory_forget",
                "description": "Archive a durable Markdown-backed user memory by id so it is no longer retrieved. If you confirm the change, keep it brief and neutral.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "description": "Memory id to archive."}},
                    "required": ["id"],
                },
                "callable": _memory_forget,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {"parallel_safe": False, "resource_locks": ["long_term_memory"], "mutates_state": True, "risk_level": "low"},
            },
            {
                "name": "memory_delete",
                "description": "Permanently delete a durable user memory by id. Use only when the user explicitly asks to delete it permanently.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "description": "Memory id to permanently delete."}},
                    "required": ["id"],
                },
                "callable": _memory_delete,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {"parallel_safe": False, "resource_locks": ["long_term_memory"], "mutates_state": True, "risk_level": "medium"},
            },
            {
                "name": "memory_curate_session",
                "description": "Run the Markdown memory curator for a completed conversation session. Use only for manual debugging or explicit user requests.",
                "parameters": {
                    "type": "object",
                    "properties": {"conversation_id": {"type": "string", "description": "Conversation id to curate."}},
                    "required": ["conversation_id"],
                },
                "callable": _memory_curate_session,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {"parallel_safe": False, "resource_locks": ["long_term_memory"], "mutates_state": True, "risk_level": "low"},
            },
        ]
    )
