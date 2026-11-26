"""Regressions for honest completion and interruptible turn endings."""

import asyncio
import json

import pytest

from app.agent.harness.turn_lifecycle import TurnLifecycle
from app.agent.llm_client import LLMResponse, ToolCallRequest
from app.agent.tests.test_turn_loop import FakeObservability, PersistingFakeMemory, _echo_registry
from app.agent.tool_registry import ToolRegistry
from app.agent.turn_loop import TurnLoop
from app.agent.turn_text import plain_text_describes_next_action


class ScriptedResponses:
    """Return provider responses while recording the actual tool-choice contract."""

    def __init__(self, responses):
        """Keep the script finite so an accidental repair loop fails immediately."""
        self.responses = list(responses)
        self.choices = []

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None, tool_choice=None):
        """Record each request without making any live provider calls."""
        self.choices.append(tool_choice)
        return self.responses.pop(0)


def run_script(responses, *, registry=None, max_iterations=5):
    """Exercise persistence and final events through the real turn coordinator."""
    client = ScriptedResponses(responses)
    memory = PersistingFakeMemory()
    loop = TurnLoop(
        llm_client=client,
        registry=registry or ToolRegistry(),
        memory=memory,
        observability=FakeObservability(),
        max_iterations=max_iterations,
    )

    async def collect():
        """Collect one complete, bounded turn."""
        return [event async for event in loop.run("Inspect the test data", "completion-regression", system_prompt="")]

    try:
        events = asyncio.run(collect())
    finally:
        loop.shutdown()
    return events, memory, client


@pytest.mark.parametrize("repaired", [True, False])
def test_empty_answer_repairs_once_or_remains_incomplete(repaired):
    """A reasoning-only response cannot be persisted as a successful Done."""
    answer = "The test data contains three records." if repaired else " "
    events, memory, client = run_script([
        LLMResponse(content="", reasoning_content="Checking the result"),
        LLMResponse(content=answer),
    ])
    done = events[-1]["data"]
    assert len(client.choices) == 2
    assert len(memory.persisted_turns) == 1
    assert done["status"] == ("complete" if repaired else "paused")
    assert memory.clear_task_progress_calls == int(repaired)
    if repaired:
        assert done["summary"] == answer
    else:
        assert done["reason_code"] == "model_empty_response"
        assert done["summary"] != "Done."


@pytest.mark.parametrize("empty_final", [
    LLMResponse(content="", tool_calls=[ToolCallRequest("empty-final", "final_answer", {})]),
    LLMResponse(content='{"name":"final_answer","arguments":{}}'),
])
def test_empty_legacy_final_cannot_reset_the_repair_limit(empty_final):
    """Alternating progress and an empty completion envelope still stops once."""
    events, memory, client = run_script([
        LLMResponse(content="I'll inspect the file."),
        empty_final,
    ], registry=_echo_registry())
    assert len(client.choices) == 2
    assert events[-1]["data"]["reason_code"] == "model_empty_response"
    assert memory.clear_task_progress_calls == 0


@pytest.mark.parametrize("finish_reason", ["blocked", "length"])
def test_unfinished_response_cannot_dispatch_tools(finish_reason):
    """Terminal failure takes precedence over a parsed but unusable tool call."""
    response = LLMResponse(
        content="Partial result",
        tool_calls=[ToolCallRequest("unaccepted", "echo_tool", {"text": "must not execute"})],
        finish_reason=finish_reason,
    )
    events, memory, client = run_script([response], registry=_echo_registry())
    assert not any(event["event"] == "tool_start" for event in events)
    assert len(client.choices) == 1
    assert events[-1]["data"]["status"] == "paused"
    assert memory.clear_task_progress_calls == 0


def test_progress_repair_allows_a_clarifying_answer():
    """A repair must not require an unrelated action when the model needs input."""
    answer = "Which file should I inspect?"
    events, _, client = run_script([
        LLMResponse(content="I'll inspect the file now."),
        LLMResponse(content=answer),
    ], registry=_echo_registry())
    assert client.choices == [None, None]
    assert events[-1]["data"]["summary"] == answer
    assert events[-1]["data"]["status"] == "complete"
    assert not any(event["event"] == "tool_start" for event in events)


def test_structured_user_answer_is_preserved():
    """Ordinary JSON containing a summary is output, not a completion command."""
    answer = json.dumps({"summary": "Three records", "records": [1, 2, 3]})
    events, memory, _ = run_script([LLMResponse(content=answer)])
    assert events[-1]["data"]["summary"] == answer
    assert memory.persisted_turns[0]["assistant_message"] == answer


@pytest.mark.parametrize("response", [
    LLMResponse(content="I'll inspect the next file."),
    LLMResponse(content="Everything is complete", finish_reason="length"),
])
def test_invalid_wrap_up_keeps_the_verified_pause_notice(response):
    """An unfinished or action-promising wrap-up cannot overwrite the pause."""
    events, _, _ = run_script([
        LLMResponse(content="", tool_calls=[ToolCallRequest("pending", "echo_tool", {"text": "test"})]),
        response,
    ], registry=_echo_registry(), max_iterations=1)
    done = events[-1]["data"]
    assert done["status"] == "paused"
    assert "iteration limit" in done["summary"]
    assert not any(event["event"] == "token" and response.content in event["data"]["content"] for event in events)


def test_pause_interrupts_the_final_wrap_up(monkeypatch):
    """Pause must cancel a summary request just as it cancels the main request."""
    lifecycles = []

    class CapturedLifecycle(TurnLifecycle):
        """Expose the active lifecycle without changing its behavior."""

        def __init__(self, *args, **kwargs):
            """Capture the lifecycle used by the real coordinator."""
            super().__init__(*args, **kwargs)
            lifecycles.append(self)

    monkeypatch.setattr("app.agent.turn_loop.TurnLifecycle", CapturedLifecycle)

    async def scenario():
        """Pause only after the tool-less provider request has actually started."""
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class HangingWrapUp:
            """Request one action and then wait forever for the summary."""

            async def chat_with_tools(self, messages, tools, **kwargs):
                """Record cancellation at the provider boundary."""
                if tools:
                    return LLMResponse(content="", tool_calls=[ToolCallRequest("pending", "echo_tool", {"text": "test"})])
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        loop = TurnLoop(
            llm_client=HangingWrapUp(), registry=_echo_registry(),
            memory=PersistingFakeMemory(), observability=FakeObservability(), max_iterations=1,
        )

        async def collect():
            """Keep the consumer attached while Pause is processed."""
            return [event async for event in loop.run("Inspect", "pause-wrap-up", system_prompt="")]

        task = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(started.wait(), 1)
            await lifecycles[0].request_pause()
            events = await asyncio.wait_for(task, 1)
            assert cancelled.is_set()
            assert events[-1]["data"]["status"] == "paused"
            assert events[-1]["data"]["reason_code"] == "user_pause"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            loop.shutdown()

    asyncio.run(scenario())


def test_progress_detection_checks_the_last_step_without_forcing_user_actions():
    """Long progress and typographic apostrophes must not masquerade as a final."""
    long_progress = "I checked the first section. " * 20 + "\n\nNext, I’ll inspect the remaining files."
    assert plain_text_describes_next_action(long_progress)
    assert not plain_text_describes_next_action("I need you to choose which workspace to use.")
    assert not plain_text_describes_next_action("I'll continue once you provide the file.")
    assert not plain_text_describes_next_action('The example sentence is: "I will inspect the file."')
