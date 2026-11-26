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
        """Bound empty/progress repairs independently of successful tool rounds."""
        self.consecutive_repairs = 0
        self.web_search_reprompted = False

    def steered(self) -> None:
        """Give a new user instruction its own response-repair allowance."""
        self.consecutive_repairs = 0
        self.web_search_reprompted = False

    def _check_answer(
        self, text: str, *, history: list[dict], budget_exhausted: bool,
        messages: list[dict], attachment_paths: list[str] | None = None, progress: str = "",
    ) -> ResponseDecision:
        """Accept an answer only after ruling out empty or unfinished output."""
        text = text.strip()
        if not text:
            self.consecutive_repairs += 1
            if self.consecutive_repairs >= 2 or budget_exhausted:
                return ResponseDecision(
                    "pause", "Paused: the model returned no answer. Continue to retry from the saved results.",
                    "model_empty_response", messages=messages, emit_error=True,
                )
            return ResponseDecision("reprompt", messages=[
                *messages, {"role": "user", "content": (
                    "Your last response contained no answer or tool call. Use the results already available "
                    "to answer the user, ask for missing information, or call the next necessary tool."
                )},
            ])
        if plain_text_describes_next_action(text):
            self.consecutive_repairs += 1
            if self.consecutive_repairs >= 2 or budget_exhausted:
                return ResponseDecision(
                    "pause", "Paused: the model described a next action but did not call a tool. Continue to retry the task.",
                    "progress_without_tool", progress=text, messages=messages,
                )
            # Keep auto tool choice: the repair may legitimately be a final
            # answer or a clarification, with no further action required.
            return ResponseDecision("reprompt", progress=text, messages=[
                *messages, {"role": "user", "content": TOOL_REPROMPT_MESSAGE},
            ])
        if not final_answer_includes_web_search_results(text, history):
            if self.web_search_reprompted or budget_exhausted:
                return ResponseDecision("pause", "Paused: I could not include the web search results in the answer.", "web_search_results_missing", progress=progress)
            self.web_search_reprompted = True
            return ResponseDecision("reprompt", progress=progress, messages=[
                *messages, {"role": "user", "content": WEB_SEARCH_RESULT_REPROMPT_MESSAGE},
            ])
        return ResponseDecision("final", text, messages=messages, attachment_paths=attachment_paths or [], progress=progress)

    def decide(self, response: LLMResponse, *, tools_available: bool, history: list[dict], budget_exhausted: bool) -> ResponseDecision:
        """Keep provider-native completed history when choosing the next turn action."""
        content = response.content or ""
        # Completion evidence takes precedence over any text or calls parsed
        # before the provider stopped. Never execute an unfinished response.
        if response.finish_reason not in {"stop", "tool_calls"}:
            if response.finish_reason == "length":
                reason = "model_output_truncated"
                text = "Paused: the model output was truncated before a final answer. Continue to resume from the saved results."
            elif response.finish_reason == "blocked":
                reason = "model_response_blocked"
                text = "Paused: the model provider blocked the response. Review the request before trying again."
            else:
                reason = "model_response_incomplete"
                text = "Paused: the model did not complete its response. Continue to retry from the saved results."
            if content.strip():
                text += f"\n\nPartial response:\n{content.strip()}"
            return ResponseDecision("pause", text, reason, emit_error=True)
        if not response.tool_calls:
            native_messages = response.provider_messages if any(
                "gemini_parts" in message for message in response.provider_messages
            ) else []
            messages = native_messages or ([{"role": "assistant", "content": content}] if content.strip() else [])
            text = content.strip()
            json_final = json_text_as_final_answer(content)
            if json_final is not None:
                text = json_final
            return self._check_answer(
                text, history=history, budget_exhausted=budget_exhausted,
                messages=messages,
            )

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
        self.consecutive_repairs = 0
        return ResponseDecision("tools", progress=content, calls=calls)

    @staticmethod
    def wrap_up_text(response: LLMResponse) -> str:
        """Keep the existing pause notice unless the summary actually finished."""
        if response.finish_reason not in {"stop", "tool_calls"}:
            return ""
        if response.tool_calls:
            if len(response.tool_calls) != 1 or response.tool_calls[0].tool_name != FINAL_ANSWER_TOOL_NAME:
                return ""
            arguments = response.tool_calls[0].arguments
            text = final_answer_text(arguments if isinstance(arguments, dict) else {}, response.content or "")
        else:
            text = str(response.content or "").strip()
        return "" if plain_text_describes_next_action(text) else text
