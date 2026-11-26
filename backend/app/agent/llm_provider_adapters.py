"""Provider adapter boundary for LLM chat execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from functools import partial

from app.agent.model_registry import get_connection


class LLMProviderAdapter(Protocol):
    provider: str

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str = "",
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        tool_choice: str | dict | None = None,
        reasoning_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> Any:
        """Execute one provider-specific chat request."""


class GeminiProviderAdapter:
    provider = "gemini"

    def __init__(self, client: Any) -> None:
        self._client = client

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str = "",
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        tool_choice: str | dict | None = None,
        reasoning_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> Any:
        """Expose the same answer/reasoning callback contract as other providers."""
        return await self._client._gemini_chat(
            messages,
            tools,
            system_prompt,
            stream_callback,
            tool_choice,
            reasoning_callback,
        )


class OpenAICompatibleProviderAdapter:
    def __init__(self, client: Any, *, provider: str) -> None:
        self._client = client
        self.provider = provider

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str = "",
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        tool_choice: str | dict | None = None,
        reasoning_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> Any:
        return await self._client._openai_chat(
            messages,
            tools,
            system_prompt,
            stream_callback,
            tool_choice,
            reasoning_callback,
        )


class CodexAccountProviderAdapter:
    provider = "codex"

    def __init__(self, client: Any) -> None:
        self._client = client

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str = "",
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        tool_choice: str | dict | None = None,
        reasoning_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> Any:
        return await self._client._codex_chat(
            messages, tools, system_prompt, stream_callback, tool_choice,
            reasoning_callback,
        )


_ADAPTER_FACTORIES = {
    "gemini_generate_content": GeminiProviderAdapter,
    "openai_responses": partial(OpenAICompatibleProviderAdapter, provider="openai"),
    "chatgpt_account": CodexAccountProviderAdapter,
}


def create_llm_provider_adapter(provider: str, client: Any) -> LLMProviderAdapter:
    """Dispatch by registered protocol, independently of UI connection labels."""
    connection = get_connection(provider)
    return _ADAPTER_FACTORIES[connection.protocol](client)
