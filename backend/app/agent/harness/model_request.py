"""One model request, independent of turn completion and tool execution."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Literal

from app.agent.harness.context_budget import ContextBudgetExceeded, fit_live_context
from app.agent.harness.turn_observer import TurnObserver
from app.agent.llm_client import LLMClient, LLMResponse
from app.agent.observability.recorder import UsageStats, usage_from_any


@dataclass(frozen=True)
class ModelRequest:
    messages: list[dict]
    tools: list[dict]
    system_prompt: str
    latest_round_start: int
    revision: int
    tool_choice: Literal["auto", "required", "none"] = "auto"
    stream_reasoning: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResult:
    response: LLMResponse
    messages: list[dict]
    latest_round_start: int
    revision: int
    usage: UsageStats
    answer_text: str
    reasoning_text: str


class ModelRequestError(RuntimeError):
    def __init__(self, message: str, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class ModelRequestRunner:
    def __init__(
        self, client: LLMClient, *, timeout_seconds: float, observer: TurnObserver,
        emit: Callable[[dict], None], on_request: Callable[[], None],
    ) -> None:
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.observer = observer
        self.emit = emit
        self.on_request = on_request
        # Retain streamed partial text if a request is cancelled or fails.
        self.answer_text = ""
        self.reasoning_text = ""

    async def _answer(self, delta: str) -> None:
        self.answer_text += str(delta or "")
        self.emit({"event": "token", "data": {"content": delta}})

    async def _reasoning(self, delta: str) -> None:
        self.reasoning_text += str(delta or "")
        self.emit({"event": "thinking", "data": {"content": delta}})

    async def request(self, request: ModelRequest) -> ModelResult:
        self.answer_text = ""
        self.reasoning_text = ""
        started = time.perf_counter()
        self.on_request()
        self.observer.event(
            "llm_call_started",
            input={"message_count": len(request.messages), "tool_count": len(request.tools), "tool_choice": request.tool_choice},
            metadata=request.metadata,
        )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                messages, round_start, compaction = await fit_live_context(
                    request.messages, request.tools, request.system_prompt, llm_client=self.client,
                    latest_round_start=request.latest_round_start, timeout_seconds=self.timeout_seconds,
                )
                if compaction:
                    self.observer.event("live_context_compacted", metadata=compaction)
                    self.emit({"event": "context_compacted", "data": compaction})
                kwargs = {}
                if request.tool_choice == "required":
                    kwargs["tool_choice"] = "required"
                if request.stream_reasoning and isinstance(self.client, LLMClient):
                    kwargs["reasoning_callback"] = self._reasoning
                # Buffer text while tools are available until the coordinator
                # decides whether it is progress or the final answer.
                response = await self.client.chat_with_tools(
                    messages, request.tools, system_prompt=request.system_prompt,
                    stream_callback=None if request.tools else self._answer, **kwargs,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, ContextBudgetExceeded):
                code, message = exc.reason_code, str(exc)
            elif isinstance(exc, TimeoutError):
                code = "llm_timeout"
                message = f"Timed out: LLM call exceeded {self.timeout_seconds}s limit."
            else:
                code, message = "provider_error", f"Provider error: {exc}"
            self.observer.error(str(exc), error_type=code, metadata={"phase": "llm_call", **request.metadata})
            self.observer.event(
                "llm_call_finished", level="warning" if request.metadata.get("wrap_up") else "error", status="error",
                duration_ms=round((time.perf_counter() - started) * 1000),
                error_code="wrap_up_failed" if request.metadata.get("wrap_up") else code,
                error_message=message, metadata=request.metadata,
            )
            raise ModelRequestError(message, code) from exc
        usage = usage_from_any(getattr(response, "usage", None))
        self.observer.event(
            "llm_call_finished", status="ok", duration_ms=round((time.perf_counter() - started) * 1000),
            output={
                "finish_reason": response.finish_reason, "content": response.content,
                "tool_calls": [{"call_id": call.call_id, "tool_name": call.tool_name, "arguments": call.arguments} for call in response.tool_calls],
            },
            tokens=usage, metadata=request.metadata,
        )
        return ModelResult(response, messages, round_start, request.revision, usage, self.answer_text, self.reasoning_text)
