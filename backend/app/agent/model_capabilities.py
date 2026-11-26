"""Provider output-cap contracts and conservative local request allowances."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapabilities:
    output_parameter: str | None
    output_allowance: int
    hidden_reasoning: bool
    source: str = "local_policy"
    minimum_output: int = 16


def model_capabilities(client) -> ModelCapabilities:
    """Use conservative policy allowances when providers publish no model limits."""
    provider = client.provider
    model = client.model_name.lower()
    effort = str(getattr(client, "reasoning_effort", "medium"))
    if provider == "gemini":
        # Pro's numeric thinking budget has a non-zero provider minimum.
        minimum = 129 if model.startswith("gemini-2.5-pro") else 16
        reasoning = not (effort == "none" and model.startswith("gemini-2.5-flash"))
        return ModelCapabilities("max_output_tokens", 8_192, reasoning, minimum_output=minimum)
    from app.agent.llm_provider_requests import openai_reasoning_effort

    reasoning = (
        openai_reasoning_effort(effort, model) != "none"
        and model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))
    )
    if provider == "codex":
        return ModelCapabilities(None, 16_384, reasoning)
    known_large_output = model.startswith(("gpt-4o", "gpt-4.1", "gpt-5", "gpt-6", "o1", "o3", "o4"))
    allowance = 16_384 if known_large_output else 4_096
    return ModelCapabilities("max_output_tokens", allowance, reasoning)


async def resolve_model_capabilities(client) -> ModelCapabilities:
    """Read Gemini's model limit with a bounded, per-client metadata cache."""
    fallback = model_capabilities(client)
    if client.provider != "gemini" or client._genai_client is None:
        return fallback
    cached = getattr(client, "_output_capabilities_cache", None)
    if cached and cached[0] > time.monotonic() and cached[2] == client.model_name:
        return cached[1]
    result, ttl = fallback, 60
    try:
        metadata = await asyncio.wait_for(client._genai_client.aio.models.get(model=client.model_name), timeout=3)
        limit = getattr(metadata, "output_token_limit", None)
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            result = ModelCapabilities(
                fallback.output_parameter, min(16_384, limit), fallback.hidden_reasoning,
                "provider_metadata", fallback.minimum_output,
            )
            ttl = 600
    except Exception:
        # Metadata unavailability must not prevent an otherwise valid request.
        pass
    client._output_capabilities_cache = (time.monotonic() + ttl, result, client.model_name)
    return result
