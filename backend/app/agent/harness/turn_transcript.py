"""Live model history and the instruction revision to which a response belongs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json

from app.agent.harness.model_request import ModelRequest, ModelResult
from app.agent.harness.tool_protocol import ToolCallResult
from app.agent.llm_client import LLMResponse, build_tool_result_message


@dataclass
class TurnTranscript:
    messages: list[dict] = field(default_factory=list)
    latest_round_start: int = 0
    revision: int = 0
    round_calls: list[tuple[str, str]] = field(default_factory=list)
    answered_call_ids: set[str] = field(default_factory=set)
    provider: str = ""

    def snapshot(self, *, tools: list[dict], system_prompt: str, **kwargs) -> ModelRequest:
        return ModelRequest(
            messages=deepcopy(self.messages),
            tools=[{**tool, "parameters": deepcopy(tool.get("parameters", {}))} for tool in tools],
            system_prompt=system_prompt,
            latest_round_start=self.latest_round_start, revision=self.revision, **kwargs,
        )

    def accept(self, result: ModelResult) -> bool:
        if result.revision != self.revision:
            return False
        self.messages = result.messages
        self.latest_round_start = result.latest_round_start
        return True

    def steer(self, message: dict) -> None:
        self.messages.append(message)
        self.revision += 1

    def begin_tool_round(self, response: LLMResponse) -> None:
        self.latest_round_start = len(self.messages)
        if response.provider_messages:
            self.messages.extend(response.provider_messages)
        elif response.content:
            self.messages.append({"role": "assistant", "content": response.content})
        self.round_calls = [(call.call_id or f"tool-{index}", call.tool_name) for index, call in enumerate(response.tool_calls, 1)]
        self.answered_call_ids.clear()

    def add_tool_result(self, result: ToolCallResult, *, images: list[dict] | None = None) -> None:
        entry = build_tool_result_message(result, provider=self.provider)
        if images:
            entry.update({"images": images, "ephemeral": True})
        self.messages.append(entry)
        self.answered_call_ids.add(result.call_id)

    def close_unanswered_calls(self, reason_code: str, *, explanation: str = "Not run: the turn stopped first.") -> None:
        for call_id, name in self.round_calls:
            if call_id not in self.answered_call_ids:
                self.add_tool_result(ToolCallResult.from_output(
                    call_id=call_id, name=name,
                    output=json.dumps({"status": "cancelled", "reason_code": reason_code, "error": explanation}),
                ))
