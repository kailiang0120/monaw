import asyncio
import json

import pytest

from app.agent.harness import context_budget
from app.agent.context_usage import estimate_message_tokens, count_text_tokens


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
