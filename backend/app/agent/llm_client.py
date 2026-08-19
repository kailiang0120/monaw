"""Unified async LLM client supporting Gemini and OpenAI with native function calling.

Uses:
- google-genai >= 1.0  (from google import genai)
- openai >= 1.0        (from openai import AsyncOpenAI)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.agent.harness.tool_protocol import ToolCallResult
from app.agent import llm_provider_requests as provider_requests
from app.agent import llm_provider_responses as provider_responses
from app.agent.llm_provider_adapters import create_llm_provider_adapter
from app.agent.observability.recorder import UsageStats

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ToolCallRequest:
    """A single tool-call request returned by the LLM."""

    call_id: str
    tool_name: str
    arguments: dict


@dataclass
class LLMResponse:
    """Normalised response from either provider."""

    content: str
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"  # "stop" | "tool_calls" | "length"
    reasoning_content: str = ""
    provider_messages: list[dict] = field(default_factory=list)
    usage: UsageStats = field(default_factory=UsageStats)


# ---------------------------------------------------------------------------
# Retry helper — delegates to the centralized retry module
# ---------------------------------------------------------------------------

from app.agent.retry import retry_with_backoff, _default_retryable


async def _with_retry(coro_factory: Callable[[], Awaitable]):
    return await retry_with_backoff(
        coro_factory,
        max_attempts=4,
        is_retryable=_default_retryable,
        base_delay=1.0,
        max_delay=30.0,
    )


# ---------------------------------------------------------------------------
# Tool format converters
# ---------------------------------------------------------------------------


def _json_schema_prop_to_gemini(prop_def: dict, types_mod):
    """Map a JSON Schema property (Pydantic-style) to google.genai.types.Schema."""
    T = types_mod.Type

    desc = str(prop_def.get("description") or prop_def.get("title") or "")

    if "anyOf" in prop_def:
        variants = prop_def["anyOf"]
        non_null = [v for v in variants if isinstance(v, dict) and v.get("type") != "null"]
        nullable = any(isinstance(v, dict) and v.get("type") == "null" for v in variants)
        if len(non_null) == 1:
            inner = _json_schema_prop_to_gemini(non_null[0], types_mod)
            if nullable:
                return inner.model_copy(update={"nullable": True})
            return inner
        if non_null:
            return _json_schema_prop_to_gemini(non_null[0], types_mod)
        return types_mod.Schema(type=T.STRING, description=desc)

    ptype = prop_def.get("type")
    if isinstance(ptype, list):
        ptype = next((x for x in ptype if x and x != "null"), "string")

    if ptype == "string":
        kw: dict = {"type": T.STRING, "description": desc}
        if "enum" in prop_def and prop_def["enum"]:
            enum_values = [str(e) for e in prop_def["enum"] if str(e)]
            if enum_values:
                kw["enum"] = enum_values
        return types_mod.Schema(**kw)
    if ptype == "number":
        return types_mod.Schema(type=T.NUMBER, description=desc)
    if ptype == "integer":
        return types_mod.Schema(type=T.INTEGER, description=desc)
    if ptype == "boolean":
        return types_mod.Schema(type=T.BOOLEAN, description=desc)
    if ptype == "array":
        items_src = prop_def.get("items") if isinstance(prop_def.get("items"), dict) else None
        items_schema = _json_schema_prop_to_gemini(items_src or {"type": "string"}, types_mod)
        return types_mod.Schema(type=T.ARRAY, description=desc, items=items_schema)
    if ptype == "object":
        raw_props = prop_def.get("properties")
        properties: dict = {}
        if isinstance(raw_props, dict):
            for name, sub in raw_props.items():
                if isinstance(sub, dict):
                    properties[name] = _json_schema_prop_to_gemini(sub, types_mod)
        req = prop_def.get("required")
        required_list = req if isinstance(req, list) else None
        # Gemini function declaration schema rejects additional_properties in this context.
        # For free-form dict inputs, keep a plain object node and rely on runtime validation.
        return types_mod.Schema(
            type=T.OBJECT,
            description=desc,
            properties=properties or None,
            required=required_list,
        )

    return types_mod.Schema(type=T.STRING, description=desc)


def _tools_to_gemini(tools: list[dict]):
    """Convert tool dicts to a google.genai types.Tool object."""
    from google.genai import types

    declarations = []
    for t in tools:
        params = t.get("parameters", {})
        props_raw = params.get("properties", {})

        properties: dict = {}
        for prop_name, prop_def in props_raw.items():
            if isinstance(prop_def, dict):
                properties[prop_name] = _json_schema_prop_to_gemini(prop_def, types)
            else:
                properties[prop_name] = types.Schema(
                    type=types.Type.STRING,
                    description="",
                )

        declarations.append(
            types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties=properties,
                    required=params.get("required", []),
                ),
            )
        )

    return types.Tool(function_declarations=declarations)


def _tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert tool dicts to OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


def _tools_to_openai_responses(tools: list[dict]) -> list[dict]:
    """Convert tool dicts to OpenAI Responses API function tools."""
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            "strict": False,
        }
        for t in tools
    ]


def _openai_reasoning_effort(value: str) -> str:
    effort = str(value or "").strip().lower()
    if effort in {"none", "minimal"}:
        return "minimal"
    if effort in {"low", "medium", "high"}:
        return effort
    if effort in {"xhigh", "max"}:
        return "high"
    return "medium"


def _openai_model_supports_reasoning_config(model_name: str) -> bool:
    model = str(model_name or "").strip().lower()
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def _gemini_thinking_config(model_name: str, reasoning_effort: str, types_mod):
    effort = str(reasoning_effort or "medium").strip().lower()
    model = str(model_name or "").strip().lower()
    if not model.startswith("gemini-"):
        return None

    if model.startswith("gemini-3"):
        if "pro" in model:
            level_by_effort = {
                "none": "low",
                "minimal": "low",
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "high",
                "max": "high",
            }
            level = level_by_effort.get(effort, "high")
        else:
            level_by_effort = {
                "none": "minimal",
                "minimal": "minimal",
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "high",
                "max": "high",
            }
            level = level_by_effort.get(effort, "medium")
        return types_mod.ThinkingConfig(thinking_level=level, include_thoughts=True)

    if model.startswith("gemini-2.5"):
        if "pro" in model:
            budget_by_effort = {
                "none": 128,
                "low": 128,
                "medium": -1,
                "high": 32768,
                "xhigh": 32768,
                "max": 32768,
            }
        else:
            budget_by_effort = {
                "none": 0,
                "low": 1024,
                "medium": -1,
                "high": 24576,
                "xhigh": 24576,
                "max": 24576,
            }
        return types_mod.ThinkingConfig(
            thinking_budget=budget_by_effort.get(effort, -1),
            include_thoughts=True,
        )

    return None


def _tool_output_response_payload(output: str) -> dict:
    try:
        parsed = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return {"output": str(output or "")}
    return parsed if isinstance(parsed, dict) else {"output": parsed}


def build_tool_result_message(result: ToolCallResult, *, provider: str = "") -> dict:
    if str(provider or "").lower() == "openai":
        return {
            "type": "function_call_output",
            "call_id": result.call_id,
            "output": result.output,
        }

    message = {
        "role": "user",
        "content": f"Tool result for {result.name}: {result.output}",
    }
    if str(provider or "").lower() == "gemini":
        message["gemini_function_response"] = {
            "id": result.call_id,
            "name": result.name,
            "response": _tool_output_response_payload(result.output),
        }
    return message


# ---------------------------------------------------------------------------
# Message normalisation
# ---------------------------------------------------------------------------


def _model_supports_vision(provider: str, model_name: str) -> bool:
    provider_name = provider.lower()
    model = model_name.lower()
    if provider_name == "gemini":
        return model.startswith("gemini")
    if provider_name == "openai":
        return model.startswith(("gpt-4", "gpt-5", "o1", "o3", "o4"))
    return True


def _image_payloads_for_message(msg: dict, include_images: bool) -> list[dict]:
    if not include_images:
        return []
    raw_images = msg.get("images") or []
    if not isinstance(raw_images, list):
        return []

    payloads: list[dict] = []
    for image in raw_images:
        if not isinstance(image, dict):
            continue
        path = str(image.get("path") or "").strip()
        if not path:
            continue
        try:
            data = Path(path).read_bytes()
        except OSError:
            logger.warning("Skipping missing image attachment: %s", path)
            continue
        mime_type = str(image.get("mime_type") or "").strip()
        if not mime_type:
            mime_type = mimetypes.guess_type(path)[0] or "image/png"
        payloads.append({"path": path, "mime_type": mime_type, "data": data})
    return payloads


def _normalise_messages_for_gemini(
    messages: list[dict],
    system_prompt: str,
    include_images: bool = False,
) -> list[dict]:
    """
    Map OpenAI-style messages to Gemini's user/model role format.
    system_prompt is passed via GenerateContentConfig, not injected here.
    """
    result: list[dict] = []
    from google.genai import types

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        image_payloads = _image_payloads_for_message(msg, include_images)

        gemini_parts = msg.get("gemini_parts")
        if isinstance(gemini_parts, list):
            gemini_role = "model" if role in {"assistant", "model"} else "user"
            result.append({"role": gemini_role, "parts": gemini_parts or [{"text": ""}]})
            continue

        function_response = msg.get("gemini_function_response")
        if isinstance(function_response, dict):
            response_payload = function_response.get("response")
            if not isinstance(response_payload, dict):
                response_payload = {"result": response_payload}
            function_response_kwargs: dict[str, Any] = {
                "name": str(function_response.get("name") or ""),
                "response": response_payload,
            }
            call_id = str(function_response.get("id") or "").strip()
            if call_id:
                function_response_kwargs["id"] = call_id
            result.append(
                {
                    "role": "user",
                    "parts": [
                        types.Part(
                            function_response=types.FunctionResponse(**function_response_kwargs)
                        )
                    ],
                }
            )
            continue

        if role == "system":
            # system messages are handled via config.system_instruction
            continue

        if role in ("user", "human"):
            parts = [{"text": str(content)}] if content else []
            parts.extend(
                types.Part.from_bytes(data=payload["data"], mime_type=payload["mime_type"])
                for payload in image_payloads
            )
            result.append({"role": "user", "parts": parts or [{"text": ""}]})
        elif role in ("assistant", "model"):
            result.append({"role": "model", "parts": [{"text": content}]})
        # other roles silently skipped

    # Gemini requires the conversation to start with a user turn
    if not result or result[0]["role"] != "user":
        result.insert(0, {"role": "user", "parts": [{"text": "Hello"}]})

    return result


def _normalise_messages_for_openai(
    messages: list[dict],
    system_prompt: str,
    include_images: bool = False,
) -> list[dict]:
    result: list[dict] = []
    if system_prompt:
        result.append({"role": "system", "content": system_prompt})
    for msg in messages:
        role = msg.get("role", "user")
        if role == "model":
            role = "assistant"
        content = msg.get("content", "")
        image_payloads = _image_payloads_for_message(msg, include_images and role == "user")
        if image_payloads:
            content_parts: list[dict] = [{"type": "text", "text": str(content)}]
            for payload in image_payloads:
                encoded = base64.b64encode(payload["data"]).decode("ascii")
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{payload['mime_type']};base64,{encoded}",
                        },
                    }
                )
            result.append({"role": role, "content": content_parts})
        else:
            result.append({"role": role, "content": content})
    return result


def _normalise_messages_for_openai_responses(
    messages: list[dict],
    include_images: bool = False,
) -> list[dict]:
    result: list[dict] = []
    for msg in messages:
        item_type = msg.get("type")
        if item_type in {"function_call", "function_call_output", "reasoning"}:
            result.append(dict(msg))
            continue

        role = msg.get("role", "user")
        if role == "model":
            role = "assistant"
        if role == "system":
            continue

        content = msg.get("content", "")
        image_payloads = _image_payloads_for_message(msg, include_images and role == "user")
        if image_payloads:
            content_parts: list[dict] = [{"type": "input_text", "text": str(content)}]
            for payload in image_payloads:
                encoded = base64.b64encode(payload["data"]).decode("ascii")
                content_parts.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{payload['mime_type']};base64,{encoded}",
                    }
                )
            result.append({"role": role, "content": content_parts})
        else:
            result.append({"role": role, "content": str(content)})
    return result


def _extract_gemini_model_parts(response) -> list:
    return provider_responses.extract_gemini_model_parts(response)


def _split_gemini_text_parts(response) -> tuple[str, str]:
    return provider_responses.split_gemini_text_parts(response)


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    return provider_responses.obj_get(obj, key, default)


def _obj_to_dict(obj: Any) -> dict:
    return provider_responses.obj_to_dict(obj)


def _usage_int(obj: Any, key: str) -> int:
    return provider_responses.usage_int(obj, key)


def _usage_nested_int(obj: Any, *path: str) -> int:
    return provider_responses.usage_nested_int(obj, *path)


def _usage_from_openai(usage: Any) -> UsageStats:
    return provider_responses.usage_from_openai(usage)


def _usage_from_gemini_metadata(metadata: Any) -> UsageStats:
    return provider_responses.usage_from_gemini_metadata(metadata)


def _usage_from_gemini_response(response: Any) -> UsageStats:
    return provider_responses.usage_from_gemini_response(response)


def _openai_response_output_items(response: Any) -> list[Any]:
    return provider_responses.openai_response_output_items(response)


def _openai_response_text_and_reasoning(response: Any) -> tuple[str, str]:
    return provider_responses.openai_response_text_and_reasoning(response)


def _extract_openai_response_tool_calls(response: Any) -> list[ToolCallRequest]:
    return provider_responses.extract_openai_response_tool_calls(response, ToolCallRequest)


def _openai_response_provider_messages(response: Any) -> list[dict]:
    return provider_responses.openai_response_provider_messages(response)


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------


class LLMClient:
    """Async LLM client supporting Gemini and OpenAI-compatible providers."""

    def __init__(
        self,
        provider: str,
        model_name: str,
        api_key: str,
        *,
        base_url: str = "",
        reasoning_effort: str = "medium",
    ) -> None:
        self.provider = provider.lower()
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url.strip()
        self.reasoning_effort = reasoning_effort
        self._openai_client = None
        self._genai_client = None
        self._supports_vision = _model_supports_vision(self.provider, self.model_name)

        if self.provider == "gemini":
            from google import genai
            self._genai_client = genai.Client(api_key=api_key) if api_key else None
        elif self.provider == "openai":
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(api_key=api_key)
        else:
            raise ValueError(f"Unsupported provider: {provider!r}. Use 'openai' or 'gemini'.")
        self.provider_adapter = create_llm_provider_adapter(self.provider, self)

    @staticmethod
    def build_tool_result_message(result: ToolCallResult) -> dict:
        return build_tool_result_message(result)

    def build_provider_tool_result_message(self, result: ToolCallResult) -> dict:
        return build_tool_result_message(result, provider=self.provider)

    @property
    def supports_vision(self) -> bool:
        return self._supports_vision

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str = "",
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        return await _with_retry(
            lambda: self.provider_adapter.chat_with_tools(
                messages,
                tools,
                system_prompt,
                stream_callback,
                tool_choice,
            )
        )

    async def chat(
        self,
        messages: list[dict],
        system_prompt: str = "",
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        resp = await self.chat_with_tools(
            messages=messages,
            tools=[],
            system_prompt=system_prompt,
            stream_callback=stream_callback,
        )
        return resp.content

    # ------------------------------------------------------------------
    # Gemini implementation (google-genai SDK)
    # ------------------------------------------------------------------

    async def _gemini_chat(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str,
        stream_callback: Callable[[str], Awaitable[None]] | None,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        gemini_messages, config = provider_requests.gemini_request_payload(
            model_name=self.model_name,
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            reasoning_effort=self.reasoning_effort,
            include_images=self._supports_vision,
            tool_choice=tool_choice,
        )

        if stream_callback is not None:
            return await self._gemini_stream(gemini_messages, config, stream_callback)
        else:
            return await self._gemini_no_stream(gemini_messages, config)

    async def _gemini_no_stream(self, contents, config) -> LLMResponse:
        if not self._genai_client:
            raise ValueError("Gemini provider selected but no Google API key is configured.")
        kwargs = {"model": self.model_name, "contents": contents}
        if config is not None:
            kwargs["config"] = config

        response = await self._genai_client.aio.models.generate_content(**kwargs)

        text, reasoning_content = _split_gemini_text_parts(response)
        if not text:
            try:
                text = response.text or ""
            except Exception:
                pass

        tool_calls = _extract_gemini_tool_calls(response)
        finish_reason = "tool_calls" if tool_calls else "stop"
        model_parts = _extract_gemini_model_parts(response) if tool_calls else []
        provider_messages = (
            [{"role": "model", "gemini_parts": model_parts}]
            if model_parts
            else []
        )
        return LLMResponse(
            content=text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning_content=reasoning_content,
            provider_messages=provider_messages,
            usage=_usage_from_gemini_response(response),
        )

    async def _gemini_stream(self, contents, config, stream_callback) -> LLMResponse:
        if not self._genai_client:
            raise ValueError("Gemini provider selected but no Google API key is configured.")
        kwargs = {"model": self.model_name, "contents": contents}
        if config is not None:
            kwargs["config"] = config

        collected_text = ""
        # Collect function calls from ALL chunks — they appear in the FIRST chunk,
        # not the last, so we can't rely on last_response.
        raw_function_calls = []
        raw_model_parts = []
        collected_reasoning_content = ""
        usage = UsageStats()

        async for chunk in await self._genai_client.aio.models.generate_content_stream(**kwargs):
            chunk_usage = _usage_from_gemini_response(chunk)
            if chunk_usage.total_tokens:
                usage = chunk_usage
            chunk_parts = _extract_gemini_model_parts(chunk)
            if chunk_parts:
                raw_model_parts.extend(chunk_parts)
            # Collect function calls
            if chunk.function_calls:
                raw_function_calls.extend(chunk.function_calls)
            # Collect text independently — a chunk can contain both text and function calls
            text_part, thought_part = _split_gemini_text_parts(chunk)
            if not text_part and not thought_part:
                try:
                    text_part = chunk.text or ""
                except Exception:
                    text_part = ""
            if thought_part:
                collected_reasoning_content += thought_part
            if text_part:
                collected_text += text_part
                await stream_callback(text_part)

        # Convert raw FunctionCall objects to ToolCallRequest
        tool_calls: list[ToolCallRequest] = []
        for i, fc in enumerate(raw_function_calls):
            try:
                args = dict(fc.args) if fc.args else {}
                call_id = fc.id or f"gemini-call-{i}"
                tool_calls.append(
                    ToolCallRequest(call_id=call_id, tool_name=fc.name or "", arguments=args)
                )
            except Exception as exc:
                logger.warning("Failed to parse streamed Gemini function_call: %s", exc)

        finish_reason = "tool_calls" if tool_calls else "stop"
        provider_messages = (
            [{"role": "model", "gemini_parts": raw_model_parts}]
            if tool_calls and raw_model_parts
            else []
        )
        return LLMResponse(
            content=collected_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning_content=collected_reasoning_content,
            provider_messages=provider_messages,
            usage=usage,
        )

    # ------------------------------------------------------------------
    # OpenAI implementation
    # ------------------------------------------------------------------

    async def _openai_chat(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str,
        stream_callback: Callable[[str], Awaitable[None]] | None,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        return await self._openai_responses_chat(
            messages,
            tools,
            system_prompt,
            stream_callback,
            tool_choice,
        )

    async def _openai_responses_chat(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str,
        stream_callback: Callable[[str], Awaitable[None]] | None,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        kwargs = provider_requests.openai_responses_kwargs(
            model_name=self.model_name,
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            reasoning_effort=self.reasoning_effort,
            include_images=self._supports_vision,
            tool_choice=tool_choice,
        )

        if stream_callback is not None:
            return await self._openai_responses_stream(kwargs, stream_callback)
        return await self._openai_responses_no_stream(kwargs)

    async def _openai_responses_no_stream(self, kwargs: dict) -> LLMResponse:
        response = await self._openai_client.responses.create(**kwargs)
        content, reasoning_content = _openai_response_text_and_reasoning(response)
        tool_calls = _extract_openai_response_tool_calls(response)
        finish_reason = "tool_calls" if tool_calls else "stop"
        status = str(_obj_get(response, "status", "") or "")
        incomplete_details = _obj_get(response, "incomplete_details", None)
        if status == "incomplete" and _obj_get(incomplete_details, "reason", "") == "max_output_tokens":
            finish_reason = "length"
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning_content=reasoning_content,
            provider_messages=_openai_response_provider_messages(response),
            usage=_usage_from_openai(_obj_get(response, "usage", None)),
        )

    async def _openai_responses_stream(
        self,
        kwargs: dict,
        stream_callback: Callable[[str], Awaitable[None]],
    ) -> LLMResponse:
        stream = await self._openai_client.responses.create(**{**kwargs, "stream": True})
        collected_content = ""
        collected_reasoning_content = ""
        final_response = None
        tool_call_buffers: dict[int, dict] = {}

        async for event in stream:
            event_type = str(_obj_get(event, "type", "") or "")
            if event_type == "response.output_text.delta":
                delta = str(_obj_get(event, "delta", "") or "")
                if delta:
                    collected_content += delta
                    await stream_callback(delta)
            elif event_type in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
                collected_reasoning_content += str(_obj_get(event, "delta", "") or "")
            elif event_type == "response.output_item.added":
                item = _obj_get(event, "item", None)
                if _obj_get(item, "type", "") == "function_call":
                    output_index = int(_obj_get(event, "output_index", len(tool_call_buffers)) or 0)
                    tool_call_buffers[output_index] = {
                        "id": str(_obj_get(item, "id", "") or ""),
                        "call_id": str(_obj_get(item, "call_id", "") or ""),
                        "name": str(_obj_get(item, "name", "") or ""),
                        "arguments_buf": str(_obj_get(item, "arguments", "") or ""),
                    }
            elif event_type == "response.function_call_arguments.delta":
                output_index = int(_obj_get(event, "output_index", 0) or 0)
                buf = tool_call_buffers.setdefault(
                    output_index,
                    {"id": "", "call_id": "", "name": "", "arguments_buf": ""},
                )
                buf["arguments_buf"] += str(_obj_get(event, "delta", "") or "")
            elif event_type == "response.function_call_arguments.done":
                output_index = int(_obj_get(event, "output_index", 0) or 0)
                buf = tool_call_buffers.setdefault(
                    output_index,
                    {"id": "", "call_id": "", "name": "", "arguments_buf": ""},
                )
                buf["name"] = str(_obj_get(event, "name", "") or buf["name"])
                buf["arguments_buf"] = str(_obj_get(event, "arguments", "") or buf["arguments_buf"])
            elif event_type == "response.completed":
                final_response = _obj_get(event, "response", None)

        if final_response is not None:
            final_content, final_reasoning = _openai_response_text_and_reasoning(final_response)
            return LLMResponse(
                content=final_content or collected_content,
                tool_calls=_extract_openai_response_tool_calls(final_response),
                finish_reason=(
                    "tool_calls"
                    if _extract_openai_response_tool_calls(final_response)
                    else "stop"
                ),
                reasoning_content=final_reasoning or collected_reasoning_content,
                provider_messages=_openai_response_provider_messages(final_response),
                usage=_usage_from_openai(_obj_get(final_response, "usage", None)),
            )

        tool_calls: list[ToolCallRequest] = []
        for index in sorted(tool_call_buffers):
            buf = tool_call_buffers[index]
            try:
                arguments = json.loads(buf["arguments_buf"]) if buf["arguments_buf"] else {}
            except json.JSONDecodeError:
                arguments = {"_raw": buf["arguments_buf"]}
            tool_calls.append(
                ToolCallRequest(
                    call_id=buf["call_id"] or buf["id"],
                    tool_name=buf["name"],
                    arguments=arguments,
                )
            )
        return LLMResponse(
            content=collected_content,
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            reasoning_content=collected_reasoning_content,
        )


# ---------------------------------------------------------------------------
# Helper extractors
# ---------------------------------------------------------------------------


def _extract_gemini_tool_calls(response) -> list[ToolCallRequest]:
    """Extract function calls from a google-genai GenerateContentResponse."""
    return provider_responses.extract_gemini_tool_calls(response, ToolCallRequest)
