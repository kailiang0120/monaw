"""Request accounting shared by a turn's main calls, compaction and retries."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

from app.agent.observability.recorder import UsageStats


class TokenBudgetExceeded(RuntimeError):
    """A model request cannot fit in the remaining configured allowance."""

    reason_code = "token_budget_exhausted"
    retryable = False


@dataclass
class ModelUsageBudget:
    limit: int = 0
    usage: UsageStats = field(default_factory=UsageStats)
    reserved: int = 0
    estimated_requests: int = 0
    requests: int = 0

    def reserve(self, input_tokens: int, *, output_cap: int = 16_384, minimum_output: int = 16) -> tuple[int, int | None]:
        """Reserve input and bounded output before any provider attempt starts."""
        if not self.limit:
            return 0, None
        remaining = self.limit - self.usage.total_tokens - self.reserved
        output_limit = min(output_cap, remaining - input_tokens)
        if output_limit < minimum_output:
            raise TokenBudgetExceeded(
                "Paused: the next model request does not fit within the remaining token budget. "
                "Continue with a new turn or increase the limit in Settings."
            )
        reservation = input_tokens + output_limit
        self.reserved += reservation
        return reservation, output_limit

    def record(self, reservation: int, usage: UsageStats, *, estimated: bool) -> None:
        """Settle one attempt using provider usage or a clearly marked estimate."""
        self.reserved -= reservation
        self.usage = self.usage.merge(usage)
        self.requests += 1
        self.estimated_requests += int(estimated)


current_model_budget: ContextVar[ModelUsageBudget | None] = ContextVar("model_usage_budget", default=None)
current_output_limit: ContextVar[int | None] = ContextVar("model_output_limit", default=None)


def estimate_request_tokens(client, messages: list[dict], tools: list[dict], system_prompt: str) -> int:
    """Use the existing context estimator without introducing an import cycle."""
    from app.agent.context_usage import count_text_tokens, estimate_message_tokens, estimate_tool_schema_tokens

    return (
        count_text_tokens(system_prompt, llm_client=client)
        + estimate_message_tokens(messages, llm_client=client)
        + estimate_tool_schema_tokens(tools, llm_client=client)
    )


def estimated_usage(client, input_tokens: int, output: str) -> UsageStats:
    """Estimate missing usage without treating ChatGPT plan calls as API charges."""
    from app.agent.context_usage import count_text_tokens

    return UsageStats(
        input_tokens=input_tokens,
        output_tokens=count_text_tokens(output, llm_client=client),
        source="chatgpt_account" if client.provider == "codex" else "estimated",
    ).normalized()


def settle_estimated_usage(client, usage: UsageStats, *, output_allowance: int | None, uncertain: bool) -> UsageStats:
    """Retain the reserved output when failure or hidden reasoning hides usage."""
    from app.agent.model_capabilities import model_capabilities

    if output_allowance is not None and (uncertain or model_capabilities(client).hidden_reasoning):
        # A visible reasoning summary is not a measurement of hidden tokens.
        usage.output_tokens = max(usage.output_tokens, output_allowance)
        usage.total_tokens = usage.input_tokens + usage.output_tokens
    return usage
