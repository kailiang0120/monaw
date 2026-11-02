"""Own tool dispatch, permission waits, ordered results, and recovery guards."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Callable

from app.agent.approval_review import AutoReviewState
from app.agent.execution_gate import ExecutionGateService
from app.agent.harness.tool_batch import ActiveToolCall, ToolBatchLedger, TrackedToolCall
from app.agent.harness.tool_executor import ToolExecutor
from app.agent.harness.tool_policy import ToolPolicy, ToolPolicyDecision
from app.agent.harness.tool_recovery import ToolRecovery, GuardedToolResult
from app.agent.harness.tool_protocol import ToolCallRequest as HarnessToolCallRequest, ToolCallResult
from app.agent.harness.turn_lifecycle import TurnLifecycle, TurnOutcome
from app.agent.harness.turn_observer import TurnObserver
from app.agent.harness.turn_transcript import TurnTranscript
from app.agent.harness.workers import unfinished_workers
from app.agent.iteration_budget import IterationBudget
from app.agent.llm_client import LLMClient
from app.agent.memory_manager import MemoryManager
from app.agent.state import ExecutionPlan, PlanStep
from app.agent.steering import SteeringInbox
from app.agent.tool_registry import ToolRegistry, can_parallelize
from app.agent.tool_observations import (
    DEVTOOLS_PREFIX, DEVTOOLS_STEP_DESCRIPTIONS, compact_tool_observation, extract_image_attachments,
    is_devtools_tool, tool_error_code,
)


@dataclass
class PreparedToolCall(TrackedToolCall):
    step_id: str
    plan_step: PlanStep | None
    arguments: dict
    tool_dict: dict
    policy_decision: ToolPolicyDecision
    obs_tool_started_at: float = 0.0
    rejection: GuardedToolResult | None = None


@dataclass(frozen=True)
class BatchOutcome:
    pause: TurnOutcome | None = None


class ToolBatchRunner:
    def __init__(
        self, *, registry: ToolRegistry, executor: ToolExecutor, execution_gate: ExecutionGateService,
        memory: MemoryManager, client: LLMClient, budget: IterationBudget,
        transcript: TurnTranscript, lifecycle: TurnLifecycle, observer: TurnObserver,
        steering: SteeringInbox | None, user_request: Callable[[], str], progress: Callable[[], str],
        max_parallel: int, max_identical_calls: int, stuck_rounds_limit: int, guard_blocks_limit: int,
    ) -> None:
        self.registry, self.executor, self.execution_gate = registry, executor, execution_gate
        self.memory, self.llm_client, self.budget = memory, client, budget
        self.transcript, self.lifecycle, self.observer = transcript, lifecycle, observer
        self.conversation_id = lifecycle.conversation_id
        self.emit = lifecycle.publisher.publish_nowait
        self.steering, self.user_request, self.progress = steering, user_request, progress
        self.STUCK_ROUNDS_LIMIT, self.GUARD_BLOCKS_LIMIT = stuck_rounds_limit, guard_blocks_limit
        self.ledger = ToolBatchLedger()
        self.history: list[dict] = []
        self.last_tool_name = ""
        self.review_state = AutoReviewState()
        self.review_history: dict = {}
        self.policy = ToolPolicy(max_identical_calls=max_identical_calls)
        self.policy.begin_turn()
        self._parallel_semaphore = asyncio.Semaphore(max_parallel)
        self._gate_lock = asyncio.Lock()
        self._call_states: dict[str, str] = {}
        self.pause: TurnOutcome | None = None
        self.recovery = ToolRecovery(observer, stuck_rounds_limit=stuck_rounds_limit, guard_blocks_limit=guard_blocks_limit)
        self._execution_plan: ExecutionPlan | None = None
        self._sync_plan_progress = getattr(memory, "sync_plan_progress", None)

    def initialize_review_history(self) -> None:
        build = getattr(self.memory, "build_approval_review_context", None)
        self.review_history = build(self.conversation_id) if callable(build) else {}

    def persistable_calls(self) -> list[dict]:
        return [dict(call) for call in self.history] + self.ledger.persistable_calls()

    async def cancel(self, reason_code: str = "user_stopped") -> None:
        await self.ledger.cancel_tasks()
        self.ledger.mark_cancelled(
            worker_state="stopping" if unfinished_workers(self.conversation_id) else "stopped",
            observe=self._observe_cancelled_call, reason_code=reason_code,
        )

    @staticmethod
    def _describe_tool_step(tool_name: str, arguments: dict) -> str:
        if is_devtools_tool(tool_name):
            short_name = tool_name[len(DEVTOOLS_PREFIX):]
            if short_name == "navigate_page":
                direction = str(arguments.get("type") or "url").strip().lower()
                if direction in {"back", "forward"}:
                    return f"Go {direction} in the browser page"
                if direction == "reload":
                    return "Reload the browser page"
                return "Open the requested browser page"
            return DEVTOOLS_STEP_DESCRIPTIONS.get(short_name, f"Run browser action `{short_name}`")
        if tool_name == "browser_fetch":
            return "Fetch the requested web page"
        return f"Run `{tool_name}`"

    @classmethod
    def _build_execution_plan(cls, message: str, tool_calls: list[dict]) -> ExecutionPlan:
        steps: list[PlanStep] = []
        for index, call in enumerate(tool_calls, start=1):
            tool_name = str(call.get("name", "tool"))
            arguments = call.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            steps.append(
                PlanStep(
                    step_id=f"tool-{index}",
                    description=cls._describe_tool_step(tool_name, arguments),
                    tool_hints=[tool_name],
                    success_criteria=f"`{tool_name}` returns a successful result or a clear recoverable error.",
                )
            )
        return ExecutionPlan(original_message=message, steps=steps)

    @staticmethod
    def _tool_output_status(tool_output: str, fallback: str = "ok") -> str:
        try:
            payload = json.loads(tool_output) if isinstance(tool_output, str) else tool_output
        except (json.JSONDecodeError, TypeError):
            return fallback
        if not isinstance(payload, dict):
            return fallback
        status = str(payload.get("status") or "").lower()
        if status == "cancelled":
            return "cancelled"
        if status in {"error", "blocked", "denied", "failed"}:
            return "error"
        return fallback

    def _observe_cancelled_call(self,
        call: ActiveToolCall,
        status: str,
        output: str,
        error_code: str,
        duration_ms: int | None,
    ) -> None:
        self.observer.tool_finished(
            call.tool_name or "tool",
            output,
            status,
            call.call_id,
            call.obs_started_at or time.perf_counter(),
            error_code=error_code,
            duration_ms=duration_ms,
        )


    def _record_call_result(self, prepared: PreparedToolCall, result: ToolCallResult, *, timed: bool = True) -> None:
        self.ledger.record_result(prepared.active_key, result, timed=timed)


    def _refresh_phase(self) -> None:
        waiting = next((state for state in self._call_states.values() if state in {"approval_required", "access_grant_required"}), None)
        if waiting:
            self.lifecycle.transition(self.lifecycle.state.waiting_for, waiting)
        elif self._call_states:
            self.lifecycle.transition(self.lifecycle.state.tool_execution)

    def _emit_gate_event(self, event: dict, key: str) -> None:
        kind = str(event.get("event"))
        if kind in {"approval_required", "access_grant_required"}:
            self._call_states[key] = kind
        elif kind in {"approval_resolved", "tool_resumed"}:
            self._call_states[key] = "running"
        self._refresh_phase()
        self.emit(event)


    async def _invoke_prepared_tool(self, prepared: PreparedToolCall) -> ToolCallResult:
        """Invoke one preflighted tool and wait on any gate it raises.

        Waiting here instead of after the whole batch shows the prompt
        as soon as this call needs it, while sibling calls keep running.
        """
        tool_name = str(prepared.tool_name)
        call_id = str(prepared.call_id)

        async def _execute(tool_dict: dict, arguments: dict) -> ToolCallResult:
            async with self._parallel_semaphore:
                blocked = self.review_state.blocked_output(tool_name, arguments)
                if blocked:
                    return ToolCallResult.from_output(call_id=call_id, name=tool_name, output=blocked)
                if self.steering is not None and self.steering.pending:
                    return ToolCallResult.from_output(
                        call_id=call_id, name=tool_name,
                        output=json.dumps({"status": "cancelled", "reason_code": "user_steered", "error": "Skipped so the agent can read the new user instruction."}),
                    )
                self._call_states[prepared.active_key] = "running"
                self._refresh_phase()
                return await self.executor.execute(budget=self.budget, tool_dict=tool_dict, arguments=arguments, call_id=call_id)

        async def _rerun_after_grant(_budget: IterationBudget, tool_dict: dict, arguments: dict) -> str:
            # Keep the original call_id so Stop and timeouts still reach the re-run.
            return (await _execute(tool_dict, arguments)).output

        try:
            result = await _execute(prepared.tool_dict, prepared.arguments)
            self._record_call_result(prepared, result)
            if ExecutionGateService.check_pending_status(result.output) is None:
                return result
            async with self._gate_lock:
                output = await self.execution_gate.resolve_pending_output(
                    tool_output=result.output,
                    tool_dict=prepared.tool_dict,
                    arguments=prepared.arguments,
                    budget=self.budget,
                    execute_tool=_rerun_after_grant,
                    emit=lambda event: self._emit_gate_event(event, prepared.active_key),
                    review_client=self.llm_client,
                    user_request=self.user_request(),
                    review_context={**self.review_history, "progress": self.progress(),
                                    "tool_evidence": self.history},
                    review_state=self.review_state,
                    review_is_current=lambda: self.steering is None or not self.steering.pending,
                    call_id=call_id,
                )
            result = ToolCallResult.from_output(call_id=call_id, name=tool_name, output=output)
            self._record_call_result(prepared, result, timed=False)
            return result
        except asyncio.CancelledError as exc:
            stopped_output = getattr(exc, "tool_output", None)
            if stopped_output:
                self._record_call_result(prepared, ToolCallResult.from_output(
                    call_id=call_id, name=tool_name, output=stopped_output), timed=False)
            raise
        except Exception as exc:
            self.observer.error(
                str(exc),
                error_type=type(exc).__name__,
                metadata={"phase": "tool_call", "tool": tool_name, "call_id": call_id},
            )
            result = ToolCallResult.from_output(
                call_id=call_id,
                name=tool_name,
                output=json.dumps({"status": "error", "error": str(exc)}),
                fallback_status="error",
            )
            self._record_call_result(prepared, result)
            return result
        finally:
            self._call_states.pop(prepared.active_key, None)
            self._refresh_phase()


    def _report_escaped_tool_error(self, prepared: PreparedToolCall, exc: BaseException) -> None:
        self.observer.error(
            str(exc),
            error_type=type(exc).__name__,
            metadata={
                "phase": "tool_call",
                "tool": str(prepared.tool_name),
                "call_id": str(prepared.call_id),
            },
        )


    async def _process_tool_result(self, prepared: PreparedToolCall, tool_result: ToolCallResult, *, guarded: GuardedToolResult | None = None) -> None:
        """Apply the ordered result guards and publish the call's final result."""

        call_id = str(prepared.call_id)
        step_id = str(prepared.step_id)
        plan_step = prepared.plan_step
        tool_name = str(prepared.tool_name)
        arguments = prepared.arguments
        policy_decision = prepared.policy_decision
        status = "ok" if tool_result.status == "ok" else "error"
        tool_output = tool_result.output

        guarded = guarded or self.recovery.observe_result(
            tool_name=tool_name, arguments=arguments, call_id=call_id,
            tool_output=tool_output, status=self._tool_output_status(tool_output, status),
        )
        raw_tool_output, status, recovery_prompt = guarded.output, guarded.status, guarded.recovery_prompt
        self.pause = guarded.pause or self.pause
        self.ledger.record_output(prepared.active_key, raw_tool_output, status)
        active_call = self.ledger.get(prepared.active_key)
        image_attachments = extract_image_attachments(tool_name, raw_tool_output)
        tool_result = ToolCallResult.from_output(
            call_id=call_id,
            name=tool_name,
            output=raw_tool_output,
            fallback_status="ok" if status == "ok" else "error",
            metadata={"policy": policy_decision.metadata},
        )
        self.recovery.record_result(tool_name, arguments, policy_decision, raw_tool_output)

        self.emit({
            "event": "tool_end",
            "data": {
                "tool": tool_name,
                "output": raw_tool_output,
                "status": status,
                "call_id": call_id,
                "risk": policy_decision.risk,
            },
        })
        self.observer.tool_finished(
            tool_name,
            raw_tool_output,
            status,
            call_id,
            prepared.obs_tool_started_at,
            risk=policy_decision.risk,
            error_code=tool_error_code(raw_tool_output) if status != "ok" else "",
            duration_ms=active_call.duration_ms if active_call is not None else None,
        )
        if active_call is not None:
            active_call.observability_finished = True
        if plan_step is not None:
            verified = status == "ok"
            plan_step.status = "done" if verified else "failed"
            if callable(self._sync_plan_progress):
                self._sync_plan_progress(self.conversation_id, self._execution_plan)
            self.emit({
                "event": "observation",
                "data": {
                    "step_id": step_id,
                    "verified": verified,
                    "detail": compact_tool_observation(tool_name, raw_tool_output) if verified else raw_tool_output,
                },
            })
            self.emit({
                "event": "step_complete",
                "data": {
                    "step_id": step_id,
                    "status": "done" if verified else "failed",
                },
            })

        self.memory.add_tool_outcome(self.conversation_id, tool_name, raw_tool_output)
        self.history.append(
            {
                "tool_name": tool_name,
                "input": json.dumps(arguments, ensure_ascii=False),
                "output": raw_tool_output,
                "status": "complete" if status == "ok" else status,
            }
        )
        self.ledger.finish(prepared.active_key)
        self.transcript.add_tool_result(tool_result, images=image_attachments)
        if recovery_prompt:
            self.transcript.messages.append({"role": "user", "content": recovery_prompt})


    async def _flush_prepared_tool_calls(self, prepared_calls: list[PreparedToolCall]) -> None:
        """Invoke and process the calls collected before a serial barrier."""
        if not prepared_calls:
            return
        calls = list(prepared_calls)
        prepared_calls.clear()
        results = await self.ledger.run(
            calls,
            self._invoke_prepared_tool,
            parallel=len(calls) > 1,
            report_escaped_error=self._report_escaped_tool_error,
        )
        for prepared, result in zip(calls, results):
            await self._process_tool_result(prepared, result)

    def _prepare(self, call: dict, steps: dict[str, PlanStep]) -> PreparedToolCall:
        step_id = str(call.get("step_id") or "")
        call_id = str(call.get("call_id") or step_id or call.get("name") or "tool-call")
        step = steps.get(step_id)
        if step is not None:
            step.status = "active"
            self._sync_plan()
            self.emit({"event": "step_start", "data": {"step_id": step_id, "description": step.description}})
        name, arguments = str(call["name"]), call["arguments"]
        self.last_tool_name = name
        dispatched = self.registry.dispatch(name, arguments)
        rejection = None
        if dispatched is None:
            tool = {"name": name}
            decision = self.policy.decide(
                request=HarnessToolCallRequest(call_id, name, arguments), registry=self.registry, arguments=arguments,
            )
            rejection = GuardedToolResult(json.dumps({
                "status": "error", "code": decision.metadata.get("code", "unknown_tool"),
                "error": decision.reason or f"Unknown tool '{name}'",
            }), "error")
        else:
            tool, arguments = dispatched["tool"], dispatched["arguments"]
            name = self.last_tool_name = str(tool["name"])
            errors = dispatched.get("validation_errors") or []
            if errors:
                decision = ToolPolicyDecision(allowed=False, risk="low")
                rejection = GuardedToolResult(json.dumps({
                    "status": "error", "reason_code": "invalid_tool_arguments",
                    "error": "; ".join(errors), "expected_schema": tool.get("parameters", {}),
                }), "error")
            else:
                decision = self.policy.decide(
                    request=HarnessToolCallRequest(call_id, name, arguments, step_id or None),
                    registry=self.registry, arguments=arguments,
                )
        return PreparedToolCall("", call_id, name, step_id, step, arguments, tool, decision, rejection=rejection)

    def _sync_plan(self) -> None:
        if callable(self._sync_plan_progress):
            self._sync_plan_progress(self.conversation_id, self._execution_plan)

    def _start(self, prepared: PreparedToolCall) -> None:
        prepared.obs_tool_started_at = self.observer.tool_started(
            prepared.tool_name, prepared.arguments, prepared.call_id, prepared.policy_decision.risk,
        )
        prepared.active_key = self.ledger.register(
            call_id=prepared.call_id, tool_name=prepared.tool_name, arguments=prepared.arguments,
            obs_started_at=prepared.obs_tool_started_at,
        )
        self.emit({"event": "tool_start", "data": {
            "tool": prepared.tool_name, "input": prepared.arguments,
            "call_id": prepared.call_id, "risk": prepared.policy_decision.risk,
        }})

    async def execute(self, calls: list[dict]) -> BatchOutcome:
        self.recovery.begin_round()
        self._execution_plan = self._build_execution_plan(self.user_request(), calls)
        steps = {step.step_id: step for step in self._execution_plan.steps}
        self.emit({"event": "plan", "data": self._execution_plan.to_dict()})
        self._sync_plan()
        for batch in can_parallelize(calls, self.registry.get_tool):
            pending: list[PreparedToolCall] = []
            for call in batch:
                if self.pause is not None:
                    break
                prepared = self._prepare(call, steps)
                if prepared.rejection is not None or not prepared.policy_decision.allowed:
                    # Preflight errors are serial barriers: commit preceding
                    # results before publishing this call's ordered result.
                    await self._flush_prepared_tool_calls(pending)
                    if self.pause is not None:
                        break
                    guarded = prepared.rejection or self.recovery.policy_blocked(
                        tool_name=prepared.tool_name, arguments=prepared.arguments,
                        call_id=prepared.call_id, policy_decision=prepared.policy_decision,
                    )
                    self._start(prepared)
                    result = ToolCallResult.from_output(
                        call_id=prepared.call_id, name=prepared.tool_name, output=guarded.output, fallback_status="error",
                    )
                    self._record_call_result(prepared, result)
                    await self._process_tool_result(prepared, result, guarded=guarded)
                else:
                    self._start(prepared)
                    pending.append(prepared)
            await self._flush_prepared_tool_calls(pending)
            if self.pause is not None:
                break
        self.pause = self.pause or self.recovery.finish_round(len(calls))
        return BatchOutcome(self.pause)
