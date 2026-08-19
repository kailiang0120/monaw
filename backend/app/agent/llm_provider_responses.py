"""Provider response parsing helpers for LLM chat calls."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from app.agent.observability.recorder import UsageStats

logger = logging.getLogger(__name__)


def obj_get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def obj_to_dict(obj: Any) -> dict:
    def convert(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items() if v is not None}
        if isinstance(value, list):
            return [convert(v) for v in value]
        if isinstance(value, tuple):
            return [convert(v) for v in value]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return convert(model_dump(exclude_none=True))
        if hasattr(value, "__dict__"):
            return {
                k: convert(v)
                for k, v in vars(value).items()
                if not k.startswith("_") and v is not None
            }
        return value

    if isinstance(obj, dict):
        return convert(obj)
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return convert(model_dump(exclude_none=True))
    if hasattr(obj, "__dict__"):
        return convert(obj)
    return {}


def usage_int(obj: Any, key: str) -> int:
    try:
        value = obj_get(obj, key, 0)
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def usage_nested_int(obj: Any, *path: str) -> int:
    current = obj
    for key in path:
        current = obj_get(current, key, None)
        if current is None:
            return 0
    try:
        return max(0, int(current or 0))
    except (TypeError, ValueError):
        return 0


def usage_from_openai(usage: Any) -> UsageStats:
    if not usage:
        return UsageStats()
    input_tokens = usage_int(usage, "input_tokens") or usage_int(usage, "prompt_tokens")
    output_tokens = usage_int(usage, "output_tokens") or usage_int(usage, "completion_tokens")
    total_tokens = usage_int(usage, "total_tokens")
    cached_tokens = (
        usage_nested_int(usage, "input_tokens_details", "cached_tokens")
        or usage_nested_int(usage, "prompt_tokens_details", "cached_tokens")
        or usage_int(usage, "prompt_cache_hit_tokens")
    )
    reasoning_tokens = (
        usage_nested_int(usage, "output_tokens_details", "reasoning_tokens")
        or usage_nested_int(usage, "completion_tokens_details", "reasoning_tokens")
    )
    return UsageStats(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens or input_tokens + output_tokens + reasoning_tokens,
        source="provider",
    ).normalized()


def usage_from_gemini_metadata(metadata: Any) -> UsageStats:
    if not metadata:
        return UsageStats()
    input_tokens = usage_int(metadata, "prompt_token_count")
    output_tokens = usage_int(metadata, "candidates_token_count")
    reasoning_tokens = usage_int(metadata, "thoughts_token_count")
    cached_tokens = usage_int(metadata, "cached_content_token_count")
    total_tokens = usage_int(metadata, "total_token_count")
    image_tokens = usage_int(metadata, "image_token_count")
    return UsageStats(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        image_tokens=image_tokens,
        total_tokens=total_tokens or input_tokens + output_tokens + reasoning_tokens + image_tokens,
        source="provider",
    ).normalized()


def usage_from_gemini_response(response: Any) -> UsageStats:
    return usage_from_gemini_metadata(
        obj_get(response, "usage_metadata", None) or obj_get(response, "usageMetadata", None)
    )


def extract_gemini_model_parts(response: Any) -> list:
    try:
        candidates = response.candidates or []
        if not candidates:
            return []
        content = candidates[0].content
        return list(content.parts or [])
    except Exception:
        return []


def split_gemini_text_parts(response: Any) -> tuple[str, str]:
    answer_parts: list[str] = []
    thought_parts: list[str] = []
    for part in extract_gemini_model_parts(response):
        text = str(getattr(part, "text", "") or "")
        if not text:
            continue
        if bool(getattr(part, "thought", False)):
            thought_parts.append(text)
        else:
            answer_parts.append(text)
    return "".join(answer_parts), "".join(thought_parts)


def extract_gemini_tool_calls(response: Any, tool_call_factory: Callable[..., Any]) -> list[Any]:
    if response is None:
        return []
    try:
        fcs = response.function_calls
    except Exception:
        return []
    if not fcs:
        return []

    result = []
    for i, fc in enumerate(fcs):
        try:
            name = fc.name or ""
            args = dict(fc.args) if fc.args else {}
            call_id = fc.id or f"gemini-call-{i}"
            result.append(tool_call_factory(call_id=call_id, tool_name=name, arguments=args))
        except Exception as exc:
            logger.warning("Failed to parse Gemini function_call: %s", exc)
    return result


def openai_response_output_items(response: Any) -> list[Any]:
    output = obj_get(response, "output", [])
    return list(output or []) if isinstance(output, (list, tuple)) else []


def openai_response_text_and_reasoning(response: Any) -> tuple[str, str]:
    text_parts: list[str] = []
    reasoning_parts: list[str] = []

    for item in openai_response_output_items(response):
        item_type = obj_get(item, "type", "")
        if item_type == "message":
            for part in list(obj_get(item, "content", []) or []):
                part_type = obj_get(part, "type", "")
                if part_type in {"output_text", "text"}:
                    text_parts.append(str(obj_get(part, "text", "") or ""))
        elif item_type == "reasoning":
            for summary in list(obj_get(item, "summary", []) or []):
                reasoning_parts.append(str(obj_get(summary, "text", "") or ""))
            for content in list(obj_get(item, "content", []) or []):
                reasoning_parts.append(str(obj_get(content, "text", "") or ""))

    if not text_parts:
        output_text = str(obj_get(response, "output_text", "") or "")
        if output_text:
            text_parts.append(output_text)

    return "".join(text_parts), "".join(reasoning_parts)


def extract_openai_response_tool_calls(response: Any, tool_call_factory: Callable[..., Any]) -> list[Any]:
    tool_calls: list[Any] = []
    for item in openai_response_output_items(response):
        if obj_get(item, "type", "") != "function_call":
            continue
        arguments_buf = str(obj_get(item, "arguments", "") or "")
        try:
            arguments = json.loads(arguments_buf) if arguments_buf else {}
        except json.JSONDecodeError:
            arguments = {"_raw": arguments_buf}
        tool_calls.append(
            tool_call_factory(
                call_id=str(obj_get(item, "call_id", "") or obj_get(item, "id", "") or ""),
                tool_name=str(obj_get(item, "name", "") or ""),
                arguments=arguments,
            )
        )
    return tool_calls


def openai_response_provider_messages(response: Any) -> list[dict]:
    messages: list[dict] = []
    for item in openai_response_output_items(response):
        item_type = obj_get(item, "type", "")
        if item_type in {"function_call", "reasoning"}:
            messages.append(obj_to_dict(item))
    return messages
