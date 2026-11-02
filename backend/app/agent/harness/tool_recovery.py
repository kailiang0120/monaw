"""Recovery decisions for blocked, repeated, and unfinished tool actions."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.agent.harness.tool_policy import ToolPolicyDecision, tool_call_signature
from app.agent.harness.turn_lifecycle import TurnOutcome
from app.agent.harness.turn_observer import TurnObserver
from app.agent.tool_observations import (
    blocked_tool_key, browser_repeat_key, build_repeat_recovery_prompt,
    is_browser_observation_action, is_browser_repeat_guarded, parse_tool_json,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuardedToolResult:
    output: str
    status: str
    recovery_prompt: str = ""
    pause: TurnOutcome | None = None


class ToolRecovery:
    def __init__(self, observer: TurnObserver, *, stuck_rounds_limit: int, guard_blocks_limit: int) -> None:
        self.observer = observer
        self.conversation_id = observer.conversation_id
        self.STUCK_ROUNDS_LIMIT = stuck_rounds_limit
        self.GUARD_BLOCKS_LIMIT = guard_blocks_limit
        self._last_tool_outputs_by_signature: dict[str, str] = {}
        self._browser_repeat_counts: dict[str, int] = {}
        self._blocked_tool_counts: dict[str, int] = {}
        self._policy_repeat_recoveries: set[str] = set()
        self._browser_repeat_recoveries: set[str] = set()
        self._blocked_tool_recoveries: set[str] = set()
        self._round_guard_blocks = self._guard_blocks = self._stuck_rounds = 0
        self._last_guard_code = ""
        self._stop_requested = False
        self._pause_text = self._pause_reason = ""

    @property
    def pause(self) -> TurnOutcome | None:
        return TurnOutcome(self._pause_text, "paused", self._pause_reason) if self._stop_requested else None

    def begin_round(self) -> None:
        self._round_guard_blocks = 0

    def record_result(self, name: str, arguments: dict, decision: ToolPolicyDecision, output: str) -> None:
        signature = str(decision.metadata.get("signature") or tool_call_signature(name, arguments))
        self._last_tool_outputs_by_signature[signature] = output

    def observe_result(self, *, tool_name: str, arguments: dict, call_id: str, tool_output: str, status: str) -> GuardedToolResult:
        recovery_prompt = ""
        review_result = parse_tool_json(tool_output)
        review_code = str(review_result.get("reason_code") or "")
        if review_code in {"auto_review_retry_blocked", "auto_review_limit"}:
            self._stop_requested = True
            self._pause_reason = review_code
            self._pause_text = "Paused: " + str(review_result.get("reason") or "Auto Review blocked this action.")
        blocked_key = blocked_tool_key(tool_name, arguments, tool_output)
        if review_code.startswith("auto_review_"):
            blocked_key = None
        if blocked_key is not None:
            self._blocked_tool_counts[blocked_key] = self._blocked_tool_counts.get(blocked_key, 0) + 1
            if self._blocked_tool_counts[blocked_key] >= 2:
                status = "error"
                self._round_guard_blocks += 1
                self._guard_blocks += 1
                self._last_guard_code = "repeated_blocked_tool_result"
                blocked_payload = parse_tool_json(tool_output)
                if blocked_key not in self._blocked_tool_recoveries:
                    self._blocked_tool_recoveries.add(blocked_key)
                    self._policy_repeat_recoveries.add(tool_call_signature(tool_name, arguments))
                    tool_output = json.dumps(
                        {
                            "status": "error",
                            "code": "repeated_blocked_tool_result",
                            "tool": tool_name,
                            "arguments": arguments,
                            "recovery_required": True,
                            "error": (
                                "The same tool action was blocked repeatedly. "
                                "Use a different permitted action, ask for permission changes, "
                                "or report the blocker."
                            ),
                            "last_result": blocked_payload,
                        },
                        ensure_ascii=False,
                    )
                    recovery_prompt = build_repeat_recovery_prompt(
                        reason_code="repeated_blocked_tool_result",
                        tool_name=tool_name,
                        arguments=arguments,
                        last_result=blocked_payload,
                    )
                    logger.info(
                        "turn_loop prompted recovery after repeated blocked tool result "
                        "conversation_id=%s tool=%s arguments=%s",
                        self.conversation_id,
                        tool_name,
                        arguments,
                    )
                    self.observer.event(
                        "guardrail_triggered",
                        level="warning",
                        status="error",
                        tool_name=tool_name,
                        error_code="repeated_blocked_tool_result",
                        error_message="Repeated blocked tool result; recovery prompt inserted.",
                        input={"arguments": arguments, "call_id": call_id},
                        output={"last_result": blocked_payload},
                        metadata={"recovery_required": True},
                    )
                else:
                    tool_output = json.dumps(
                        {
                            "status": "error",
                            "code": "repeated_blocked_tool_result",
                            "tool": tool_name,
                            "arguments": arguments,
                            "error": (
                                "This exact action was blocked again after a recovery prompt and will stay blocked. "
                                "Stop retrying it: choose a different permitted action, or tell the user "
                                "what is blocked and what they can change."
                            ),
                            "last_result": blocked_payload,
                        },
                        ensure_ascii=False,
                    )
                    logger.warning(
                        "turn_loop repeated blocked tool result after recovery "
                        "conversation_id=%s tool=%s arguments=%s",
                        self.conversation_id,
                        tool_name,
                        arguments,
                    )
                    self.observer.event(
                        "guardrail_triggered",
                        level="warning",
                        status="error",
                        tool_name=tool_name,
                        error_code="repeated_blocked_tool_result",
                        error_message="Repeated blocked tool result after a recovery prompt.",
                        input={"arguments": arguments, "call_id": call_id},
                        output={"last_result": blocked_payload},
                        metadata={"after_recovery": True},
                    )
        if is_browser_observation_action(tool_name, arguments) and status == "ok":
            self._browser_repeat_counts.clear()
        if is_browser_repeat_guarded(tool_name, arguments) and status == "ok":
            repeat_key = browser_repeat_key(tool_name, arguments, tool_output)
            self._browser_repeat_counts[repeat_key] = self._browser_repeat_counts.get(repeat_key, 0) + 1
            if self._browser_repeat_counts[repeat_key] >= 2:
                status = "error"
                self._round_guard_blocks += 1
                self._guard_blocks += 1
                self._last_guard_code = "stalled_repeat_detected"
                repeated_browser_result = tool_output
                if repeat_key not in self._browser_repeat_recoveries:
                    self._browser_repeat_recoveries.add(repeat_key)
                    self._policy_repeat_recoveries.add(tool_call_signature(tool_name, arguments))
                    tool_output = json.dumps(
                        {
                            "status": "error",
                            "code": "stalled_repeat_detected",
                            "tool": tool_name,
                            "arguments": arguments,
                            "recovery_required": True,
                            "error": (
                                "Repeated the same browser action with the same arguments "
                                "and unchanged page state. Inspect status/tabs/snapshot or report the blocker."
                            ),
                        },
                        ensure_ascii=False,
                    )
                    recovery_prompt = build_repeat_recovery_prompt(
                        reason_code="stalled_repeat_detected",
                        tool_name=tool_name,
                        arguments=arguments,
                        last_result=repeated_browser_result,
                    )
                    logger.info(
                        "turn_loop prompted recovery after repeated browser action "
                        "conversation_id=%s tool=%s arguments=%s",
                        self.conversation_id,
                        tool_name,
                        arguments,
                    )
                    self.observer.event(
                        "guardrail_triggered",
                        level="warning",
                        status="error",
                        tool_name=tool_name,
                        error_code="stalled_repeat_detected",
                        error_message="Repeated browser action with unchanged page state; recovery prompt inserted.",
                        input={"arguments": arguments, "call_id": call_id},
                        output={"last_result": repeated_browser_result},
                        metadata={"recovery_required": True},
                    )
                else:
                    tool_output = json.dumps(
                        {
                            "status": "error",
                            "code": "stalled_repeat_detected",
                            "tool": tool_name,
                            "arguments": arguments,
                            "error": (
                                "Repeated the same browser action again after a recovery prompt, and the page "
                                "still did not change. Stop repeating it: inspect the page with a snapshot, "
                                "change the target, or tell the user what is blocking you."
                            ),
                        },
                        ensure_ascii=False,
                    )
                    logger.warning(
                        "turn_loop repeated browser action after recovery "
                        "conversation_id=%s tool=%s arguments=%s",
                        self.conversation_id,
                        tool_name,
                        arguments,
                    )
                    self.observer.event(
                        "guardrail_triggered",
                        level="warning",
                        status="error",
                        tool_name=tool_name,
                        error_code="stalled_repeat_detected",
                        error_message="Repeated browser action with unchanged page state after a recovery prompt.",
                        input={"arguments": arguments, "call_id": call_id},
                        output={"last_result": repeated_browser_result},
                        metadata={"after_recovery": True},
                    )

        result_payload = parse_tool_json(tool_output)
        if result_payload.get("worker_state") == "stopping":
            self._stop_requested = True
            self._pause_reason = "tool_still_running"
            self._pause_text = "Paused: a tool is still stopping after cancellation or timeout. Wait for it to finish, then continue and verify its last action."
        return GuardedToolResult(tool_output, status, recovery_prompt, self.pause)

    def policy_blocked(self, *, tool_name: str, arguments: dict, call_id: str, policy_decision: ToolPolicyDecision) -> GuardedToolResult:
        blocked_code = policy_decision.metadata.get("code", "tool_policy_blocked")
        recovery_prompt = ""
        tool_output = json.dumps(
            {
                "status": "error",
                "code": blocked_code,
                "tool": tool_name,
                "arguments": arguments,
                "error": policy_decision.reason,
            },
            ensure_ascii=False,
        )
        self.observer.event(
            "guardrail_triggered",
            level="warning",
            status="error",
            tool_name=tool_name,
            error_code=blocked_code,
            error_message=policy_decision.reason,
            input={"arguments": arguments, "call_id": call_id},
            metadata={"risk": policy_decision.risk},
        )
        if blocked_code == "repeated_tool_call_blocked":
            self._round_guard_blocks += 1
            self._guard_blocks += 1
            self._last_guard_code = blocked_code
            signature = str(
                policy_decision.metadata.get("signature")
                or tool_call_signature(tool_name, arguments)
            )
            if signature not in self._policy_repeat_recoveries:
                self._policy_repeat_recoveries.add(signature)
                recovery_prompt = build_repeat_recovery_prompt(
                    reason_code="repeated_tool_call_blocked",
                    tool_name=tool_name,
                    arguments=arguments,
                    last_result=self._last_tool_outputs_by_signature.get(signature),
                )
                logger.info(
                    "turn_loop prompted recovery after repeated blocked tool call "
                    "conversation_id=%s tool=%s arguments=%s",
                    self.conversation_id,
                    tool_name,
                    arguments,
                )
            else:
                tool_output = json.dumps(
                    {
                        "status": "error",
                        "code": blocked_code,
                        "tool": tool_name,
                        "arguments": arguments,
                        "error": (
                            "This exact call was already blocked as a repeat and will stay blocked. "
                            "Use the earlier result, choose a different action, "
                            "or answer the user."
                        ),
                    },
                    ensure_ascii=False,
                )
                logger.warning(
                    "turn_loop repeated blocked tool call after recovery "
                    "conversation_id=%s tool=%s arguments=%s",
                    self.conversation_id,
                    tool_name,
                    arguments,
                )
        return GuardedToolResult(tool_output, "error", recovery_prompt)

    def finish_round(self, call_count: int) -> TurnOutcome | None:
        if call_count and not self._stop_requested:
            self._stuck_rounds = self._stuck_rounds + 1 if self._round_guard_blocks >= call_count else 0
            if self._stuck_rounds >= self.STUCK_ROUNDS_LIMIT or self._guard_blocks >= self.GUARD_BLOCKS_LIMIT:
                self._stop_requested = True
                self._pause_reason = self._last_guard_code
                self._pause_text = (
                    f"Stopped: every tool call in the last {self._stuck_rounds} rounds was blocked or repeated."
                    if self._stuck_rounds >= self.STUCK_ROUNDS_LIMIT
                    else f"Stopped: {self._guard_blocks} tool calls in this turn were blocked as repeats."
                )
                logger.warning(
                    "turn_loop stopped: stuck on guard blocks conversation_id=%s "
                    "stuck_rounds=%s guard_blocks=%s code=%s",
                    self.conversation_id,
                    self._stuck_rounds,
                    self._guard_blocks,
                    self._last_guard_code,
                )
                self.observer.event(
                    "guardrail_triggered",
                    level="warning",
                    status="error",
                    error_code=self._last_guard_code,
                    error_message=self._pause_text,
                    metadata={"stuck_rounds": self._stuck_rounds, "guard_blocks": self._guard_blocks},
                )


        return self.pause
