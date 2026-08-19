"""Provider adapter boundary for LLM chat execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol


class LLMProviderAdapter(Protocol):
    provider: str

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str = "",
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        tool_choice: str | dict | None = None,
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
    ) -> Any:
        return await self._client._gemini_chat(
            messages,
            tools,
            system_prompt,
            stream_callback,
            tool_choice,
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
    ) -> Any:
        return await self._client._openai_chat(
            messages,
            tools,
            system_prompt,
            stream_callback,
            tool_choice,
        )


def create_llm_provider_adapter(provider: str, client: Any) -> LLMProviderAdapter:
    provider_name = str(provider or "").lower()
    if provider_name == "gemini":
        return GeminiProviderAdapter(client)
    if provider_name == "openai":
        return OpenAICompatibleProviderAdapter(client, provider=provider_name)
    raise ValueError(f"Unsupported provider adapter: {provider!r}")
