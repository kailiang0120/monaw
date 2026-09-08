import asyncio
import json
import shutil
import threading
import time
from pathlib import Path
from app.agent.iteration_budget import IterationBudget
from app.agent.tool_registry import ToolRegistry
from app.agent.turn_loop import TurnLoop, _extract_image_attachments, _repeat_key
from app.agent.run_context import current_conversation_id
from app.agent.controller_policy import load_policy, update_permitted_roots
from app.agent.llm_client import LLMResponse, ToolCallRequest


def final_answer_response(answer: str, attachment_paths: list[str] | None = None) -> LLMResponse:
    arguments = {"answer": answer}
    if attachment_paths is not None:
        arguments["attachment_paths"] = attachment_paths
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCallRequest(
                call_id="final-answer",
                tool_name="final_answer",
                arguments=arguments,
            )
        ],
        finish_reason="tool_calls",
    )


def png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


class FakeMemory:
    def __init__(self) -> None:
        self.added_messages: list[tuple[str, str]] = []
        self.tool_results: list[tuple[str, str]] = []
        self.task_goal = ""
        self.clear_task_progress_calls = 0

    def get_or_create(self, _conversation_id: str, title: str = ""):
        return {"title": title}

    def set_task_goal(self, _conversation_id: str, goal: str) -> None:
        self.task_goal = goal

    def set_active_task(self, _conversation_id: str, _task) -> None:
        return None

    async def ensure_context_fits(
        self,
        _conversation_id: str,
        _system_prompt: str = "",
        **_kwargs,
    ) -> None:
        return None

    def build_llm_messages(self, _conversation_id: str) -> list[dict]:
        return []

    def add_tool_outcome(self, _conversation_id: str, tool_name: str, result: str) -> None:
        self.tool_results.append((tool_name, result))

    async def add_message_and_maybe_summarize(self, _conversation_id: str, role: str, content: str) -> None:
        self.added_messages.append((role, content))

    def clear_task_progress(self, _conversation_id: str) -> None:
        self.clear_task_progress_calls += 1
        self.task_goal = ""


class PersistingFakeMemory(FakeMemory):
    def __init__(self) -> None:
        super().__init__()
        self.persisted_turns: list[dict] = []

    async def persist_turn(
        self,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        *,
        tool_calls: list[dict],
        thinking: str = "",
        status: str = "complete",
        response_duration_ms: int | None = None,
        response_attachments: list[dict] | None = None,
    ) -> None:
        self.persisted_turns.append(
            {
                "conversation_id": conversation_id,
                "user_message": user_message,
                "assistant_message": assistant_message,
                "tool_calls": tool_calls,
                "thinking": thinking,
                "status": status,
                "response_duration_ms": response_duration_ms,
                "response_attachments": response_attachments or [],
            }
        )


class FakeObservability:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def start_run(self, **kwargs):
        self.events.append({"method": "start_run", **kwargs})
        return "obs-test"

    def log_event(self, **kwargs):
        self.events.append({"method": "log_event", **kwargs})
        return "event-test"

    def log_error(self, **kwargs):
        self.events.append({"method": "log_error", **kwargs})
        return "error-test"

    def finish_run(self, **kwargs):
        self.events.append({"method": "finish_run", **kwargs})

    def finish_open_run_for_conversation(self, **kwargs):
        self.events.append({"method": "finish_open_run_for_conversation", **kwargs})


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        if stream_callback is not None:
            await stream_callback("thinking delta")
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="call-1",
                        tool_name="echo_tool",
                        arguments={"text": "hello"},
                    )
                ],
                finish_reason="tool_calls",
            )
        return final_answer_response("All done.")


def test_turn_loop_accepts_observability_port():
    registry = ToolRegistry()
    observability = FakeObservability()

    loop = TurnLoop(
        llm_client=FakeLLM(),
        registry=registry,
        memory=FakeMemory(),
        observability=observability,
    )

    assert loop.observability is observability


class SlowLLM:
    """LLM that takes longer than the configured timeout."""

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        await asyncio.sleep(999)
        return LLMResponse(content="never", tool_calls=[], finish_reason="stop")


class SingleShotLLM:
    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        return final_answer_response("Done.")


class LongFinalAnswerLLM:
    answer = "This is a final answer that should stream in fast typewriter chunks."

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        return final_answer_response(self.answer)


class SlowToolCallLLM:
    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    call_id="slow-1",
                    tool_name="slow_tool",
                    arguments={"text": "wait"},
                )
            ],
            finish_reason="tool_calls",
        )


class LengthLimitedLLM:
    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        return LLMResponse(content="Partial answer", tool_calls=[], finish_reason="length")


class TextOnlyFinalLLM:
    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        return LLMResponse(content="I'm Monaw, your local AI agent.", tool_calls=[], finish_reason="stop")


class JsonTextFinalLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        return LLMResponse(
            content=json.dumps({"answer": "Finished from JSON text."}),
            tool_calls=[],
            finish_reason="stop",
        )


class FinalAnswerToolLLM:
    provider = "openai"

    def __init__(self) -> None:
        self.seen_tools: list[list[dict]] = []
        self.seen_system_prompts: list[str] = []

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.seen_tools.append(tools)
        self.seen_system_prompts.append(system_prompt)
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    call_id="call-final",
                    tool_name="final_answer",
                    arguments={"answer": "Finished via final tool."},
                )
            ],
            finish_reason="tool_calls",
        )


class CompletionRepromptLLM:
    provider = "openai"

    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    async def chat_with_tools(
        self,
        messages,
        tools,
        system_prompt="",
        stream_callback=None,
    ):  # noqa: ARG002
        self.calls += 1
        self.seen_messages.append([dict(message) for message in messages])
        if self.calls == 1:
            return LLMResponse(
                content="Done — saved the evidence screenshots.",
                tool_calls=[],
                finish_reason="stop",
            )
        return final_answer_response("Saved the evidence screenshots.")


class PendingSummaryThenGenericFinalLLM:
    provider = "openai"

    summary = (
        "## FIG summary\n\n"
        "- The move was driven by renewed investor demand after the latest company update.\n"
        "- The bullish case depends on revenue growth, margin expansion, and follow-through volume.\n"
        "- The bearish case is that the move is already crowded and can retrace if news flow fades.\n"
        "- Near term, the key watch points are volume, support around the breakout area, and fresh catalysts."
    )
    generic_final = "OK."

    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    async def chat_with_tools(
        self,
        messages,
        tools,
        system_prompt="",
        stream_callback=None,
        tool_choice=None,
    ):  # noqa: ARG002
        self.calls += 1
        self.seen_messages.append([dict(message) for message in messages])
        if self.calls == 1:
            return LLMResponse(
                content=self.summary,
                tool_calls=[],
                finish_reason="stop",
            )
        return final_answer_response(self.generic_final)


class TextWithoutToolLLM:
    provider = "openai"

    def __init__(self) -> None:
        self.calls = 0
        self.tool_choices: list[str | dict | None] = []

    async def chat_with_tools(  # noqa: ARG002
        self,
        messages,
        tools,
        system_prompt="",
        stream_callback=None,
        tool_choice=None,
    ):
        self.calls += 1
        self.tool_choices.append(tool_choice)
        return LLMResponse(content="Let me take another screenshot.", tool_calls=[], finish_reason="stop")


class TextOnlyThenForcedFinalLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.tool_choices: list[str | dict | None] = []

    async def chat_with_tools(  # noqa: ARG002
        self,
        messages,
        tools,
        system_prompt="",
        stream_callback=None,
        tool_choice=None,
    ):
        self.calls += 1
        self.tool_choices.append(tool_choice)
        if tool_choice == "required":
            return final_answer_response("Finished after forced tool choice.")
        return LLMResponse(
            content=f"Searching without a tool call {self.calls}.",
            tool_calls=[],
            finish_reason="stop",
        )


class TextOnlyIgnoresRequiredLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.tool_choices: list[str | dict | None] = []

    async def chat_with_tools(  # noqa: ARG002
        self,
        messages,
        tools,
        system_prompt="",
        stream_callback=None,
        tool_choice=None,
    ):
        self.calls += 1
        self.tool_choices.append(tool_choice)
        return LLMResponse(
            content=f"Still narrating without a tool call {self.calls}.",
            tool_calls=[],
            finish_reason="stop",
        )


class TextThenToolLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        self.seen_messages.append(list(messages))
        if self.calls == 1:
            return LLMResponse(content="I'll open the browser now.", tool_calls=[], finish_reason="stop")
        if self.calls == 2:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="browser-open-1",
                        tool_name="browser_open",
                        arguments={"url": "https://example.com"},
                    )
                ],
                finish_reason="tool_calls",
            )
        return final_answer_response("Done.")


class GreetingThenFinalAnswerLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        assert stream_callback is None
        if self.calls == 1:
            return LLMResponse(content="I'm doing well, thanks for asking!", tool_calls=[], finish_reason="stop")
        return final_answer_response("I'm ready. What's the task?")


class ToolThenIntentThenToolLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="echo-1",
                        tool_name="echo_tool",
                        arguments={"text": "first"},
                    )
                ],
                finish_reason="tool_calls",
            )
        if self.calls == 2:
            return LLMResponse(
                content="I don't see a search bar in the current view. Let me inspect the DOM for a search element.",
                tool_calls=[],
                finish_reason="stop",
            )
        if self.calls == 3:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="echo-2",
                        tool_name="echo_tool",
                        arguments={"text": "second"},
                    )
                ],
                finish_reason="tool_calls",
            )
        return final_answer_response("Done.")


class ToolThenRepeatedIntentLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.tool_choices: list[str | dict | None] = []

    async def chat_with_tools(  # noqa: ARG002
        self,
        messages,
        tools,
        system_prompt="",
        stream_callback=None,
        tool_choice=None,
    ):
        self.calls += 1
        self.tool_choices.append(tool_choice)
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="echo-1",
                        tool_name="echo_tool",
                        arguments={"text": "first"},
                    )
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content="Let me power through the rest now. I'll check Malay Mail, NST, The Edge, and Bernama.",
            tool_calls=[],
            finish_reason="stop",
        )


class ToolThenDoneTextLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="echo-1",
                        tool_name="echo_tool",
                        arguments={"text": "captured"},
                    )
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content=(
                "Done — I captured and saved the evidence screenshots. "
                "If you want, I can summarize the key facts next."
            ),
            tool_calls=[],
            finish_reason="stop",
        ) if self.calls == 2 else final_answer_response("Done.")


class ToolThenRepeatedDoneTextLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="echo-1",
                        tool_name="echo_tool",
                        arguments={"text": "captured"},
                    )
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content="Done — I captured and saved the evidence screenshots.",
            tool_calls=[],
            finish_reason="stop",
        )


class ToolThenDoneButPendingActionLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="echo-1",
                        tool_name="echo_tool",
                        arguments={"text": "typed"},
                    )
                ],
                finish_reason="tool_calls",
            )
        if self.calls == 2:
            return LLMResponse(content="Done typing. Let me send it.", tool_calls=[], finish_reason="stop")
        return final_answer_response("Sent.")


class WebSearchGenericThenConcreteLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        self.seen_messages.append(list(messages))
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="web-search-1",
                        tool_name="web_search",
                        arguments={"query": "latest news United States 2026"},
                    )
                ],
                finish_reason="tool_calls",
            )
        if self.calls == 2:
            return final_answer_response("This is the latest US news result for you.")
        return final_answer_response(
            "Latest US news:\n"
            "- Reuters: Latest U.S. News | Top headlines from the USA - "
            "https://www.reuters.com/world/us/ - May 8, 2026 headlines and updates."
        )


class AlwaysToolLLM:
    def __init__(self, tool_name: str = "echo_tool") -> None:
        self.calls = 0
        self.tool_name = tool_name

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    call_id=f"always-tool-{self.calls}",
                    tool_name=self.tool_name,
                    arguments={"text": f"value-{self.calls}"},
                )
            ],
            finish_reason="tool_calls",
        )


class PromptRecordingLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.prompts.append(system_prompt)
        if len(self.prompts) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="call-refresh",
                        tool_name="refresh_tools",
                        arguments={},
                    )
                ],
                finish_reason="tool_calls",
            )
        return final_answer_response("Done.")


class ScreenshotToolLLM:
    def __init__(
        self,
        *,
        final_answer: str = "Screenshot inspected.",
        final_attachment_paths: list[str] | None = None,
    ) -> None:
        self.calls = 0
        self.seen_messages: list[list[dict]] = []
        self.final_answer = final_answer
        self.final_attachment_paths = final_attachment_paths

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        self.seen_messages.append(messages)
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="shot-1",
                        tool_name="browser_screenshot",
                        arguments={},
                    )
                ],
                finish_reason="tool_calls",
            )
        return final_answer_response(
            self.final_answer,
            attachment_paths=self.final_attachment_paths,
        )


class RepeatBrowserLLM:
    def __init__(self, tool_name: str = "browser_snapshot", arguments: dict | None = None) -> None:
        self.calls = 0
        self.tool_name = tool_name
        self.arguments = arguments or {}

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        if self.calls > 2:
            return final_answer_response("Recovered after inspection.")
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    call_id=f"repeat-{self.calls}",
                    tool_name=self.tool_name,
                    arguments=self.arguments,
                )
            ],
            finish_reason="tool_calls",
        )


class RepeatGenericToolLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        self.seen_messages.append(messages)
        if self.calls > 3:
            return final_answer_response("Stopped repeating.")
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    call_id=f"repeat-tool-{self.calls}",
                    tool_name="echo_tool",
                    arguments={"text": "same"},
                )
            ],
            finish_reason="tool_calls",
        )


class AlwaysRepeatGenericToolLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    call_id=f"repeat-tool-{self.calls}",
                    tool_name="echo_tool",
                    arguments={"text": "same"},
                )
            ],
            finish_reason="tool_calls",
        )


class RepeatBlockedToolLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        if self.calls > 3:
            return final_answer_response("Should not be reached.")
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    call_id=f"blocked-tool-{self.calls}",
                    tool_name="blocked_tool",
                    arguments={"target": "same"},
                )
            ],
            finish_reason="tool_calls",
        )


class BrowserInspectThenRetryLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="click-1",
                        tool_name="browser_click",
                        arguments={"ref": "b22"},
                    )
                ],
                finish_reason="tool_calls",
            )
        if self.calls == 2:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="snapshot-1",
                        tool_name="browser_snapshot",
                        arguments={},
                    )
                ],
                finish_reason="tool_calls",
            )
        if self.calls == 3:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="click-2",
                        tool_name="browser_click",
                        arguments={"ref": "b22"},
                    )
                ],
                finish_reason="tool_calls",
            )
        return final_answer_response("Retried after inspection.")


class TimeoutBrowserLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="timeout-1",
                        tool_name="browser_snapshot",
                        arguments={},
                    )
                ],
                finish_reason="tool_calls",
            )
        return final_answer_response("Handled timeout.")


def test_turn_loop_executes_tool_and_finishes():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    loop = TurnLoop(llm_client=FakeLLM(), registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("say hello", "conv-1", system_prompt="system")]

    events = asyncio.run(collect())
    event_names = [event["event"] for event in events]

    assert "tool_start" in event_names
    assert "tool_end" in event_names
    assert events[-1]["event"] == "done"
    assert memory.tool_results == [("echo_tool", "echo:hello")]
    assert memory.added_messages[-1] == ("assistant", "All done.")


def test_turn_loop_overlaps_safe_tools_and_preserves_model_result_order():
    first_entered = threading.Event()
    second_entered = threading.Event()

    def first_tool() -> str:
        first_entered.set()
        return "first" if second_entered.wait(1.0) else "first:serial"

    def second_tool() -> str:
        second_entered.set()
        return "second" if first_entered.wait(1.0) else "second:serial"

    registry = ToolRegistry(
        [
            {
                "name": "first_tool",
                "parameters": {"type": "object", "properties": {}},
                "callable": first_tool,
                "execution_mode": "sync_stateless",
                "metadata": {"parallel_safe": True, "resource_locks": []},
            },
            {
                "name": "second_tool",
                "parameters": {"type": "object", "properties": {}},
                "callable": second_tool,
                "execution_mode": "sync_stateless",
                "metadata": {"parallel_safe": True, "resource_locks": []},
            },
        ]
    )

    class TwoToolLLM:
        calls = 0

        async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(call_id="first", tool_name="first_tool", arguments={}),
                        ToolCallRequest(call_id="second", tool_name="second_tool", arguments={}),
                    ],
                    finish_reason="tool_calls",
                )
            return final_answer_response("Finished.")

    memory = PersistingFakeMemory()
    loop = TurnLoop(
        llm_client=TwoToolLLM(),
        registry=registry,
        memory=memory,
        max_parallel_tool_calls=2,
    )

    async def collect():
        return [event async for event in loop.run("run both", "conv-parallel", system_prompt="system")]

    events = asyncio.run(collect())
    tool_end_events = [event for event in events if event["event"] == "tool_end"]

    assert [event["data"]["call_id"] for event in tool_end_events] == ["first", "second"]
    assert [event["data"]["output"] for event in tool_end_events] == ["first", "second"]
    assert [call["tool_name"] for call in memory.persisted_turns[0]["tool_calls"]] == [
        "first_tool",
        "second_tool",
    ]


def test_turn_loop_bounds_safe_tool_concurrency():
    active = 0
    maximum_active = 0
    active_lock = threading.Lock()

    def bounded_tool(label: str) -> str:
        nonlocal active, maximum_active
        with active_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.04)
            return label
        finally:
            with active_lock:
                active -= 1

    registry = ToolRegistry(
        [
            {
                "name": "bounded_tool",
                "parameters": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                    "required": ["label"],
                },
                "callable": bounded_tool,
                "execution_mode": "sync_stateless",
                "metadata": {"parallel_safe": True, "resource_locks": []},
            }
        ]
    )

    class ManyToolLLM:
        calls = 0

        async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            call_id=f"call-{index}",
                            tool_name="bounded_tool",
                            arguments={"label": str(index)},
                        )
                        for index in range(5)
                    ],
                    finish_reason="tool_calls",
                )
            return final_answer_response("Finished.")

    loop = TurnLoop(
        llm_client=ManyToolLLM(),
        registry=registry,
        memory=FakeMemory(),
        max_parallel_tool_calls=2,
    )

    async def collect():
        return [event async for event in loop.run("run several", "conv-bounded", system_prompt="system")]

    asyncio.run(collect())

    assert 1 < maximum_active <= 2


def test_turn_loop_isolates_parallel_tool_errors():
    def maybe_fail(mode: str) -> str:
        if mode == "fail":
            raise RuntimeError("expected failure")
        return "ok"

    registry = ToolRegistry(
        [
            {
                "name": "maybe_fail",
                "parameters": {
                    "type": "object",
                    "properties": {"mode": {"type": "string"}},
                    "required": ["mode"],
                },
                "callable": maybe_fail,
                "execution_mode": "sync_stateless",
                "metadata": {"parallel_safe": True, "resource_locks": []},
            }
        ]
    )

    class ErrorIsolationLLM:
        calls = 0

        async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(call_id="bad", tool_name="maybe_fail", arguments={"mode": "fail"}),
                        ToolCallRequest(call_id="good", tool_name="maybe_fail", arguments={"mode": "ok"}),
                    ],
                    finish_reason="tool_calls",
                )
            return final_answer_response("Finished.")

    memory = PersistingFakeMemory()
    loop = TurnLoop(llm_client=ErrorIsolationLLM(), registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("run both", "conv-error-isolation", system_prompt="system")]

    events = asyncio.run(collect())
    tool_end_events = [event for event in events if event["event"] == "tool_end"]

    assert [event["data"]["status"] for event in tool_end_events] == ["error", "ok"]
    assert [call["status"] for call in memory.persisted_turns[0]["tool_calls"]] == ["error", "complete"]


def test_turn_loop_orders_invalid_safe_call_between_valid_results():
    def echo(label: str) -> str:
        return label

    registry = ToolRegistry(
        [
            {
                "name": "echo",
                "parameters": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                    "required": ["label"],
                },
                "callable": echo,
                "execution_mode": "sync_stateless",
                "metadata": {"parallel_safe": True, "resource_locks": []},
            }
        ]
    )

    class MixedValidityLLM:
        calls = 0

        async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(call_id="valid-1", tool_name="echo", arguments={"label": "one"}),
                        ToolCallRequest(call_id="invalid", tool_name="echo", arguments={}),
                        ToolCallRequest(call_id="valid-2", tool_name="echo", arguments={"label": "two"}),
                    ],
                    finish_reason="tool_calls",
                )
            return final_answer_response("Finished.")

    memory = PersistingFakeMemory()
    loop = TurnLoop(llm_client=MixedValidityLLM(), registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("run mixed", "conv-mixed-validity", system_prompt="system")]

    events = asyncio.run(collect())
    tool_end_events = [event for event in events if event["event"] == "tool_end"]

    assert [event["data"]["call_id"] for event in tool_end_events] == ["valid-1", "invalid", "valid-2"]
    assert json.loads(tool_end_events[1]["data"]["output"])["reason_code"] == "invalid_tool_arguments"


def test_turn_loop_cancels_and_drains_all_parallel_tool_tasks():
    started: set[str] = set()
    cancelled: set[str] = set()
    all_started = asyncio.Event()

    async def slow_tool(label: str) -> str:
        started.add(label)
        if len(started) == 2:
            all_started.set()
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            cancelled.add(label)
            raise
        return label

    registry = ToolRegistry(
        [
            {
                "name": "slow_parallel",
                "parameters": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                    "required": ["label"],
                },
                "callable": slow_tool,
                "execution_mode": "async",
                "metadata": {"parallel_safe": True, "resource_locks": []},
            }
        ]
    )

    class SlowParallelLLM:
        async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(call_id="slow-a", tool_name="slow_parallel", arguments={"label": "a"}),
                    ToolCallRequest(call_id="slow-b", tool_name="slow_parallel", arguments={"label": "b"}),
                ],
                finish_reason="tool_calls",
            )

    memory = PersistingFakeMemory()
    loop = TurnLoop(llm_client=SlowParallelLLM(), registry=registry, memory=memory)

    async def collect_until_cancelled():
        stream = loop.run("start parallel work", "conv-parallel-cancel", system_prompt="system")
        events = []
        try:
            while True:
                event = await stream.__anext__()
                events.append(event)
                if event["event"] == "tool_start" and len(
                    [item for item in events if item["event"] == "tool_start"]
                ) == 2:
                    await asyncio.wait_for(all_started.wait(), timeout=1.0)
                    break
        finally:
            await stream.aclose()
        return events, not any(
            task.get_name().startswith("turn-tool-")
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        )

    events, no_orphans = asyncio.run(collect_until_cancelled())

    assert events[-1]["event"] == "tool_start"
    assert cancelled == {"a", "b"}
    assert no_orphans
    persisted = memory.persisted_turns[0]["tool_calls"]
    assert [json.loads(call["input"])["label"] for call in persisted] == ["a", "b"]
    assert [call["status"] for call in persisted] == ["cancelled", "cancelled"]


def test_turn_loop_preserves_completed_parallel_sibling_when_other_is_cancelled():
    started: set[str] = set()
    cancelled: set[str] = set()
    all_started = asyncio.Event()
    completed = asyncio.Event()

    async def mixed_tool(label: str) -> str:
        started.add(label)
        if len(started) == 2:
            all_started.set()
        if label == "done":
            await asyncio.sleep(0.02)
            completed.set()
            return "done"
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            cancelled.add(label)
            raise
        return label

    registry = ToolRegistry(
        [
            {
                "name": "mixed_parallel",
                "parameters": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                    "required": ["label"],
                },
                "callable": mixed_tool,
                "execution_mode": "async",
                "metadata": {"parallel_safe": True, "resource_locks": []},
            }
        ]
    )

    class MixedParallelLLM:
        async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(call_id="done", tool_name="mixed_parallel", arguments={"label": "done"}),
                    ToolCallRequest(call_id="hang", tool_name="mixed_parallel", arguments={"label": "hang"}),
                ],
                finish_reason="tool_calls",
            )

    memory = PersistingFakeMemory()
    loop = TurnLoop(llm_client=MixedParallelLLM(), registry=registry, memory=memory)

    async def collect_until_cancelled():
        stream = loop.run("finish one", "conv-completed-sibling", system_prompt="system")
        events = []
        try:
            while True:
                event = await stream.__anext__()
                events.append(event)
                if event["event"] == "tool_start" and len(
                    [item for item in events if item["event"] == "tool_start"]
                ) == 2:
                    await asyncio.wait_for(all_started.wait(), timeout=1.0)
                    await asyncio.wait_for(completed.wait(), timeout=1.0)
                    break
        finally:
            await stream.aclose()
        return events, not any(
            task.get_name().startswith("turn-tool-")
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        )

    events, no_orphans = asyncio.run(collect_until_cancelled())

    assert events[-1]["event"] == "tool_start"
    assert cancelled == {"hang"}
    assert no_orphans
    persisted = memory.persisted_turns[0]["tool_calls"]
    assert [json.loads(call["input"])["label"] for call in persisted] == ["done", "hang"]
    assert [call["status"] for call in persisted] == ["complete", "cancelled"]
    assert persisted[0]["output"] == "done"


def test_turn_loop_propagates_conversation_context_to_sync_tools():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: json.dumps(
                    {"status": "ok", "conversation_id": current_conversation_id()}
                ),
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    loop = TurnLoop(llm_client=FakeLLM(), registry=registry, memory=FakeMemory())

    async def collect():
        return [event async for event in loop.run("say hello", "conv-context-tool", system_prompt="system")]

    events = asyncio.run(collect())
    tool_end = next(event for event in events if event["event"] == "tool_end")
    output = json.loads(tool_end["data"]["output"])

    assert output["conversation_id"] == "conv-context-tool"


def test_turn_loop_trusts_text_final_when_no_tool_call_is_emitted():
    registry = ToolRegistry([])
    memory = FakeMemory()
    loop = TurnLoop(llm_client=TextOnlyFinalLLM(), registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("continue browser task", "conv-text-final", system_prompt="system")]

    events = asyncio.run(collect())

    assert not any(event["event"] == "tool_start" for event in events)
    assert events[-1]["event"] == "done"
    assert events[-1]["data"].get("incomplete") is not True
    assert memory.added_messages[-1] == ("assistant", "I'm Monaw, your local AI agent.")


def test_turn_loop_still_accepts_final_answer_tool_when_model_emits_it():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    llm = FinalAnswerToolLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("finish task", "conv-final-answer-tool", system_prompt="system")]

    events = asyncio.run(collect())

    assert any(tool["name"] == "final_answer" for tool in llm.seen_tools[0])
    assert "call `final_answer`" in llm.seen_system_prompts[0]
    assert not any(event["event"] == "tool_start" for event in events)
    assert events[-1]["data"]["summary"] == "Finished via final tool."
    assert events[-1]["data"].get("incomplete") is not True
    assert memory.added_messages[-1] == ("assistant", "Finished via final tool.")


def test_turn_loop_typewrites_final_answer_tool_response():
    registry = ToolRegistry([])
    memory = PersistingFakeMemory()
    llm = LongFinalAnswerLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("finish task", "conv-typewriter", system_prompt="system")]

    events = asyncio.run(collect())
    token_chunks = [
        event["data"]["content"]
        for event in events
        if event["event"] == "token"
    ]

    assert len(token_chunks) > 1
    assert "".join(token_chunks) == llm.answer
    assert events[-1]["data"]["summary"] == llm.answer
    assert events[-1]["data"]["response_duration_ms"] >= 0
    assert memory.persisted_turns[-1]["response_duration_ms"] >= 0


def test_turn_loop_reprompts_explicit_completion_text_to_call_final_answer():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    llm = CompletionRepromptLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("finish task", "conv-completion-reprompt", system_prompt="system")]

    events = asyncio.run(collect())

    assert llm.calls == 2
    assert any(
        msg.get("role") == "assistant"
        and "saved the evidence screenshots" in msg.get("content", "")
        for msg in llm.seen_messages[1]
    )
    assert any(
        msg.get("role") == "user"
        and "exact previous assistant message" in msg.get("content", "")
        and "Do not summarize, shorten, rewrite, translate, add to, or remove anything" in msg.get("content", "")
        for msg in llm.seen_messages[1]
    )
    assert not any(event["event"] == "progress" for event in events)
    assert events[-1]["data"]["summary"] == "Saved the evidence screenshots."


def test_turn_loop_uses_pending_plain_final_when_final_answer_is_generic():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = PersistingFakeMemory()
    llm = PendingSummaryThenGenericFinalLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("summarize FIG", "conv-pending-final", system_prompt="system")]

    events = asyncio.run(collect())
    progress_events = [event for event in events if event["event"] == "progress"]

    assert llm.calls == 2
    assert progress_events[0]["data"]["content"] == llm.summary
    assert any(
        msg.get("role") == "user"
        and "exact previous assistant message" in msg.get("content", "")
        for msg in llm.seen_messages[1]
    )
    assert not any(event["event"] == "tool_start" for event in events)
    assert events[-1]["data"]["status"] == "complete"
    assert events[-1]["data"]["summary"] == llm.summary
    assert memory.persisted_turns[-1]["assistant_message"] == llm.summary


def test_turn_loop_accepts_json_text_matching_final_answer_schema():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    llm = JsonTextFinalLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("answer a question", "conv-json-final", system_prompt="system")]

    events = asyncio.run(collect())

    assert llm.calls == 1
    assert not any(event["event"] == "progress" for event in events)
    assert not any(event["event"] == "tool_start" for event in events)
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["summary"] == "Finished from JSON text."
    assert events[-1]["data"].get("incomplete") is not True
    assert memory.added_messages[-1] == ("assistant", "Finished from JSON text.")


def test_turn_loop_continues_text_only_progress_until_iteration_budget():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    llm = TextWithoutToolLLM()
    loop = TurnLoop(
        llm_client=llm,
        registry=registry,
        memory=memory,
        budget=IterationBudget(max_iterations=3),
        max_iterations=10,
    )

    async def collect():
        return [event async for event in loop.run("continue browser task", "conv-text-without-tool", system_prompt="system")]

    events = asyncio.run(collect())
    done_event = next(event for event in events if event["event"] == "done")
    error_event = next(event for event in events if event["event"] == "error")
    progress_events = [event for event in events if event["event"] == "progress"]

    assert llm.calls == 3
    assert llm.tool_choices == [None, None, "required"]
    assert len(progress_events) == 3
    assert error_event["data"]["code"] == "iteration_budget_exhausted"
    assert done_event["data"]["status"] == "paused"
    assert done_event["data"]["incomplete"] is True
    assert done_event["data"]["reason_code"] == "iteration_budget_exhausted"
    assert memory.clear_task_progress_calls == 0


def test_turn_loop_forces_tool_choice_after_repeated_text_only_progress():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    llm = TextOnlyThenForcedFinalLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("search now", "conv-force-tool-choice", system_prompt="system")]

    events = asyncio.run(collect())
    progress_events = [event for event in events if event["event"] == "progress"]

    assert llm.calls == 3
    assert llm.tool_choices == [None, None, "required"]
    assert [event["data"]["content"] for event in progress_events] == [
        "Searching without a tool call 1.",
        "Searching without a tool call 2.",
    ]
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["status"] == "complete"
    assert events[-1]["data"]["summary"] == "Finished after forced tool choice."
    assert memory.clear_task_progress_calls == 1


def test_turn_loop_circuit_breaks_repeated_text_only_progress():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    llm = TextOnlyIgnoresRequiredLLM()
    loop = TurnLoop(
        llm_client=llm,
        registry=registry,
        memory=memory,
        budget=IterationBudget(max_iterations=10),
        max_iterations=10,
    )

    async def collect():
        return [event async for event in loop.run("search now", "conv-text-circuit-break", system_prompt="system")]

    events = asyncio.run(collect())
    progress_events = [event for event in events if event["event"] == "progress"]
    error_events = [event for event in events if event["event"] == "error"]

    assert llm.calls == 4
    assert llm.tool_choices == [None, None, "required", "required"]
    assert len(progress_events) == 4
    assert error_events == []
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["status"] == "complete"
    assert events[-1]["data"].get("incomplete") is not True
    assert events[-1]["data"]["summary"] == "Still narrating without a tool call 4."
    assert memory.clear_task_progress_calls == 1


def test_turn_loop_reprompts_once_when_model_returns_intent_text_before_tool_call():
    registry = ToolRegistry(
        [
            {
                "name": "browser_open",
                "description": "Open URL",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
                "callable": lambda url: json.dumps({"status": "ok", "url": url}),
                "domain": "browser",
                "execution_mode": "sync_stateless",
                "affinity_group": "browser-use",
            }
        ]
    )
    memory = FakeMemory()
    llm = TextThenToolLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("open the browser to example.com", "conv-text-then-tool", system_prompt="system")]

    events = asyncio.run(collect())
    progress_events = [event for event in events if event["event"] == "progress"]

    assert llm.calls == 3
    assert progress_events[0]["data"]["content"] == "I'll open the browser now."
    assert any(
        msg.get("role") == "assistant" and "I'll open the browser now." in msg.get("content", "")
        for msg in llm.seen_messages[1]
    )
    assert any(event["event"] == "tool_start" for event in events)
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["summary"] == "Done."


def test_turn_loop_displays_plain_text_before_final_answer_reprompt():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    llm = GreetingThenFinalAnswerLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("Hey, how are you?", "conv-greeting", system_prompt="system")]

    events = asyncio.run(collect())
    progress_events = [event for event in events if event["event"] == "progress"]

    assert llm.calls == 2
    assert [event["data"]["content"] for event in progress_events] == ["I'm doing well, thanks for asking!"]
    assert not any(event["event"] == "thinking" for event in events)
    token_chunks = [event["data"]["content"] for event in events if event["event"] == "token"]
    assert len(token_chunks) > 1
    assert "".join(token_chunks) == "I'm ready. What's the task?"
    assert events[-1]["data"]["summary"] == "I'm ready. What's the task?"


def test_turn_loop_reprompts_when_model_returns_intent_text_after_tool_call():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    llm = ToolThenIntentThenToolLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("check several news sites", "conv-post-tool-intent", system_prompt="system")]

    events = asyncio.run(collect())
    tool_start_events = [event for event in events if event["event"] == "tool_start"]
    progress_events = [event for event in events if event["event"] == "progress"]

    assert llm.calls == 4
    assert len(tool_start_events) == 2
    assert progress_events[0]["data"]["content"].startswith("I don't see a search bar")
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["status"] == "complete"
    assert events[-1]["data"].get("incomplete") is not True


def test_turn_loop_keeps_reprompting_post_tool_progress_until_iteration_budget():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    llm = ToolThenRepeatedIntentLLM()
    loop = TurnLoop(
        llm_client=llm,
        registry=registry,
        memory=memory,
        budget=IterationBudget(max_iterations=4),
        max_iterations=10,
    )

    async def collect():
        return [event async for event in loop.run("check several news sites", "conv-post-tool-intent-pause", system_prompt="system")]

    events = asyncio.run(collect())
    done_event = next(event for event in events if event["event"] == "done")
    error_event = next(event for event in events if event["event"] == "error")
    progress_events = [event for event in events if event["event"] == "progress"]

    assert llm.calls == 4
    assert llm.tool_choices == [None, None, None, "required"]
    assert len(progress_events) == 3
    assert error_event["data"]["code"] == "iteration_budget_exhausted"
    assert done_event["data"]["status"] == "paused"
    assert done_event["data"]["incomplete"] is True
    assert done_event["data"]["reason_code"] == "iteration_budget_exhausted"
    assert memory.clear_task_progress_calls == 0


def test_turn_loop_accepts_explicit_done_text_as_final_after_tools():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    llm = ToolThenDoneTextLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("capture evidence", "conv-done-text", system_prompt="system")]

    events = asyncio.run(collect())
    progress_events = [event for event in events if event["event"] == "progress"]
    done_event = next(event for event in events if event["event"] == "done")

    assert llm.calls == 3
    assert progress_events == []
    assert done_event["data"]["status"] == "complete"
    assert done_event["data"].get("incomplete") is not True
    assert done_event["data"]["summary"] == "Done."
    assert memory.clear_task_progress_calls == 1


def test_turn_loop_accepts_repeated_explicit_done_text_as_loop_breaker():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    llm = ToolThenRepeatedDoneTextLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("capture evidence", "conv-repeated-done-text", system_prompt="system")]

    events = asyncio.run(collect())
    done_event = next(event for event in events if event["event"] == "done")

    assert llm.calls == 3
    assert done_event["data"]["status"] == "complete"
    assert done_event["data"]["summary"].startswith("Done")


def test_turn_loop_does_not_accept_done_text_with_pending_action_as_final():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    llm = ToolThenDoneButPendingActionLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("type and send", "conv-done-pending", system_prompt="system")]

    events = asyncio.run(collect())
    progress_events = [event for event in events if event["event"] == "progress"]
    done_event = next(event for event in events if event["event"] == "done")

    assert llm.calls == 3
    assert progress_events[0]["data"]["content"] == "Done typing. Let me send it."
    assert done_event["data"]["status"] == "complete"
    assert done_event["data"]["summary"] == "Sent."


def test_turn_loop_reprompts_generic_web_search_final_answer():
    search_output = str(
        {
            "query": "latest news United States 2026",
            "answer": None,
            "results": [
                {
                    "title": "Latest U.S. News | Top headlines from the USA - Reuters",
                    "url": "https://www.reuters.com/world/us/",
                    "content": "May 8, 2026. New York Mayor Mamdani's freeze the rent promise...",
                }
            ],
        }
    )
    registry = ToolRegistry(
        [
            {
                "name": "web_search",
                "description": "Search the web",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "callable": lambda query: search_output,
                "domain": "web",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = PersistingFakeMemory()
    llm = WebSearchGenericThenConcreteLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("latest US news", "conv-web-search-final", system_prompt="system")]

    events = asyncio.run(collect())

    assert llm.calls == 3
    assert any(
        msg.get("role") == "user" and "actual web_search results" in msg.get("content", "")
        for msg in llm.seen_messages[-1]
    )
    assert "Reuters" in events[-1]["data"]["summary"]
    assert "https://www.reuters.com/world/us/" in events[-1]["data"]["summary"]
    assert memory.persisted_turns[-1]["response_duration_ms"] is not None


def test_turn_loop_pauses_when_model_output_is_truncated():
    registry = ToolRegistry([])
    memory = FakeMemory()
    loop = TurnLoop(llm_client=LengthLimitedLLM(), registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("write a long report", "conv-length", system_prompt="system")]

    events = asyncio.run(collect())
    error_event = next(event for event in events if event["event"] == "error")
    done_event = next(event for event in events if event["event"] == "done")

    assert error_event["data"]["code"] == "model_output_truncated"
    assert done_event["data"]["status"] == "paused"
    assert done_event["data"]["incomplete"] is True
    assert done_event["data"]["reason_code"] == "model_output_truncated"
    assert memory.clear_task_progress_calls == 0


def test_turn_loop_reports_iteration_limit_after_last_tool_result():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    loop = TurnLoop(
        llm_client=AlwaysToolLLM(),
        registry=registry,
        memory=memory,
        budget=IterationBudget(max_iterations=2),
        max_iterations=1,
    )

    async def collect():
        return [event async for event in loop.run("finish task", "conv-limit-after-tool", system_prompt="system")]

    events = asyncio.run(collect())
    error_events = [event for event in events if event["event"] == "error"]
    done_event = next(event for event in events if event["event"] == "done")

    assert error_events[-1]["data"]["code"] == "iteration_limit_reached_after_tool"
    assert done_event["data"]["summary"] != "Done."
    assert done_event["data"]["incomplete"] is True
    assert done_event["data"]["reason_code"] == "iteration_limit_reached_after_tool"
    assert memory.clear_task_progress_calls == 0


def test_turn_loop_pauses_before_tools_when_no_final_reasoning_turn_remains():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    loop = TurnLoop(
        llm_client=AlwaysToolLLM(),
        registry=registry,
        memory=memory,
        max_iterations=2,
    )

    async def collect():
        return [event async for event in loop.run("finish task", "conv-limit-before-tools", system_prompt="system")]

    events = asyncio.run(collect())
    error_events = [event for event in events if event["event"] == "error"]
    tool_start_events = [event for event in events if event["event"] == "tool_start"]
    done_event = next(event for event in events if event["event"] == "done")

    assert len(tool_start_events) == 1
    assert error_events[-1]["data"]["code"] == "iteration_limit_reached_before_more_tools"
    assert done_event["data"]["summary"] != "Done."
    assert done_event["data"]["incomplete"] is True
    assert done_event["data"]["reason_code"] == "iteration_limit_reached_before_more_tools"
    assert memory.clear_task_progress_calls == 0


def test_turn_loop_clears_task_progress_on_normal_completion():
    registry = ToolRegistry([])
    memory = FakeMemory()
    loop = TurnLoop(llm_client=SingleShotLLM(), registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("finish task", "conv-normal-clear", system_prompt="system")]

    events = asyncio.run(collect())

    assert events[-1]["event"] == "done"
    assert events[-1]["data"].get("incomplete") is not True
    assert memory.clear_task_progress_calls == 1


def test_turn_loop_emits_plan_and_step_events_for_tool_calls():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    loop = TurnLoop(llm_client=FakeLLM(), registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("say hello", "conv-plan", system_prompt="system")]

    events = asyncio.run(collect())
    event_names = [event["event"] for event in events]

    assert "plan" in event_names
    assert "step_start" in event_names
    assert "observation" in event_names
    assert "step_complete" in event_names
    plan_event = next(event for event in events if event["event"] == "plan")
    assert plan_event["data"]["steps"][0]["tool_hints"] == ["echo_tool"]
    complete_event = next(event for event in events if event["event"] == "step_complete")
    assert complete_event["data"]["status"] == "done"


def test_turn_loop_persists_tool_calls_when_memory_supports_persist_turn():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = PersistingFakeMemory()
    loop = TurnLoop(llm_client=FakeLLM(), registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("say hello", "conv-persist", system_prompt="system")]

    asyncio.run(collect())

    assert memory.persisted_turns[0]["user_message"] == "say hello"
    assert memory.persisted_turns[0]["assistant_message"] == "All done."
    assert memory.persisted_turns[0]["status"] == "complete"
    assert memory.persisted_turns[0]["tool_calls"][0]["tool_name"] == "echo_tool"
    assert memory.persisted_turns[0]["tool_calls"][0]["status"] == "complete"


def test_turn_loop_persists_partial_turn_when_stream_is_cancelled():
    async def slow_tool(text: str):  # noqa: ARG001
        await asyncio.sleep(999)
        return "never"

    registry = ToolRegistry(
        [
            {
                "name": "slow_tool",
                "description": "Slow tool",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": slow_tool,
                "domain": "general",
                "execution_mode": "async",
                "affinity_group": None,
            }
        ]
    )
    memory = PersistingFakeMemory()
    loop = TurnLoop(llm_client=SlowToolCallLLM(), registry=registry, memory=memory)

    async def collect_until_tool_start():
        stream = loop.run("start slow work", "conv-cancel", system_prompt="system")
        events = []
        try:
            while True:
                event = await stream.__anext__()
                events.append(event)
                if event["event"] == "tool_start":
                    break
        finally:
            await stream.aclose()
        return events

    events = asyncio.run(collect_until_tool_start())

    assert events[-1]["event"] == "tool_start"
    assert len(memory.persisted_turns) == 1
    persisted = memory.persisted_turns[0]
    assert persisted["user_message"] == "start slow work"
    assert persisted["status"] == "paused"
    assert persisted["assistant_message"].startswith("Stopped before final answer")
    assert persisted["tool_calls"][0]["tool_name"] == "slow_tool"
    assert persisted["tool_calls"][0]["status"] == "cancelled"
    assert json.loads(persisted["tool_calls"][0]["output"])["reason_code"] == "user_stopped"


def test_turn_loop_attaches_browser_screenshot_ephemerally():
    tmp_dir = Path.cwd() / ".tmp-turn-loop-screenshot"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = tmp_dir / "browser.png"
    screenshot_path.write_bytes(b"fake image bytes")
    registry = ToolRegistry(
        [
            {
                "name": "browser_screenshot",
                "description": "Screenshot",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: json.dumps({"status": "ok", "path": str(screenshot_path)}),
                "domain": "browser",
                "execution_mode": "sync_stateless",
                "affinity_group": "browser-use",
            }
        ]
    )
    llm = ScreenshotToolLLM()
    memory = PersistingFakeMemory()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("inspect browser", "conv-shot", system_prompt="system")]

    try:
        events = asyncio.run(collect())

        second_call_messages = llm.seen_messages[1]
        tool_result_message = next(message for message in second_call_messages if message.get("images"))
        assert tool_result_message["ephemeral"] is True
        assert tool_result_message["images"][0]["path"] == str(screenshot_path)
        persisted_output = memory.persisted_turns[0]["tool_calls"][0]["output"]
        assert json.loads(persisted_output)["path"] == str(screenshot_path)
        assert "images" not in persisted_output
        done_event = next(event for event in events if event["event"] == "done")
        assert done_event["data"]["attachments"] == []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_turn_loop_includes_generated_screenshot_when_user_requests_image_delivery(tmp_path: Path):
    screenshot_path = tmp_path / "browser_page_result.png"
    screenshot_path.write_bytes(png_header(536, 320))
    prior_roots = list(load_policy().permitted_roots)
    update_permitted_roots([*prior_roots, str(tmp_path)])
    registry = ToolRegistry(
        [
            {
                "name": "browser_screenshot",
                "description": "Screenshot",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: json.dumps({"status": "ok", "path": str(screenshot_path)}),
                "domain": "browser",
                "execution_mode": "sync_stateless",
                "affinity_group": "browser-use",
            }
        ]
    )
    memory = PersistingFakeMemory()
    loop = TurnLoop(
        llm_client=ScreenshotToolLLM(
            final_answer="Here is the image.",
            final_attachment_paths=[str(screenshot_path)],
        ),
        registry=registry,
        memory=memory,
    )

    async def collect():
        return [
            event
            async for event in loop.run(
                "\u628a\u7167\u7247\u53d1\u6211\u554a",
                "conv-send-shot",
                system_prompt="system",
            )
        ]

    try:
        events = asyncio.run(collect())

        done_event = next(event for event in events if event["event"] == "done")
        attachments = done_event["data"]["attachments"]
        assert [attachment["name"] for attachment in attachments] == ["browser_page_result.png"]
        assert [attachment["name"] for attachment in memory.persisted_turns[0]["response_attachments"]] == [
            "browser_page_result.png"
        ]
    finally:
        update_permitted_roots(prior_roots)


def test_turn_loop_attaches_tool_screenshots_for_the_model_to_read():
    tmp_dir = Path.cwd() / ".tmp-turn-loop-native-vision"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = tmp_dir / "browser.png"
    screenshot_path.write_bytes(b"fake image bytes")
    registry = ToolRegistry(
        [
            {
                "name": "browser_screenshot",
                "description": "Screenshot",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: json.dumps({"status": "ok", "path": str(screenshot_path)}),
                "domain": "browser",
                "execution_mode": "sync_stateless",
                "affinity_group": "browser-use",
            }
        ]
    )
    llm = ScreenshotToolLLM()
    memory = PersistingFakeMemory()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [event async for event in loop.run("inspect browser", "conv-native-vision", system_prompt="system")]

    try:
        asyncio.run(collect())

        second_call_messages = llm.seen_messages[1]
        tool_result_message = next(
            message for message in second_call_messages if message.get("images")
        )
        assert tool_result_message["images"] == [
            {"path": str(screenshot_path), "mime_type": "image/png"}
        ]
        assert tool_result_message["ephemeral"] is True
        persisted_output = memory.persisted_turns[0]["tool_calls"][0]["output"]
        assert json.loads(persisted_output)["path"] == str(screenshot_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_extract_image_attachments_supports_computer_window_state():
    output = json.dumps({"status": "ok", "screenshot": {"path": r"C:\tmp\desktop.png"}})

    assert _extract_image_attachments("computer_functions_get_window_state", output) == [
        {"path": r"C:\tmp\desktop.png", "mime_type": "image/png"}
    ]


def test_extract_image_attachments_supports_browser_full_page_screenshot():
    output = json.dumps({"status": "ok", "path": r"C:\tmp\browser-full.png", "full_page": True})

    assert _extract_image_attachments("browser_full_page_screenshot", output) == [
        {"path": r"C:\tmp\browser-full.png", "mime_type": "image/png"}
    ]


def test_browser_repeat_key_includes_target_after_state():
    base_output = {
        "status": "ok",
        "page": {"url": "https://example.test/form", "title": "Form"},
        "selector": "[data-agent-ref='name']",
        "target_after": {
            "ref": "name",
            "tag": "input",
            "role": "textbox",
            "value": "before",
        },
    }
    changed_output = {
        **base_output,
        "target_after": {
            **base_output["target_after"],
            "value": "after",
        },
    }

    assert _repeat_key("browser_type", {"ref": "name", "text": "after"}, json.dumps(base_output)) != _repeat_key(
        "browser_type",
        {"ref": "name", "text": "after"},
        json.dumps(changed_output),
    )


def test_turn_loop_allows_repeated_browser_snapshot_inspection():
    snapshot_output = json.dumps(
        {
            "status": "ok",
            "page": {"url": "about:blank", "title": ""},
            "snapshot": {"page_state": {"loading_state": "complete", "is_blank_page": True}},
        }
    )
    registry = ToolRegistry(
        [
            {
                "name": "browser_snapshot",
                "description": "Snapshot",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: snapshot_output,
                "domain": "browser",
                "execution_mode": "sync_stateless",
                "affinity_group": "browser-use",
            }
        ]
    )
    llm = RepeatBrowserLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=FakeMemory())

    async def collect():
        return [event async for event in loop.run("repeat snapshot", "conv-repeat-snapshot", system_prompt="system")]

    events = asyncio.run(collect())
    tool_end_outputs = [
        json.loads(event["data"]["output"])
        for event in events
        if event["event"] == "tool_end"
    ]

    assert llm.calls == 3
    assert all(output["status"] == "ok" for output in tool_end_outputs)
    assert events[-1]["event"] == "done"
    assert "recovered after inspection" in events[-1]["data"]["summary"].lower()


def test_turn_loop_prompts_recovery_for_repeated_mutating_browser_action_with_unchanged_state():
    click_output = json.dumps(
        {
            "status": "ok",
            "page": {"url": "https://mail.google.com/mail/u/0/#inbox", "title": "Gmail"},
        }
    )
    registry = ToolRegistry(
        [
            {
                "name": "browser_click",
                "description": "Click",
                "parameters": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]},
                "callable": lambda ref: click_output,
                "domain": "browser",
                "execution_mode": "sync_stateless",
                "affinity_group": "browser-use",
            }
        ]
    )
    llm = RepeatBrowserLLM(tool_name="browser_click", arguments={"ref": "compose"})
    loop = TurnLoop(llm_client=llm, registry=registry, memory=FakeMemory())

    async def collect():
        return [event async for event in loop.run("repeat click", "conv-repeat-click", system_prompt="system")]

    events = asyncio.run(collect())
    tool_end_outputs = [
        json.loads(event["data"]["output"])
        for event in events
        if event["event"] == "tool_end"
    ]

    assert llm.calls == 3
    assert tool_end_outputs[-1]["code"] == "stalled_repeat_detected"
    assert tool_end_outputs[-1]["recovery_required"] is True
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["status"] == "complete"
    assert "recovered after inspection" in events[-1]["data"]["summary"].lower()


def test_turn_loop_allows_repeated_browser_press_when_page_state_changes():
    outputs = [
        json.dumps(
            {
                "status": "ok",
                "key": "Escape",
                "page": {"url": f"https://example.test/post/{index}", "title": f"Post {index}"},
            }
        )
        for index in range(1, 4)
    ]
    calls = {"count": 0}

    def press_output(key: str) -> str:  # noqa: ARG001
        calls["count"] += 1
        return outputs[calls["count"] - 1]

    class RepeatPressLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
            self.calls += 1
            if self.calls > 3:
                return final_answer_response("Escaped each post.")
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id=f"press-{self.calls}",
                        tool_name="browser_press",
                        arguments={"key": "Escape"},
                    )
                ],
                finish_reason="tool_calls",
            )

    registry = ToolRegistry(
        [
            {
                "name": "browser_press",
                "description": "Press key",
                "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
                "callable": press_output,
                "domain": "browser",
                "execution_mode": "sync_stateless",
                "affinity_group": "browser-use",
            }
        ]
    )
    llm = RepeatPressLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=FakeMemory())

    async def collect():
        return [event async for event in loop.run("dismiss overlays", "conv-repeat-press", system_prompt="system")]

    events = asyncio.run(collect())
    tool_end_outputs = [
        json.loads(event["data"]["output"])
        for event in events
        if event["event"] == "tool_end"
    ]

    assert llm.calls == 4
    assert [output["status"] for output in tool_end_outputs] == ["ok", "ok", "ok"]
    assert [output["page"]["url"] for output in tool_end_outputs] == [
        "https://example.test/post/1",
        "https://example.test/post/2",
        "https://example.test/post/3",
    ]
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["status"] == "complete"
    assert events[-1]["data"]["summary"] == "Escaped each post."


def test_turn_loop_allows_retry_after_browser_inspection():
    click_output = json.dumps(
        {
            "status": "ok",
            "page": {"url": "https://mail.google.com/mail/u/0/#inbox", "title": "Gmail"},
        }
    )
    snapshot_output = json.dumps(
        {
            "status": "ok",
            "page": {"url": "https://mail.google.com/mail/u/0/#inbox", "title": "Gmail"},
            "snapshot": {"page_state": {"loading_state": "complete", "is_blank_page": False}},
        }
    )
    registry = ToolRegistry(
        [
            {
                "name": "browser_click",
                "description": "Click",
                "parameters": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]},
                "callable": lambda ref: click_output,
                "domain": "browser",
                "execution_mode": "sync_stateless",
                "affinity_group": "browser-use",
            },
            {
                "name": "browser_snapshot",
                "description": "Snapshot",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: snapshot_output,
                "domain": "browser",
                "execution_mode": "sync_stateless",
                "affinity_group": "browser-use",
            },
        ]
    )
    llm = BrowserInspectThenRetryLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=FakeMemory())

    async def collect():
        return [event async for event in loop.run("click then inspect", "conv-click-inspect", system_prompt="system")]

    events = asyncio.run(collect())
    tool_end_outputs = [
        json.loads(event["data"]["output"])
        for event in events
        if event["event"] == "tool_end"
    ]

    assert llm.calls == 4
    assert [output["status"] for output in tool_end_outputs] == ["ok", "ok", "ok"]
    assert events[-1]["event"] == "done"
    assert "retried after inspection" in events[-1]["data"]["summary"].lower()


def test_turn_loop_prompts_recovery_on_repeated_policy_blocked_tool_call():
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    llm = RepeatGenericToolLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=FakeMemory())

    async def collect():
        return [event async for event in loop.run("repeat tool", "conv-repeat-tool", system_prompt="system")]

    events = asyncio.run(collect())
    tool_end_outputs = [
        event["data"]["output"]
        for event in events
        if event["event"] == "tool_end"
    ]
    blocked_output = json.loads(tool_end_outputs[2])

    assert llm.calls == 4
    assert tool_end_outputs[0] == "echo:same"
    assert tool_end_outputs[1] == "echo:same"
    assert blocked_output["code"] == "repeated_tool_call_blocked"
    assert events[-1]["data"]["status"] == "complete"
    assert events[-1]["data"]["summary"] == "Stopped repeating."
    assert any(
        "Loop recovery checkpoint" in str(message.get("content") or "")
        for message in llm.seen_messages[-1]
    )


def test_turn_loop_stops_on_repeated_policy_blocked_tool_call_after_recovery(monkeypatch):
    class CapturingRecorder:
        def __init__(self) -> None:
            self.events: list[dict] = []
            self.finished: list[dict] = []

        def start_run(self, **_kwargs):
            return "run-repeat"

        def log_event(self, **kwargs):
            self.events.append(kwargs)

        def log_error(self, **kwargs):
            self.events.append({"event_type": "error", **kwargs})

        def finish_run(self, **kwargs):
            self.finished.append(kwargs)

        def finish_open_run_for_conversation(self, **_kwargs):
            return None

    recorder = CapturingRecorder()
    monkeypatch.setattr("app.agent.turn_loop.get_observability_recorder", lambda: recorder)
    registry = ToolRegistry(
        [
            {
                "name": "echo_tool",
                "description": "Echo text",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "callable": lambda text: f"echo:{text}",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    llm = AlwaysRepeatGenericToolLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=FakeMemory())

    async def collect():
        return [event async for event in loop.run("repeat tool", "conv-repeat-tool-stop", system_prompt="system")]

    events = asyncio.run(collect())
    tool_end_outputs = [
        event["data"]["output"]
        for event in events
        if event["event"] == "tool_end"
    ]
    blocked_output = json.loads(tool_end_outputs[-1])

    assert llm.calls == 4
    assert blocked_output["code"] == "repeated_tool_call_blocked"
    assert events[-1]["data"]["status"] == "paused"
    assert events[-1]["data"]["reason_code"] == "repeated_tool_call_blocked"
    assert "after a recovery prompt" in events[-1]["data"]["summary"]
    assert any(
        item.get("event_type") == "guardrail_triggered"
        and item.get("error_code") == "repeated_tool_call_blocked"
        for item in recorder.events
    )
    assert recorder.finished[-1]["failure_reason"] == "repeated_tool_call_blocked"


def test_turn_loop_allows_repeated_metadata_observation_tool_calls():
    class RepeatObservationToolLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
            self.calls += 1
            if self.calls > 3:
                return final_answer_response("Window inspection complete.")
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id=f"observe-{self.calls}",
                        tool_name="computer_functions_list_apps",
                        arguments={},
                    )
                ],
                finish_reason="tool_calls",
            )

    registry = ToolRegistry(
        [
            {
                "name": "computer_functions_list_apps",
                "description": "List windows",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": lambda: json.dumps({"status": "ok", "apps": [{"id": "monaw", "windows": [{"hwnd": 1, "title": "AI Agent"}]}]}),
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
                "metadata": {
                    "observation": True,
                    "mutates_state": False,
                    "repeat_safe": True,
                    "risk_level": "low",
                },
            }
        ]
    )
    llm = RepeatObservationToolLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=FakeMemory())

    async def collect():
        return [event async for event in loop.run("repeat window inspection", "conv-window-inspect", system_prompt="system")]

    events = asyncio.run(collect())
    tool_end_outputs = [
        json.loads(event["data"]["output"])
        for event in events
        if event["event"] == "tool_end"
    ]

    assert llm.calls == 4
    assert len(tool_end_outputs) == 3
    assert all(output["status"] == "ok" for output in tool_end_outputs)
    assert events[-1]["data"]["summary"] == "Window inspection complete."


def test_turn_loop_stops_on_repeated_tool_level_blocked_result():
    blocked_output = json.dumps(
        {
            "status": "blocked",
            "reason_code": "screen_fallback_disabled",
            "reason": "Screen fallback is disabled.",
        }
    )
    registry = ToolRegistry(
        [
            {
                "name": "blocked_tool",
                "description": "Always blocked",
                "parameters": {
                    "type": "object",
                    "properties": {"target": {"type": "string"}},
                    "required": ["target"],
                },
                "callable": lambda target: blocked_output,
                "domain": "desktop",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    llm = RepeatBlockedToolLLM()
    loop = TurnLoop(llm_client=llm, registry=registry, memory=FakeMemory())

    async def collect():
        return [event async for event in loop.run("repeat blocked tool", "conv-blocked-tool", system_prompt="system")]

    events = asyncio.run(collect())
    tool_end_outputs = [
        json.loads(event["data"]["output"])
        for event in events
        if event["event"] == "tool_end"
    ]

    assert llm.calls == 3
    assert tool_end_outputs[0]["status"] == "blocked"
    assert tool_end_outputs[1]["code"] == "repeated_blocked_tool_result"
    assert tool_end_outputs[1]["recovery_required"] is True
    assert tool_end_outputs[2]["code"] == "repeated_tool_call_blocked"
    assert events[-1]["data"]["status"] == "paused"
    assert events[-1]["data"]["reason_code"] == "repeated_tool_call_blocked"


def test_turn_loop_browser_tool_timeout_returns_structured_error():
    async def slow_snapshot():
        await asyncio.sleep(0.05)
        return json.dumps({"status": "ok"})

    registry = ToolRegistry(
        [
            {
                "name": "browser_snapshot",
                "description": "Snapshot",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": slow_snapshot,
                "domain": "browser",
                "execution_mode": "async",
                "affinity_group": "browser-use",
                "timeout_seconds": 0.01,
            }
        ]
    )
    loop = TurnLoop(llm_client=TimeoutBrowserLLM(), registry=registry, memory=FakeMemory())

    async def collect():
        return [event async for event in loop.run("snapshot", "conv-timeout", system_prompt="system")]

    events = asyncio.run(collect())
    timeout_end = next(event for event in events if event["event"] == "tool_end")
    output = json.loads(timeout_end["data"]["output"])

    assert timeout_end["data"]["status"] == "error"
    assert output["code"] == "tool_timeout"
    assert events[-1]["event"] == "done"


def test_turn_loop_emits_reasoning_content_as_thinking_events():
    registry = ToolRegistry([])
    memory = FakeMemory()

    class ReasoningLLM:
        async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
            assert stream_callback is None
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        call_id="final-answer",
                        tool_name="final_answer",
                        arguments={"answer": "All done."},
                    )
                ],
                finish_reason="tool_calls",
                reasoning_content="reasoning delta",
            )

    llm = ReasoningLLM()
    loop_obj = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [e async for e in loop_obj.run("hello", "conv-think", system_prompt="")]

    events = asyncio.run(collect())
    thinking_events = [e for e in events if e["event"] == "thinking"]
    assert thinking_events, "Expected at least one 'thinking' event from reasoning_content"
    assert any(e["data"]["content"] == "reasoning delta" for e in thinking_events)


def test_turn_loop_heartbeat_emitted_on_idle():
    """Heartbeat pump fires when the react loop is idle for >= HEARTBEAT_INTERVAL_S."""
    from app.agent.turn_loop import HEARTBEAT_INTERVAL_S

    registry = ToolRegistry([])
    memory = FakeMemory()

    class SleepyLLM:
        """Sleeps slightly longer than HEARTBEAT_INTERVAL_S before responding."""

        async def chat_with_tools(self, messages, tools, system_prompt="", stream_callback=None):  # noqa: ARG002
            await asyncio.sleep(HEARTBEAT_INTERVAL_S + 0.2)
            return LLMResponse(content="Done.", tool_calls=[], finish_reason="stop")

    loop_obj = TurnLoop(llm_client=SleepyLLM(), registry=registry, memory=memory)

    async def collect():
        return [e async for e in loop_obj.run("hi", "conv-hb", system_prompt="")]

    events = asyncio.run(collect())
    hb_events = [e for e in events if e["event"] == "heartbeat"]
    assert hb_events, "Expected at least one heartbeat during slow LLM call"
    assert all("t" in e["data"] for e in hb_events)


def test_turn_loop_llm_timeout_emits_error_and_done():
    """When LLM exceeds max_llm_call_seconds, error + done events are emitted."""
    registry = ToolRegistry([])
    memory = FakeMemory()
    loop_obj = TurnLoop(
        llm_client=SlowLLM(),
        registry=registry,
        memory=memory,
        max_llm_call_seconds=0.1,
    )

    async def collect():
        return [e async for e in loop_obj.run("hi", "conv-llm-to", system_prompt="")]

    events = asyncio.run(collect())
    event_names = [e["event"] for e in events]
    assert "error" in event_names
    error_evt = next(e for e in events if e["event"] == "error")
    assert error_evt["data"]["code"] == "llm_timeout"
    assert "done" in event_names


def test_turn_loop_turn_timeout_emits_error_and_done():
    """When total turn exceeds max_turn_seconds, error + done events are emitted."""
    registry = ToolRegistry([])
    memory = FakeMemory()
    loop_obj = TurnLoop(
        llm_client=SlowLLM(),
        registry=registry,
        memory=memory,
        max_turn_seconds=0.1,
        max_llm_call_seconds=999,
    )

    async def collect():
        return [e async for e in loop_obj.run("hi", "conv-turn-to", system_prompt="")]

    events = asyncio.run(collect())
    event_names = [e["event"] for e in events]
    assert "error" in event_names
    error_evt = next(e for e in events if e["event"] == "error")
    assert error_evt["data"]["code"] == "turn_timeout"
    assert "done" in event_names


def test_turn_loop_resets_budget_between_runs():
    registry = ToolRegistry([])
    memory = FakeMemory()
    shared_budget = IterationBudget(max_iterations=1)
    loop_obj = TurnLoop(
        llm_client=SingleShotLLM(),
        registry=registry,
        memory=memory,
        budget=shared_budget,
        max_iterations=1,
    )

    async def collect(conv_id: str):
        return [e async for e in loop_obj.run("hello", conv_id, system_prompt="")]

    first = asyncio.run(collect("conv-budget-1"))
    second = asyncio.run(collect("conv-budget-2"))

    assert first[-1]["data"]["summary"] == "Done."
    assert second[-1]["data"]["summary"] == "Done."


def test_turn_loop_emits_iteration_budget_error_and_done():
    registry = ToolRegistry([])
    memory = FakeMemory()
    exhausted_budget = IterationBudget(max_iterations=0)
    loop_obj = TurnLoop(
        llm_client=SingleShotLLM(),
        registry=registry,
        memory=memory,
        budget=exhausted_budget,
        max_iterations=1,
    )

    async def collect():
        return [e async for e in loop_obj.run("hello", "conv-budget-error", system_prompt="")]

    events = asyncio.run(collect())
    event_names = [e["event"] for e in events]

    assert "error" in event_names
    error_evt = next(e for e in events if e["event"] == "error")
    assert error_evt["data"]["code"] == "iteration_budget_exhausted"
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["summary"] == "Iteration budget exhausted."


def test_turn_loop_recomputes_system_prompt_after_registry_refresh():
    llm = PromptRecordingLLM()

    def refresh_tools():
        registry.register(
            {
                "name": "new_reflected_tool",
                "description": "New reflected tool",
                "parameters": {"type": "object", "properties": {}},
                "callable": lambda: "ok",
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        )
        return "refreshed"

    registry = ToolRegistry(
        [
            {
                "name": "refresh_tools",
                "description": "Refresh tools",
                "parameters": {"type": "object", "properties": {}},
                "callable": refresh_tools,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            }
        ]
    )
    memory = FakeMemory()
    loop_obj = TurnLoop(llm_client=llm, registry=registry, memory=memory)

    async def collect():
        return [e async for e in loop_obj.run("refresh", "conv-refresh", system_prompt=lambda: f"rev:{registry.revision}")]

    asyncio.run(collect())

    assert len(llm.prompts) == 2
    assert llm.prompts[0].startswith("rev:1")
    assert llm.prompts[1].startswith("rev:2")
    assert "## Completion Protocol" in llm.prompts[0]
