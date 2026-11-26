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
from app.agent.model_usage import current_model_budget, current_output_limit, estimate_request_tokens, estimated_usage, settle_estimated_usage
from app.agent.model_capabilities import resolve_model_capabilities

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
    finish_reason: str = "stop"  # stop | tool_calls | length | blocked | incomplete
    reasoning_content: str = ""
    provider_messages: list[dict] = field(default_factory=list)
    usage: UsageStats = field(default_factory=UsageStats)
    provider_finish_reason: str = ""


class ResponseFailedError(RuntimeError):
    """A streamed Responses API call that ended with response.failed."""

    def __init__(self, message: str, *, code: str = "", param: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.param = param


class ResponseInterruptedError(RuntimeError):
    """An unfinished response must not dispatch tools or restart visible output."""

    retryable = False


def _stateless_input(items: list[dict]) -> list[dict]:
    """Responses input for store=false: nothing can be looked up by id.

    Reasoning saved without encrypted content (from the OpenAI API provider)
    cannot be replayed, so it is dropped; other items are sent without ids.
    """
    result = []
    for item in items:
        if item.get("type") == "reasoning" and not item.get("encrypted_content"):
            continue
        if item.get("type") in {"reasoning", "function_call"}:
            item = {key: value for key, value in item.items() if key not in {"id", "status"}}
        result.append(item)
    return result


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
    """Delegate the legacy schema helper to the provider implementation."""
    return provider_requests.json_schema_prop_to_gemini(prop_def, types_mod)


def _tools_to_gemini(tools: list[dict]):
    """Use the same schema conversion as actual Gemini requests."""
    return provider_requests.tools_to_gemini(tools)


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


def _openai_reasoning_effort(value: str, model_name: str = "") -> str:
    return provider_requests.openai_reasoning_effort(value, model_name)


def _openai_model_supports_reasoning_config(model_name: str) -> bool:
    return provider_requests.openai_model_supports_reasoning_config(model_name)


def _gemini_thinking_config(model_name: str, reasoning_effort: str, types_mod):
    return provider_requests.gemini_thinking_config(model_name, reasoning_effort, types_mod)


def _tool_output_response_payload(output: str) -> dict:
    try:
        parsed = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return {"output": str(output or "")}
    return parsed if isinstance(parsed, dict) else {"output": parsed}


def build_tool_result_message(result: ToolCallResult, *, provider: str = "") -> dict:
    if str(provider or "").lower() in {"openai", "codex"}:
        return {
            "type": "function_call_output",
            "call_id": result.call_id,
            "output": result.output,
        }

    message = {
        "role": "user",
        "content": f"Tool result for {result.name}: {result.output}",
        "tool_result": True,
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
        return model.startswith(("gpt-4", "gpt-5", "gpt-6", "o1", "o3", "o4"))
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
    """Use the same Gemini conversion in compatibility helpers and live requests."""
    return provider_requests.normalise_messages_for_gemini(messages, system_prompt, include_images)


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
    return provider_requests.normalise_messages_for_openai_responses(messages, include_images)


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

        from app.agent.model_registry import initialize_provider_client

        initialize_provider_client(self)
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
        reasoning_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        emitted = False
        streamed_tokens = 0
        budget = current_model_budget.get()
        input_estimate = estimate_request_tokens(self, messages, tools, system_prompt) if budget is not None else 0
        capabilities = await resolve_model_capabilities(self) if budget is not None and budget.limit else None

        async def emit_answer(delta: str) -> None:
            """Record visible output before forwarding an answer delta."""
            nonlocal emitted, streamed_tokens
            emitted = emitted or bool(delta)
            if budget is not None:
                streamed_tokens += estimated_usage(self, 0, delta).output_tokens
            await stream_callback(delta)

        async def emit_reasoning(delta: str) -> None:
            """Record visible output before forwarding a reasoning delta."""
            nonlocal emitted, streamed_tokens
            emitted = emitted or bool(delta)
            if budget is not None:
                streamed_tokens += estimated_usage(self, 0, delta).output_tokens
            await reasoning_callback(delta)

        async def call_provider() -> LLMResponse:
            """Retry only before anything from this request becomes visible."""
            kwargs = {}
            if reasoning_callback is not None:
                kwargs["reasoning_callback"] = emit_reasoning
            reservation, output_limit = budget.reserve(
                input_estimate, output_cap=capabilities.output_allowance if capabilities is not None else 16_384,
                minimum_output=capabilities.minimum_output if capabilities is not None else 16,
            ) if budget is not None else (0, None)
            output_token = current_output_limit.set(
                output_limit if capabilities is None or capabilities.output_parameter is not None else None
            )
            response = None
            try:
                response = await self.provider_adapter.chat_with_tools(
                    messages, tools, system_prompt,
                    emit_answer if stream_callback is not None else None,
                    tool_choice, **kwargs,
                )
                return response
            except Exception as exc:
                if emitted:
                    raise ResponseInterruptedError(
                        "The model response was interrupted after streaming began. "
                        "Partial output was preserved; continue to request a new response."
                    ) from exc
                raise
            finally:
                current_output_limit.reset(output_token)
                if budget is not None:
                    usage = response.usage.normalized() if response is not None else UsageStats()
                    estimated = usage.total_tokens == 0
                    if estimated:
                        if response is not None:
                            output = response.content + response.reasoning_content + json.dumps([
                                {"name": call.tool_name, "arguments": call.arguments} for call in response.tool_calls
                            ])
                            usage = estimated_usage(self, input_estimate, output)
                        else:
                            usage = estimated_usage(self, input_estimate, "")
                            usage.output_tokens = streamed_tokens
                            usage.total_tokens += streamed_tokens
                        usage = settle_estimated_usage(
                            self, usage, output_allowance=output_limit, uncertain=response is None,
                        )
                    budget.record(reservation, usage, estimated=estimated)
        return await _with_retry(
            call_provider
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

    async def _codex_chat(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str,
        stream_callback: Callable[[str], Awaitable[None]] | None,
        tool_choice: str | dict | None = None,
        reasoning_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        # The ChatGPT account ("codex" provider) calls the Responses API with
        # the account's token. That route needs store=false and streaming, so
        # reasoning travels as encrypted content and items are sent without ids.
        import openai

        from app.agent.chatgpt_account import chatgpt_account, plan_error_from_response

        kwargs = provider_requests.openai_responses_kwargs(
            model_name=self.model_name,
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            reasoning_effort="max" if self.reasoning_effort == "ultra" else self.reasoning_effort,
            include_images=self._supports_vision,
            tool_choice=tool_choice,
        )
        kwargs["store"] = False
        # This account endpoint does not accept the API's output-limit field.
        # Input preflight and usage accounting still share the turn budget.
        kwargs.pop("max_output_tokens", None)
        kwargs["include"] = ["reasoning.encrypted_content"]
        kwargs["input"] = _stateless_input(kwargs["input"])

        for attempt in range(2):
            token = await asyncio.to_thread(chatgpt_account.access_token, force_refresh=attempt > 0)
            if self._openai_client is None or token != self._chatgpt_token:
                from openai import AsyncOpenAI

                # Retries stay with _with_retry, which knows which plan errors are final.
                self._openai_client = AsyncOpenAI(api_key=token, max_retries=0)
                self._chatgpt_token = token
            try:
                response = await self._openai_responses_stream(kwargs, stream_callback, reasoning_callback)
                response.usage.source = "chatgpt_account"
                return response
            except ResponseFailedError as exc:
                mapped = plan_error_from_response(0, exc.code, str(exc), exc.param)
                if mapped is None:
                    raise
                raise mapped from exc
            except openai.APIStatusError as exc:
                body = exc.body if isinstance(exc.body, dict) else {}
                error = body.get("error") if isinstance(body.get("error"), dict) else body
                code = str(error.get("code") or "")
                message = str(error.get("message") or error.get("detail") or "")
                if exc.status_code == 401 and attempt == 0:
                    # Renew once before asking the user to sign in again.
                    continue
                mapped = plan_error_from_response(exc.status_code, code, message, str(error.get("param") or ""))
                if mapped is None:
                    raise
                raise mapped from exc
        raise RuntimeError("unreachable")

    async def _gemini_chat(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str,
        stream_callback: Callable[[str], Awaitable[None]] | None,
        tool_choice: str | dict | None = None,
        reasoning_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Build a Gemini request using the common answer/reasoning interface."""
        gemini_messages, config = provider_requests.gemini_request_payload(
            model_name=self.model_name,
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            reasoning_effort=self.reasoning_effort,
            include_images=self._supports_vision,
            tool_choice=tool_choice,
        )

        if stream_callback is not None or reasoning_callback is not None:
            return await self._gemini_stream(gemini_messages, config, stream_callback, reasoning_callback)
        else:
            return await self._gemini_no_stream(gemini_messages, config)

    async def _gemini_no_stream(self, contents, config) -> LLMResponse:
        """Accept callable parts only from a successfully completed candidate."""
        if not self._genai_client:
            raise ValueError("Gemini provider selected but no Google API key is configured.")
        kwargs = {"model": self.model_name, "contents": contents}
        if config is not None:
            kwargs["config"] = config

        response = await self._genai_client.aio.models.generate_content(**kwargs)
        terminal_reason = provider_responses.gemini_finish_reason(response)
        if not terminal_reason:
            raise ResponseInterruptedError("Gemini returned no terminal completion; no tool calls were accepted.")

        text, reasoning_content = _split_gemini_text_parts(response)
        if not text:
            try:
                text = response.text or ""
            except Exception:
                pass

        tool_calls = _extract_gemini_tool_calls(response) if terminal_reason == "STOP" else []
        finish_reason = provider_responses.gemini_completion_status(terminal_reason, has_tool_calls=bool(tool_calls))
        # Final text can carry a thought signature needed by the next turn.
        model_parts = _extract_gemini_model_parts(response) if terminal_reason == "STOP" else []
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
            provider_finish_reason=terminal_reason,
        )

    async def _gemini_stream(self, contents, config, stream_callback, reasoning_callback=None) -> LLMResponse:
        """Collect streamed parts but expose tools only after successful completion."""
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
        terminal_reason = ""

        async for chunk in await self._genai_client.aio.models.generate_content_stream(**kwargs):
            # A finish reason authorizes only the content preceding that terminal
            # candidate. Later metadata-only chunks may still report usage.
            chunk_parts = _extract_gemini_model_parts(chunk)
            if terminal_reason and chunk_parts:
                raise ResponseInterruptedError("Gemini sent content after completion; no tool calls were accepted.")
            terminal_reason = provider_responses.gemini_finish_reason(chunk) or terminal_reason
            chunk_usage = _usage_from_gemini_response(chunk)
            if chunk_usage.total_tokens:
                usage = chunk_usage
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
                if reasoning_callback is not None:
                    await reasoning_callback(thought_part)
            if text_part:
                collected_text += text_part
                if stream_callback is not None:
                    await stream_callback(text_part)

        if not terminal_reason:
            raise ResponseInterruptedError("Gemini stream ended without terminal completion; no tool calls were accepted.")
        if terminal_reason != "STOP":
            raw_function_calls = []
            raw_model_parts = []

        # Convert raw FunctionCall objects only after a successful terminal event.
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

        finish_reason = provider_responses.gemini_completion_status(terminal_reason, has_tool_calls=bool(tool_calls))
        provider_messages = (
            [{"role": "model", "gemini_parts": raw_model_parts}]
            if raw_model_parts
            else []
        )
        return LLMResponse(
            content=collected_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning_content=collected_reasoning_content,
            provider_messages=provider_messages,
            usage=usage,
            provider_finish_reason=terminal_reason,
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
        reasoning_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        return await self._openai_responses_chat(
            messages,
            tools,
            system_prompt,
            stream_callback,
            tool_choice,
            reasoning_callback,
        )

    async def _openai_responses_chat(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str,
        stream_callback: Callable[[str], Awaitable[None]] | None,
        tool_choice: str | dict | None = None,
        reasoning_callback: Callable[[str], Awaitable[None]] | None = None,
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

        if stream_callback is not None or reasoning_callback is not None:
            return await self._openai_responses_stream(kwargs, stream_callback, reasoning_callback)
        return await self._openai_responses_no_stream(kwargs)

    async def _openai_responses_no_stream(self, kwargs: dict) -> LLMResponse:
        """Require a terminal response before returning any callable tool results."""
        response = await self._openai_client.responses.create(**kwargs)
        content, reasoning_content = _openai_response_text_and_reasoning(response)
        tool_calls = _extract_openai_response_tool_calls(response)
        status = str(_obj_get(response, "status", "") or "")
        if status not in {"completed", "incomplete"}:
            raise ResponseInterruptedError(f"The model response ended with status {status}.")
        finish_reason, terminal_reason = provider_responses.openai_completion_status(
            response, has_tool_calls=bool(tool_calls),
        )
        if finish_reason not in {"stop", "tool_calls"}:
            tool_calls = []
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning_content=reasoning_content,
            provider_messages=_openai_response_provider_messages(response) if finish_reason in {"stop", "tool_calls"} else [],
            usage=_usage_from_openai(_obj_get(response, "usage", None)),
            provider_finish_reason=terminal_reason,
        )

    async def _openai_responses_stream(
        self,
        kwargs: dict,
        stream_callback: Callable[[str], Awaitable[None]] | None,
        reasoning_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        stream = await self._openai_client.responses.create(**{**kwargs, "stream": True})
        collected_content = ""
        collected_reasoning_content = ""
        final_response = None
        completed_items: list[Any] = []

        async for event in stream:
            event_type = str(_obj_get(event, "type", "") or "")
            if final_response is not None and event_type.startswith(("response.output", "response.reasoning")):
                raise ResponseInterruptedError("The model sent content after completion; no tool calls were accepted.")
            if event_type == "response.output_text.delta":
                delta = str(_obj_get(event, "delta", "") or "")
                if delta:
                    collected_content += delta
                    if stream_callback is not None:
                        await stream_callback(delta)
            elif event_type == "response.reasoning_summary_text.delta":
                delta = str(_obj_get(event, "delta", "") or "")
                collected_reasoning_content += delta
                if delta and reasoning_callback is not None:
                    await reasoning_callback(delta)
            elif event_type == "response.output_item.done":
                item = _obj_get(event, "item", None)
                if item is not None:
                    completed_items.append(item)
            elif event_type in {"response.completed", "response.incomplete", "response.failed"}:
                final_response = _obj_get(event, "response", None)

        if final_response is not None and completed_items and not _openai_response_output_items(final_response):
            # The ChatGPT plan route (store=false) streams each item but sends
            # response.completed with an empty output; read the items instead.
            final_response = {
                "output": completed_items,
                **{key: _obj_get(final_response, key, None)
                   for key in ("status", "error", "incomplete_details", "usage")},
            }

        if final_response is not None:
            status = str(_obj_get(final_response, "status", "") or "")
            if status == "failed":
                error = _obj_get(final_response, "error", None)
                raise ResponseFailedError(
                    str(_obj_get(error, "message", "") or "OpenAI response failed."),
                    code=str(_obj_get(error, "code", "") or ""),
                    param=str(_obj_get(error, "param", "") or ""),
                )
            final_content, final_reasoning = _openai_response_text_and_reasoning(final_response)
            if status not in {"completed", "incomplete"}:
                raise ResponseInterruptedError("The model stream did not confirm a terminal response state.")
            final_tool_calls = _extract_openai_response_tool_calls(final_response)
            finish_reason, terminal_reason = provider_responses.openai_completion_status(
                final_response, has_tool_calls=bool(final_tool_calls),
            )
            accepted = finish_reason in {"stop", "tool_calls"}
            return LLMResponse(
                content=final_content or collected_content,
                tool_calls=final_tool_calls if accepted else [],
                finish_reason=finish_reason,
                reasoning_content=final_reasoning or collected_reasoning_content,
                provider_messages=_openai_response_provider_messages(final_response) if accepted else [],
                usage=_usage_from_openai(_obj_get(final_response, "usage", None)),
                provider_finish_reason=terminal_reason,
            )

        raise ResponseInterruptedError(
            "The model stream ended without a completion event. No buffered tool calls were dispatched."
        )


# ---------------------------------------------------------------------------
# Helper extractors
# ---------------------------------------------------------------------------


def _extract_gemini_tool_calls(response) -> list[ToolCallRequest]:
    """Extract function calls from a google-genai GenerateContentResponse."""
    return provider_responses.extract_gemini_tool_calls(response, ToolCallRequest)
