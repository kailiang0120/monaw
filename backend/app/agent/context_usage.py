"""Context-window estimation for runtime requests."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from app.agent.llm_client import _normalise_messages_for_openai, _tools_to_openai

try:
    import tiktoken
except Exception:  # pragma: no cover - optional dependency fallback
    tiktoken = None


DEFAULT_CONTEXT_TOKEN_LIMIT = 200_000
DEFAULT_COMPACTION_RATIO = 0.925  # 185 000 / 200 000
_OPENAI_REPLY_PRIMER_TOKENS = 3
_OPENAI_MESSAGE_OVERHEAD_TOKENS = 3
_OPENAI_NAME_OVERHEAD_TOKENS = 1
_GENERIC_MESSAGE_OVERHEAD_TOKENS = 4
_TOOL_OVERHEAD_TOKENS = 12
_IMAGE_ATTACHMENT_TOKEN_FALLBACK = 850
_CHARS_PER_TOKEN_FALLBACK = 4
_OPENAI_CONTEXT_WINDOWS = {
    "gpt-5.5": 200_000,
    "gpt-5.4": 200_000,
    "gpt-5.4-mini": 200_000,
    "gpt-5.4-nano": 200_000,
}
_GEMINI_CONTEXT_WINDOWS = {
    "gemini-3.1-pro-preview": 200_000,
    "gemini-3.1-flash-lite": 200_000,
    "gemini-3.1-flash-lite-preview": 200_000,
    "gemini-3-flash-preview": 200_000,
}
_DEEPSEEK_CONTEXT_WINDOWS = {
    "deepseek-v4-flash": 200_000,
    "deepseek-v4-pro": 200_000,
}


def _provider_name(llm_client) -> str:
    return str(getattr(llm_client, "provider", "openai") or "openai").lower()


def _model_name(llm_client) -> str:
    return str(getattr(llm_client, "model_name", "gpt-5.4") or "gpt-5.4")


def _normalize_model_id(model_name: str) -> str:
    model = str(model_name or "").strip().lower()
    # Snapshot IDs normally suffix an ISO date; map them back to the stable alias.
    parts = model.rsplit("-", 3)
    if (
        len(parts) == 4
        and len(parts[-3]) == 4
        and len(parts[-2]) == 2
        and len(parts[-1]) == 2
        and all(part.isdigit() for part in parts[-3:])
    ):
        return parts[0]
    return model


def _lookup_context_window(model: str, windows: dict[str, int]) -> int:
    normalized = _normalize_model_id(model)
    if normalized in windows:
        return windows[normalized]
    for prefix, limit in sorted(windows.items(), key=lambda item: len(item[0]), reverse=True):
        if normalized.startswith(prefix):
            return limit
    return DEFAULT_CONTEXT_TOKEN_LIMIT


def model_context_token_limit(llm_client) -> int:
    provider = _provider_name(llm_client)
    model = _model_name(llm_client)
    if provider == "openai":
        return _lookup_context_window(model, _OPENAI_CONTEXT_WINDOWS)
    if provider == "deepseek":
        return _lookup_context_window(model, _DEEPSEEK_CONTEXT_WINDOWS)
    if provider == "gemini":
        return _lookup_context_window(model, _GEMINI_CONTEXT_WINDOWS)
    return DEFAULT_CONTEXT_TOKEN_LIMIT


def model_compaction_threshold(llm_client) -> int:
    return int(model_context_token_limit(llm_client) * DEFAULT_COMPACTION_RATIO)


def _encoding_name(provider: str, model_name: str) -> str:
    model = model_name.lower()
    if provider in {"openai", "deepseek"}:
        try:
            return tiktoken.encoding_for_model(model_name).name if tiktoken is not None else ""
        except Exception:
            if model.startswith(("gpt-5", "gpt-4.1", "gpt-4o", "o1", "o3", "o4")):
                return "o200k_base"
            return "cl100k_base"
    if provider == "gemini":
        return "o200k_base"
    return "cl100k_base"


@lru_cache(maxsize=16)
def _load_encoding(name: str):
    if tiktoken is None or not name:
        return None
    try:
        return tiktoken.get_encoding(name)
    except Exception:
        return None


def token_estimation_method(llm_client) -> str:
    encoding_name = _encoding_name(_provider_name(llm_client), _model_name(llm_client))
    encoding = _load_encoding(encoding_name)
    if encoding is not None:
        return f"tiktoken:{encoding.name}"
    return "chars/4 fallback"


def count_text_tokens(text: str, *, llm_client) -> int:
    content = str(text or "")
    if not content:
        return 0
    encoding = _load_encoding(_encoding_name(_provider_name(llm_client), _model_name(llm_client)))
    if encoding is not None:
        try:
            return len(encoding.encode(content))
        except Exception:
            pass
    return max(1, len(content) // _CHARS_PER_TOKEN_FALLBACK)


def _count_content_tokens(content: Any, *, llm_client) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return count_text_tokens(content, llm_client=llm_client)
    if isinstance(content, list):
        total = 0
        for item in content:
            if isinstance(item, dict):
                part_type = str(item.get("type") or "").lower()
                if part_type == "text":
                    total += count_text_tokens(str(item.get("text") or ""), llm_client=llm_client)
                elif part_type == "image_url":
                    total += _IMAGE_ATTACHMENT_TOKEN_FALLBACK
                else:
                    total += count_text_tokens(
                        json.dumps(item, ensure_ascii=False, sort_keys=True),
                        llm_client=llm_client,
                    )
            else:
                total += count_text_tokens(str(item), llm_client=llm_client)
        return total
    if isinstance(content, dict):
        return count_text_tokens(
            json.dumps(content, ensure_ascii=False, sort_keys=True),
            llm_client=llm_client,
        )
    return count_text_tokens(str(content), llm_client=llm_client)


def estimate_message_tokens(messages: list[dict], *, llm_client) -> int:
    provider = _provider_name(llm_client)
    if provider in {"openai", "deepseek"}:
        return _estimate_openai_message_tokens(messages, llm_client=llm_client)

    total = 2
    for message in messages:
        total += _GENERIC_MESSAGE_OVERHEAD_TOKENS
        total += count_text_tokens(str(message.get("role", "user")), llm_client=llm_client)
        total += _count_content_tokens(message.get("content", ""), llm_client=llm_client)
        images = message.get("images") if isinstance(message.get("images"), list) else []
        total += len(images) * _IMAGE_ATTACHMENT_TOKEN_FALLBACK
    return total


def _estimate_openai_message_tokens(messages: list[dict], *, llm_client) -> int:
    normalized = _normalise_messages_for_openai(
        messages,
        system_prompt="",
        include_images=bool(getattr(llm_client, "supports_vision", False)),
    )
    total = _OPENAI_REPLY_PRIMER_TOKENS
    for message in normalized:
        total += _OPENAI_MESSAGE_OVERHEAD_TOKENS
        for key, value in message.items():
            if key == "content":
                total += _count_content_tokens(value, llm_client=llm_client)
            elif key == "name":
                total += _OPENAI_NAME_OVERHEAD_TOKENS
                total += count_text_tokens(str(value), llm_client=llm_client)
            elif key == "tool_calls":
                total += count_text_tokens(
                    json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    llm_client=llm_client,
                )
            else:
                total += count_text_tokens(str(value), llm_client=llm_client)
    return total


def estimate_tool_schema_tokens(tools: list[dict], *, llm_client) -> int:
    provider = _provider_name(llm_client)
    normalized_tools: list[dict]
    if provider in {"openai", "deepseek"}:
        normalized_tools = _tools_to_openai(tools)
    else:
        normalized_tools = [
            {
                "function_declarations": [
                    {
                        "name": str(tool.get("name", "")),
                        "description": str(tool.get("description", "")),
                        "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                    }
                ]
            }
            for tool in tools
        ]

    total = 0
    for payload in normalized_tools:
        total += _TOOL_OVERHEAD_TOKENS
        total += count_text_tokens(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            llm_client=llm_client,
        )
    return total


def infer_context_query(memory, conversation_id: str, current_user_message: str = "") -> str:
    query = str(current_user_message or "").strip()
    if query:
        return query

    state = memory.get_or_create(conversation_id)
    goal = str(getattr(state, "task_goal", "") or "").strip()
    if goal:
        return goal

    history = list(getattr(state, "all_messages", []) or getattr(state, "recent_messages", []) or [])
    for message in reversed(history):
        if str(message.get("role", "")).lower() == "user":
            content = str(message.get("content", "") or "").strip()
            if content:
                return content

    return str(getattr(state, "title", "") or "").strip()


def _breakdown_item(
    *,
    key: str,
    label: str,
    tokens: int,
    limit: int,
    kind: str = "used",
    detail: str = "",
    count: int = 0,
) -> dict[str, Any]:
    percentage = round((tokens / limit) * 100, 1) if limit else 0.0
    return {
        "key": key,
        "label": label,
        "tokens": max(0, int(tokens)),
        "percentage": percentage,
        "kind": kind,
        "detail": detail,
        "count": max(0, int(count)),
    }


def build_context_usage_report(
    *,
    memory,
    llm_client,
    conversation_id: str,
    runtime_prompt_text: str = "",
    skill_prompt_text: str = "",
    long_term_context: str = "",
    visible_tools: list[dict] | None = None,
    hidden_tools: list[dict] | None = None,
    current_user_message: str = "",
    limit: int,
    compaction_at: int,
) -> dict[str, Any]:
    history_messages = list(memory.build_llm_messages(conversation_id))
    if current_user_message.strip():
        history_messages.append({"role": "user", "content": current_user_message})

    active_tools = list(visible_tools or [])
    deferred_tools = list(hidden_tools or [])
    mcp_tools = [tool for tool in active_tools if tool.get("mcp_bridge")]
    built_in_tools = [tool for tool in active_tools if not tool.get("mcp_bridge")]

    breakdown: list[dict[str, Any]] = []

    message_tokens = estimate_message_tokens(history_messages, llm_client=llm_client)
    if message_tokens:
        breakdown.append(
            _breakdown_item(
                key="messages",
                label="Conversation memory",
                tokens=message_tokens,
                limit=limit,
                detail=f"{len(history_messages)} message blocks prepared for the next turn",
                count=len(history_messages),
            )
        )

    runtime_tokens = count_text_tokens(runtime_prompt_text, llm_client=llm_client)
    if runtime_tokens:
        breakdown.append(
            _breakdown_item(
                key="runtime_prompt",
                label="Runtime rules",
                tokens=runtime_tokens,
                limit=limit,
                detail="Base runtime instructions, policies, identity, and completion protocol",
            )
        )

    skill_tokens = count_text_tokens(skill_prompt_text, llm_client=llm_client)
    if skill_tokens:
        breakdown.append(
            _breakdown_item(
                key="skills",
                label="Enabled skills",
                tokens=skill_tokens,
                limit=limit,
                detail="Capability checklist and loaded skill guidance",
            )
        )

    long_term_tokens = count_text_tokens(long_term_context, llm_client=llm_client)
    if long_term_tokens:
        breakdown.append(
            _breakdown_item(
                key="memory_retrieval",
                label="Memory retrieval",
                tokens=long_term_tokens,
                limit=limit,
                detail="Relevant long-term memories injected for the next turn",
            )
        )

    built_in_tool_tokens = estimate_tool_schema_tokens(built_in_tools, llm_client=llm_client)
    if built_in_tool_tokens:
        breakdown.append(
            _breakdown_item(
                key="builtin_tools",
                label="Built-in tools",
                tokens=built_in_tool_tokens,
                limit=limit,
                detail="Visible native tool schemas loaded into the model call",
                count=len(built_in_tools),
            )
        )

    mcp_tool_tokens = estimate_tool_schema_tokens(mcp_tools, llm_client=llm_client)
    if mcp_tool_tokens:
        breakdown.append(
            _breakdown_item(
                key="mcp_tools",
                label="MCP reflected tools",
                tokens=mcp_tool_tokens,
                limit=limit,
                detail="Visible MCP tool schemas reflected from connected servers",
                count=len(mcp_tools),
            )
        )

    deferred_tool_tokens = estimate_tool_schema_tokens(deferred_tools, llm_client=llm_client)
    if deferred_tool_tokens:
        breakdown.append(
            _breakdown_item(
                key="deferred_tools",
                label="Deferred compatibility tools",
                tokens=deferred_tool_tokens,
                limit=limit,
                detail="Hidden or compatibility-only tool schemas kept out of the default model call",
                count=len(deferred_tools),
            )
        )

    used = sum(item["tokens"] for item in breakdown)
    percentage = round((used / limit) * 100, 1) if limit else 0.0
    compaction_buffer_tokens = max(compaction_at - used, 0)
    free_tokens = max(limit - used, 0)

    breakdown.append(
        _breakdown_item(
            key="autocompact_buffer",
            label="Autocompact buffer",
            tokens=compaction_buffer_tokens,
            limit=limit,
            kind="reserved",
            detail="Remaining space before auto-compaction begins",
        )
    )
    breakdown.append(
        _breakdown_item(
            key="free_space",
            label="Free space",
            tokens=free_tokens,
            limit=limit,
            kind="free",
            detail="Remaining room before the hard context limit",
        )
    )

    return {
        "used": used,
        "limit": limit,
        "compaction_at": compaction_at,
        "percentage": percentage,
        "free_tokens": free_tokens,
        "compaction_buffer_tokens": compaction_buffer_tokens,
        "estimator": token_estimation_method(llm_client),
        "breakdown": breakdown,
        "notes": (
            "Counts the next provider request shape, including runtime prompt, skills, "
            "long-term memory, and normalized tool schemas."
        ),
    }

