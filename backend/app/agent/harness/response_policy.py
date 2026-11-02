"""Decide whether a model response finishes, reprompts, or requests tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.agent.llm_client import LLMResponse
from app.agent.turn_text import (
    FINAL_ANSWER_TOOL_NAME, TOOL_REPROMPT_MESSAGE, WEB_SEARCH_RESULT_REPROMPT_MESSAGE,
    final_answer_attachment_paths, final_answer_includes_web_search_results, final_answer_text,
    json_text_as_final_answer, plain_text_describes_next_action,
)


@dataclass(frozen=True)
class ResponseDecision:
    action: Literal["final", "reprompt", "tools", "pause"]
    text: str = ""
    reason_code: str = ""
    progress: str = ""
    messages: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    attachment_paths: list[str] = field(default_factory=list)
    emit_error: bool = False


class TurnResponsePolicy:
    def __init__(self) -> None:
        self.consecutive_text_only = 0
        self.force_tool_choice = False
        self.web_search_reprompted = False

    def steered(self) -> None:
        self.consecutive_text_only = 0
        self.force_tool_choice = False

    def take_tool_choice(self) -> Literal["required", "auto"]:
        choice = "required" if self.force_tool_choice else "auto"
        self.force_tool_choice = False
        return choice

    def _check_answer(
        self, text: str, *, history: list[dict], budget_exhausted: bool,
        messages: list[dict], attachment_paths: list[str] | None = None, progress: str = "",
    ) -> ResponseDecision:
        if not final_answer_includes_web_search_results(text, history):
            if self.web_search_reprompted or budget_exhausted:
                return ResponseDecision("pause", "Paused: I could not include the web search results in the answer.", "web_search_results_missing", progress=progress)
            self.web_search_reprompted = True
            return ResponseDecision("reprompt", progress=progress, messages=[
                *messages, {"role": "user", "content": WEB_SEARCH_RESULT_REPROMPT_MESSAGE},
            ])
        return ResponseDecision("final", text, messages=messages, attachment_paths=attachment_paths or [], progress=progress)

    def decide(self, response: LLMResponse, *, tools_available: bool, history: list[dict], budget_exhausted: bool) -> ResponseDecision:
        content = response.content or ""
        if not response.tool_calls:
            text = content.strip() or "Done."
            if response.finish_reason == "length":
                if text == "Done.":
                    text = "Paused: the model output was truncated before a final answer. Continue to resume from the current state."
                return ResponseDecision("pause", text, "model_output_truncated", emit_error=True)
            json_final = json_text_as_final_answer(content)
            if json_final is not None:
                text = json_final
            elif tools_available and plain_text_describes_next_action(content):
                self.consecutive_text_only += 1
                messages = [{"role": "assistant", "content": content}]
                if self.consecutive_text_only >= 2 or budget_exhausted:
                    return ResponseDecision(
                        "pause", "Paused: the model described a next action but did not call a tool. Continue to retry the task.",
                        "progress_without_tool", progress=content, messages=messages,
                    )
                self.force_tool_choice = True
                return ResponseDecision("reprompt", progress=content, messages=[*messages, {"role": "user", "content": TOOL_REPROMPT_MESSAGE}])
            return self._check_answer(
                text, history=history, budget_exhausted=budget_exhausted,
                messages=[{"role": "assistant", "content": content}] if content else [],
            )

        self.consecutive_text_only = 0
        calls = []
        final_call = None
        for index, call in enumerate(response.tool_calls, 1):
            if call.tool_name == FINAL_ANSWER_TOOL_NAME:
                final_call = call
            else:
                calls.append({
                    "call_id": call.call_id or f"tool-{index}", "name": call.tool_name,
                    "arguments": call.arguments if isinstance(call.arguments, dict) else {}, "step_id": f"tool-{index}",
                })
        if final_call is not None and not calls:
            arguments = final_call.arguments if isinstance(final_call.arguments, dict) else {}
            return self._check_answer(
                final_answer_text(arguments, content), history=history, budget_exhausted=budget_exhausted,
                messages=[], attachment_paths=final_answer_attachment_paths(arguments), progress=content,
            )
        return ResponseDecision("tools", progress=content, calls=calls)
