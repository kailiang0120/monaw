import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent.harness import context_budget
from app.agent.context_usage import estimate_message_tokens, count_text_tokens
from app.agent.harness.context_protection import PROTECTED_CONTEXT
from app.agent.harness.model_request import ModelRequest, ModelRequestRunner
from app.agent.harness.tool_protocol import ToolCallResult
from app.agent.harness.turn_transcript import TurnTranscript
from app.agent.llm_client import LLMResponse, ToolCallRequest


def test_compaction_covers_all_old_context_and_keeps_current_tool_round(monkeypatch):
    monkeypatch.setattr(context_budget, "input_budget", lambda _client: 3000)

    class Summarizer:
        provider = "openai"
        model_name = "gpt-6-luna"
        segments = []
        async def chat(self, messages, system_prompt):
            assert count_text_tokens(system_prompt, llm_client=self) + estimate_message_tokens(messages, llm_client=self) <= 1500
            self.segments.append(messages[0]["content"].split("Next transcript segment:\n", 1)[1])
            return "Kept the exact decisions, identifiers, and unresolved work."

    llm = Summarizer()
    instruction = {"role": "user", "content": "Tool result for this task: do not publish", "images": [{"path": "example.png", "mime_type": "image/png"}]}
    older = [{"role": "assistant", "content": "START " + "historical decision " * 5000 + " END"}]
    current = [
        {"type": "function_call", "call_id": "current", "name": "inspect", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "current", "output": "result"},
        {"role": "user", "content": "Use a table instead"},
    ]
    messages = [instruction, *older, *current]
    result, boundary, info = asyncio.run(context_budget.fit_live_context(messages, [], "", llm_client=llm, latest_round_start=2, timeout_seconds=1))
    assert "".join(llm.segments) == json.dumps(older, ensure_ascii=False)
    assert result[boundary:] == current
    assert instruction in result
    assert info["tokens_after"] < info["tokens_before"]
    assert info["summarized"] is True
    assert messages == [instruction, *older, *current]


def test_compaction_failure_pauses_without_dropping_instructions(monkeypatch):
    monkeypatch.setattr(context_budget, "input_budget", lambda _client: 1000)
    class FailingLLM:
        async def chat(self, **kwargs):
            raise RuntimeError("offline")
    messages = [{"role": "assistant", "content": "old context " * 1000}, {"role": "user", "content": "preserve this"}]
    with pytest.raises(context_budget.ContextBudgetExceeded, match="compaction failed"):
        asyncio.run(context_budget.fit_live_context(messages, [], "", llm_client=FailingLLM(), latest_round_start=1, timeout_seconds=1))
    assert messages[-1]["content"] == "preserve this"


def test_gemini_budget_includes_function_parts_and_bounds_response(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(context_budget, "input_budget", lambda _client: 7000)
    llm = SimpleNamespace(provider="gemini", model_name="gemini-flash-latest")
    call = {"role": "assistant", "gemini_parts": [{"function_call": {"name": "inspect", "args": {"query": "argument " * 400}}}]}
    response = {"role": "user", "content": "ignored", "gemini_function_response": {"id": "call", "name": "inspect", "response": {"data": "observation " * 10000}}}
    assert estimate_message_tokens([call], llm_client=llm) > 800
    result, boundary, info = asyncio.run(context_budget.fit_live_context([call, response], [], "", llm_client=llm, latest_round_start=0, timeout_seconds=1))
    assert result[0] == call and boundary == 0
    assert result[1]["gemini_function_response"]["id"] == "call"
    assert estimate_message_tokens(result, llm_client=llm) <= 7000
    assert info["tokens_before"] > info["tokens_after"]


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_failed_tool_round_is_preserved_exactly_and_internal_flags_stay_local(monkeypatch, provider):
    """Keep failed calls and sibling results exact while summarizing completed work."""
    monkeypatch.setattr(context_budget, "input_budget", lambda _client: 18_000)

    class Client:
        """Capture both the summary input and the final provider request."""

        model_name = "gpt-6-luna" if provider == "openai" else "gemini-flash-lite-latest"

        def __init__(self):
            """Record inputs without making any network request."""
            self.provider = provider
            self.summaries, self.received = [], []

        async def chat(self, messages, **kwargs):
            """Summarize only the older, successfully completed background."""
            self.summaries.append(messages)
            return "Older completed background work."

        async def chat_with_tools(self, messages, tools, **kwargs):
            """Receive exact failure evidence without private compaction flags."""
            self.received = messages
            return LLMResponse(content="The failed lookup needs correction.")

    client = Client()
    transcript = TurnTranscript(provider=provider)
    transcript.append({"role": "assistant", "content": "completed background " * 16000})
    call = {"type": "function_call", "call_id": "failed", "name": "lookup", "arguments": '{"id":"0042"}'}
    sibling = {"type": "function_call", "call_id": "sibling", "name": "lookup", "arguments": '{"id":"0043"}'}
    provider_messages = [call, sibling]
    if provider == "gemini":
        provider_messages = [
            {
                "role": "model",
                "gemini_parts": [
                    {"function_call": {"id": "failed", "name": "lookup", "args": {"id": "0042"}}},
                    {"function_call": {"id": "sibling", "name": "lookup", "args": {"id": "0043"}}},
                ],
            }
        ]
    transcript.begin_tool_round(
        LLMResponse(
            content="",
            provider_messages=provider_messages,
            tool_calls=[
                ToolCallRequest("failed", "lookup", {"id": "0042"}),
                ToolCallRequest("sibling", "lookup", {"id": "0043"}),
            ],
        )
    )
    error = json.dumps(
        {"status": "error", "error": "exact-error-prefix " + "x" * 20000 + " exact-error-suffix"}
    )
    transcript.add_tool_result(ToolCallResult.from_output(call_id="failed", name="lookup", output=error))
    transcript.add_tool_result(
        ToolCallResult.from_output(
            call_id="sibling",
            name="lookup",
            output='{"status":"ok","result":"second lookup"}',
        )
    )
    preserved = transcript.messages[1:]
    transcript.begin_tool_round(LLMResponse(content="The next check returned a result."))
    assert all(item.get(PROTECTED_CONTEXT) for item in preserved)
    runner = ModelRequestRunner(
        client,
        timeout_seconds=10,
        observer=SimpleNamespace(event=lambda *a, **k: None, error=lambda *a, **k: None),
        emit=lambda event: None,
        on_request=lambda: None,
    )
    result = asyncio.run(
        runner.request(
            ModelRequest(
                messages=transcript.messages,
                tools=[],
                system_prompt="Keep the original task goal.",
                latest_round_start=transcript.latest_round_start,
                revision=0,
            )
        )
    )
    assert client.summaries and "exact-error-prefix" not in str(client.summaries)
    assert all(item in result.messages for item in preserved)
    assert "exact-error-prefix" in str(client.received) and "exact-error-suffix" in str(client.received)
    assert all(PROTECTED_CONTEXT not in item for item in client.received)


def test_oversized_protected_failure_pauses_without_truncation_or_summary(monkeypatch):
    """A hard limit must never silently remove the error needed for continuation."""
    monkeypatch.setattr(context_budget, "input_budget", lambda _client: 1000)

    class Client:
        """Reject any attempt to summarize protected records."""

        async def chat(self, **kwargs):
            """No summarizer call is needed when protected input alone is too large."""
            raise AssertionError("Protected failure was sent to the summarizer")

    failure = {
        "type": "function_call_output",
        "call_id": "failed",
        "output": "error " * 10000,
        PROTECTED_CONTEXT: True,
    }
    messages = [
        {"role": "assistant", "content": "Older completed work."},
        failure,
        {"role": "user", "content": "Continue"},
    ]
    with pytest.raises(
        context_budget.ContextBudgetExceeded, match="protected task instructions, failure records"
    ):
        asyncio.run(
            context_budget.fit_live_context(
                messages, [], "", llm_client=Client(), latest_round_start=2, timeout_seconds=1
            )
        )
    assert messages[1]["output"] == "error " * 10000
