"""Single-turn agent tool loop."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import uuid
from collections import defaultdict
from typing import AsyncIterator, Callable

from app.agent.execution_gate import ExecutionGateService
from app.agent.iteration_budget import IterationBudget
from app.agent.llm_client import LLMClient
from app.agent.conversation_workspace import selected_workspace_dir
from app.agent.memory_manager import MemoryManager
from app.agent.observability.recorder import (
    UsageStats,
    get_observability_recorder,
    infer_source,
    reset_current_run_id,
    set_current_run_id,
)
from app.agent.observability.ports import ObservabilityPort
from app.agent.response_attachments import collect_response_attachments, public_runtime_attachments
from app.agent.run_events import RunEventPublisher
from app.agent.run_context import (
    current_interactive,
    reset_current_conversation_id,
    set_current_conversation_id,
)
from app.agent.steering import SteeringInbox
from app.agent.harness.tool_executor import ToolExecutor
from app.agent.harness.run_outcome import turn_status
from app.agent.harness import turn_lifecycle
from app.agent.harness.turn_lifecycle import (
    TurnLifecycle, TurnOutcome, TurnStopReason, drain_task,
)
from app.agent.tool_registry import ToolRegistry
from app.agent.tool_cancellation import set_tool_scope, reset_tool_scope
from app.agent.harness.workers import unfinished_workers
from app.agent.tool_observations import attachment_image_payloads
from app.agent.harness.tool_runner import ToolBatchRunner
from app.agent.harness.model_request import ModelRequestRunner, ModelRequestError
from app.agent.harness.turn_transcript import TurnTranscript
from app.agent.harness.turn_observer import TurnObserver
from app.agent.harness.response_policy import TurnResponsePolicy
from app.agent.durable.turn_run import register_active_lifecycle, unregister_active_lifecycle
from app.agent.durable.models import is_safe_identifier, sanitize_public_evidence
from app.agent.turn_text import (
    FINAL_ANSWER_TOOL_NAME,
    GUARD_WRAP_UP_REASON_CODES,
    completion_protocol_prompt,
    conversation_title_from_prompt,
    emit_progress_message,
    emit_typewriter_tokens,
    final_answer_text,
    guard_wrap_up_prompt,
)

logger = logging.getLogger(__name__)

_SESSION_LOCKS: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
HEARTBEAT_INTERVAL_S = turn_lifecycle.HEARTBEAT_INTERVAL_S


def conversation_turn_running(conversation_id: str) -> bool:
    lock = _SESSION_LOCKS.get(conversation_id)
    return lock is not None and lock.locked()


def conversation_lock(conversation_id: str) -> asyncio.Lock:
    """Shared serialization lock used by runtime entry points."""
    return _SESSION_LOCKS[conversation_id]


class TurnLoop:
    MAX_ITERATIONS = 200
    # A turn nobody is watching (scheduled task, Telegram) has no Stop button,
    # so it keeps a limit even when chats have none.
    UNATTENDED_MAX_ITERATIONS = 200
    # Guard stops: the turn ends after this many rounds in a row in which every
    # tool call was blocked or repeated, or after this many guard blocks in all.
    STUCK_ROUNDS_LIMIT = 3
    GUARD_BLOCKS_LIMIT = 10
    # Share of max_tokens_per_turn at which the user is told the task will pause.
    TOKEN_WARNING_FRACTION = 0.8

    def __init__(
        self,
        llm_client: LLMClient,
        registry: ToolRegistry,
        memory: MemoryManager,
        budget: IterationBudget | None = None,
        max_iterations: int | None = None,
        max_turn_seconds: float = 7200.0,
        max_llm_call_seconds: float = 600.0,
        observability: ObservabilityPort | None = None,
        max_parallel_tool_calls: int = 4,
        max_tokens_per_turn: int = 0,
    ) -> None:
        self.llm_client = llm_client
        self.registry = registry
        self.memory = memory
        # 0 sets no limit for interactive turns.
        self._max_iterations = max_iterations if max_iterations is not None else self.MAX_ITERATIONS
        seed_budget = budget or IterationBudget(max_iterations=self._max_iterations or None)
        self._turn_budget_max_iterations = seed_budget.max_iterations
        self.max_turn_seconds = max_turn_seconds
        self.max_llm_call_seconds = max_llm_call_seconds
        self.max_parallel_tool_calls = max(1, int(max_parallel_tool_calls))
        # Model tokens (input and output, as providers report them) per turn; 0 is no limit.
        self.max_tokens_per_turn = max(0, int(max_tokens_per_turn))
        self.executor = ToolExecutor()
        self.max_identical_calls = 2
        self.execution_gate = ExecutionGateService(executor=self.executor)
        self.observability: ObservabilityPort = observability or get_observability_recorder()

    def shutdown(self) -> None:
        self.executor.shutdown()

    def run_on_affinity(self, affinity_group: str, fn) -> bool:
        return self.executor.run_on_affinity(affinity_group, fn)

    conversation_lock = staticmethod(conversation_lock)

    def _new_turn_budget(self) -> IterationBudget:
        limit = self._turn_budget_max_iterations
        if limit is None and not current_interactive():
            limit = self.UNATTENDED_MAX_ITERATIONS
        return IterationBudget(max_iterations=limit)

    _describe_tool_step = staticmethod(ToolBatchRunner._describe_tool_step)

    async def _react_worker(
        self,
        message: str,
        conversation_id: str,
        system_prompt: str | Callable[[], str],
        publisher: RunEventPublisher,
        attachments: list[dict] | None = None,
        steering: SteeringInbox | None = None,
        *,
        lifecycle: TurnLifecycle,
    ) -> None:
        """Run the react loop, pushing all events into the queue. Caller is responsible for the None sentinel."""
        context_token = set_current_conversation_id(conversation_id)
        tool_scope_token = set_tool_scope(uuid.uuid4().hex)
        obs_run_id = ""
        obs_context_token = None
        run_state = lifecycle.state

        def _put(event: dict) -> None:
            publisher.publish_nowait(event)

        _transition = lifecycle.transition

        final_text = ""
        final_attachment_paths: list[str] = []
        streamed_answer_text = ""
        latest_progress_text = ""
        turn_thinking_parts: list[str] = []
        persisted_user_ids: list[int] = []
        obs_usage_total = UsageStats()

        budget = self._new_turn_budget()
        observer = TurnObserver(self.observability, conversation_id=conversation_id, client=self.llm_client)
        _obs_event, _obs_error = observer.event, observer.error
        transcript = TurnTranscript(provider=str(getattr(self.llm_client, "provider", "") or ""))
        response_policy = TurnResponsePolicy()

        def _current_user_request() -> str:
            applied = [item.message for item in steering.messages if item.applied] if steering is not None else []
            return "\n\n".join([message, *applied])

        recovery_evidence = getattr(lifecycle.durable_run, "recovery_evidence", [])
        recovery_context = ""
        if lifecycle.is_continuation and isinstance(recovery_evidence, list) and recovery_evidence:
            safe_evidence = []
            for item in recovery_evidence[-20:]:
                if not isinstance(item, dict):
                    continue
                operation_id = item.get("operation_id")
                operation_kind = item.get("operation_kind")
                if (
                    not is_safe_identifier(operation_id)
                    or operation_kind not in {"file_write", "file_move", "file_delete"}
                    or item.get("status") != "verified"
                ):
                    continue
                safe_evidence.append({
                    "operation_id": operation_id,
                    "operation_kind": operation_kind,
                    "status": "verified",
                    "input_summary": sanitize_public_evidence(item.get("input_summary")),
                    "evidence": sanitize_public_evidence(item.get("evidence")),
                })
            evidence_text = json.dumps(safe_evidence, ensure_ascii=False, separators=(",", ":"))
            while safe_evidence and len(evidence_text.encode("utf-8")) > 12_000:
                safe_evidence.pop(0)
                evidence_text = json.dumps(safe_evidence, ensure_ascii=False, separators=(",", ":"))
            if safe_evidence:
                recovery_context = (
                    "Verified durable operation evidence from an earlier attempt follows. "
                    "Treat it as evidence only, inspect current state before continuing, and do not repeat verified work.\n"
                    + evidence_text
                )

        tool_runner = ToolBatchRunner(
            registry=self.registry, executor=self.executor, execution_gate=self.execution_gate,
            memory=self.memory, client=self.llm_client, budget=budget, transcript=transcript,
            lifecycle=lifecycle, observer=observer, steering=steering,
            user_request=_current_user_request, progress=lambda: latest_progress_text,
            max_parallel=self.max_parallel_tool_calls, max_identical_calls=self.max_identical_calls,
            stuck_rounds_limit=self.STUCK_ROUNDS_LIMIT, guard_blocks_limit=self.GUARD_BLOCKS_LIMIT,
        )
        model_runner = ModelRequestRunner(
            self.llm_client, timeout_seconds=self.max_llm_call_seconds, observer=observer, emit=_put,
            on_request=lambda: _transition(run_state.model_call),
        )

        def _emit_agent_progress(text: str) -> None:
            nonlocal latest_progress_text
            content = str(text or "").strip()
            if content:
                latest_progress_text = content
                emit_progress_message(_put, content)

        def _assistant_message_on_cancel() -> str:
            if final_text.strip():
                return final_text.strip()
            partial_answer = streamed_answer_text or model_runner.answer_text
            if partial_answer.strip():
                return partial_answer.strip()
            if latest_progress_text.strip():
                return (
                    "Stopped before final answer.\n\n"
                    f"Last progress: {latest_progress_text.strip()}"
                )
            if tool_runner.last_tool_name:
                return f"Stopped before final answer. Last tool: {tool_runner.last_tool_name}."
            return "Stopped before final answer."

        _tool_calls_for_persist = tool_runner.persistable_calls

        def _emit_response_token(content: str) -> None:
            _put({"event": "token", "data": {"content": content}})

        def _emit_response_event(event: dict) -> None:
            if event.get("event") == "token":
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                _emit_response_token(str(data.get("content") or ""))
                return
            _put(event)

        def _response_duration_ms() -> int:
            return lifecycle.duration_ms()

        async def _persist_turn(outcome: TurnOutcome) -> None:
            nonlocal persisted_user_ids
            if steering is not None:
                steering.closed = True
            # A steering message that arrived too late was never read; the app
            # queues it as the next turn, so it is not saved with this one.
            applied_steering = [item for item in steering.messages if item.applied] if steering is not None else []
            steering_messages = [item.message for item in applied_steering]
            attachment_kwargs = {}
            if attachments:
                attachment_kwargs["user_attachments"] = public_runtime_attachments(attachments)
            if any(item.attachments for item in applied_steering):
                attachment_kwargs["steering_attachments"] = [public_runtime_attachments(item.attachments) for item in applied_steering]
            response_duration_ms = _response_duration_ms()
            persist_turn = getattr(self.memory, "persist_turn", None)
            if callable(persist_turn):
                continuation_kwargs = {"persist_user_message": False} if lifecycle.is_continuation else {}
                persisted_ids = await persist_turn(
                    conversation_id,
                    message,
                    outcome.summary,
                    turn_id=lifecycle.turn_id,
                    tool_calls=_tool_calls_for_persist(),
                    thinking="\n\n".join(turn_thinking_parts),
                    status=outcome.status,
                    response_duration_ms=response_duration_ms,
                    response_attachments=outcome.attachments,
                    **({
                        "steering_messages": steering_messages,
                        "steering_tool_call_indexes": [item.tool_call_index for item in applied_steering],
                    } if steering_messages else {}),
                    **attachment_kwargs,
                    **continuation_kwargs,
                )
                if isinstance(persisted_ids, list):
                    persisted_user_ids = [item for item in persisted_ids if isinstance(item, int)]
            else:
                if lifecycle.is_continuation:
                    raise RuntimeError("Safe continuation requires atomic turn checkpoint persistence.")
                await self.memory.add_message_and_maybe_summarize(
                    conversation_id, "user", message
                )
                for steering_message in steering_messages:
                    await self.memory.add_message_and_maybe_summarize(conversation_id, "user", steering_message)
                await self.memory.add_message_and_maybe_summarize(
                    conversation_id, "assistant", outcome.summary
                )

        try:
            obs_run_id = self.observability.start_run(
                conversation_id=conversation_id,
                user_message=message,
                source=infer_source(conversation_id),
                model=str(getattr(self.llm_client, "model_name", "") or ""),
                provider=str(getattr(self.llm_client, "provider", "") or ""),
                metadata={"attachment_count": len(attachments or [])},
            )
            lifecycle.run_id = observer.run_id = obs_run_id
            obs_context_token = set_current_run_id(obs_run_id)
            self.memory.get_or_create(
                conversation_id, title=conversation_title_from_prompt(message)
            )
            self.memory.set_task_goal(conversation_id, message)
            self.memory.set_active_task(conversation_id, None)
            if unfinished_workers(conversation_id):
                final_text = "Paused: a previous tool is still stopping. Wait for it to finish, then continue and verify its last action."
                _put({"event": "error", "data": {"code": "tool_still_running", "message": final_text}})
                await lifecycle.finalize(
                    TurnOutcome(final_text, "paused", "tool_still_running"),
                    persist=_persist_turn, usage=obs_usage_total, tool_calls=_tool_calls_for_persist(),
                )
                return
            build_long_term_context = getattr(self.memory, "build_long_term_memory_context", None)
            build_long_term_context_async = getattr(self.memory, "build_long_term_memory_context_async", None)
            if callable(build_long_term_context_async):
                long_term_context = await build_long_term_context_async(message, conversation_id=conversation_id, workspace=selected_workspace_dir())
            else:
                long_term_context = (build_long_term_context(message, conversation_id=conversation_id, workspace=selected_workspace_dir()) if callable(build_long_term_context) else "")

            tool_runner.initialize_review_history()

            def _current_system_prompt() -> str:
                base_prompt = system_prompt() if callable(system_prompt) else system_prompt
                if long_term_context:
                    base_prompt = f"{base_prompt}\n\n{long_term_context}" if base_prompt else long_term_context
                if recovery_context:
                    base_prompt = f"{base_prompt}\n\n{recovery_context}" if base_prompt else recovery_context
                if tool_runner.review_state.denials:
                    base_prompt = f"{base_prompt}\n\n{tool_runner.review_state.instructions()}"
                return completion_protocol_prompt(base_prompt)

            initial_visible_tools = self.registry.get_all_tools(visible_only=True)
            await self.memory.ensure_context_fits(
                conversation_id,
                _current_system_prompt(),
                tools=initial_visible_tools,
                current_user_message=message,
            )
            transcript.messages = self.memory.build_llm_messages(conversation_id)
            durable_run = lifecycle.durable_run
            persist_recovery_input = getattr(durable_run, "persist_recovery_input", None)
            if durable_run is not None and not lifecycle.is_continuation and callable(persist_recovery_input):
                persist_recovery_input(
                    conversation_id,
                    message,
                    attachments=public_runtime_attachments(attachments) if attachments else None,
                )
            initial_message = {"role": "user", "content": message}
            initial_images = attachment_image_payloads(attachments)
            if initial_images:
                initial_message["images"] = initial_images
                initial_message["ephemeral"] = True
            transcript.messages.append(initial_message)
            transcript.latest_round_start = len(transcript.messages) - 1

            def _apply_steering() -> bool:
                nonlocal streamed_answer_text
                pending = steering.drain() if steering is not None else []
                for item in pending:
                    item.tool_call_index = len(tool_runner.history)
                    entry = {"role": "user", "content": item.message}
                    images = attachment_image_payloads(item.attachments)
                    if images:
                        entry.update({"images": images, "ephemeral": True})
                    transcript.steer(entry)
                    _put({"event": "steering_applied", "data": {
                        "message_id": item.id,
                        "message": item.display_message or item.message,
                        "attachments": item.public_attachments,
                        "reset_response": True,
                    }})
                    _obs_event("steering_applied", input={"message": item.message}, metadata={"message_id": item.id})
                if pending:
                    streamed_answer_text = ""
                    model_runner.answer_text = ""
                    self.memory.set_task_goal(conversation_id, _current_user_request())
                return bool(pending)

            budget_exhausted = False
            completed_normally = False
            iteration_limit_hit = False
            terminal_error = False
            incomplete_reason_code = ""
            stop_requested = False
            # Pause notices wait until the wrap-up call has had its chance to answer.
            pause_error_event: dict | None = None
            token_warning_sent = False
            rounds = itertools.count() if budget.max_iterations is None else range(self._max_iterations or budget.max_iterations)
            for iteration_index in rounds:
                if stop_requested:
                    break
                if lifecycle.review_requested:
                    terminal_error = True
                    incomplete_reason_code = lifecycle.review_reason_code or "external_effect_unknown"
                    final_text = "This run needs review before it can safely continue."
                    break
                if lifecycle.pause_requested:
                    stop_requested = True
                    incomplete_reason_code = "user_pause"
                    final_text = "Paused at a safe point. Continue to resume from the saved conversation."
                    break
                if _apply_steering():
                    response_policy.steered()
                token_limit = self.max_tokens_per_turn
                tokens_used = obs_usage_total.total_tokens
                if token_limit and tokens_used >= token_limit:
                    final_text = (
                        f"Paused: this task used {tokens_used:,} tokens, over its limit of {token_limit:,}. "
                        "Continue to resume, or change the limit in Settings > Model."
                    )
                    budget_exhausted = True
                    incomplete_reason_code = "token_budget_exhausted"
                    logger.warning(
                        "turn_loop paused: token budget exhausted conversation_id=%s limit=%s used=%s",
                        conversation_id,
                        token_limit,
                        tokens_used,
                    )
                    pause_error_event = {
                        "event": "error",
                        "data": {
                            "code": incomplete_reason_code,
                            "message": final_text,
                            "limit": token_limit,
                            "consumed": tokens_used,
                        },
                    }
                    _obs_event(
                        "guardrail_triggered",
                        level="warning",
                        status="error",
                        error_code=incomplete_reason_code,
                        error_message=final_text,
                        metadata={"token_limit": token_limit, "tokens_used": tokens_used},
                    )
                    break
                if token_limit and not token_warning_sent and tokens_used >= token_limit * self.TOKEN_WARNING_FRACTION:
                    token_warning_sent = True
                    _put({"event": "progress", "data": {"content": (
                        f"This task has used {tokens_used:,} of its {token_limit:,}-token limit "
                        "and will pause when it reaches it."
                    )}})
                if not budget.consume():
                    final_text = "Iteration budget exhausted."
                    budget_exhausted = True
                    iteration_limit_hit = True
                    incomplete_reason_code = "iteration_budget_exhausted"
                    logger.warning(
                        "turn_loop paused: iteration budget exhausted before model call "
                        "conversation_id=%s limit=%s consumed=%s",
                        conversation_id,
                        budget.max_iterations,
                        budget.consumed,
                    )
                    pause_error_event = {
                        "event": "error",
                        "data": {
                            "code": "iteration_budget_exhausted",
                            "message": final_text,
                            "limit": budget.max_iterations,
                            "consumed": budget.consumed,
                        },
                    }
                    _obs_event(
                        "guardrail_triggered",
                        level="warning",
                        status="error",
                        error_code="iteration_budget_exhausted",
                        error_message=final_text,
                        metadata={"limit": budget.max_iterations, "consumed": budget.consumed},
                    )
                    break

                streamed_answer_text = ""
                try:
                    visible_tools = self.registry.get_all_tools(visible_only=True)
                    tool_choice = response_policy.take_tool_choice()
                    model_result = await model_runner.request(transcript.snapshot(
                        tools=visible_tools, system_prompt=_current_system_prompt(), tool_choice=tool_choice,
                        metadata={"iteration": iteration_index + 1, "consumed": budget.consumed},
                    ))
                    obs_usage_total = obs_usage_total.merge(model_result.usage)
                except ModelRequestError as exc:
                    final_text = str(exc)
                    terminal_error = True
                    incomplete_reason_code = exc.reason_code
                    _put({"event": "error", "data": {"code": exc.reason_code, "message": final_text}})
                    break

                if lifecycle.pause_requested:
                    transcript.accept(model_result)
                    transcript.close_unanswered_calls(
                        "user_pause", explanation="Not run: the turn was paused before the next tool action."
                    )
                    streamed_answer_text = model_result.answer_text
                    stop_requested = True
                    incomplete_reason_code = "user_pause"
                    final_text = streamed_answer_text.strip() or (
                        "Paused at a safe point. Continue to resume from the saved conversation."
                    )
                    break

                # Usage belongs to the turn even when steering makes a response stale.
                # Its fitted history and tool actions only belong to its original revision.
                if _apply_steering() or not transcript.accept(model_result):
                    response_policy.steered()
                    continue
                llm_response = model_result.response
                streamed_answer_text = model_result.answer_text
                streamed_reasoning_content = model_result.reasoning_text

                reasoning_content = str(getattr(llm_response, "reasoning_content", "") or "")
                if reasoning_content:
                    turn_thinking_parts.append(reasoning_content)
                    remaining_reasoning = (
                        reasoning_content[len(streamed_reasoning_content):]
                        if streamed_reasoning_content
                        and reasoning_content.startswith(streamed_reasoning_content)
                        else "" if streamed_reasoning_content else reasoning_content
                    )
                    if remaining_reasoning:
                        _put({"event": "thinking", "data": {"content": remaining_reasoning}})

                if llm_response.tool_calls:
                    transcript.begin_tool_round(llm_response)
                decision = response_policy.decide(
                    llm_response, tools_available=bool(visible_tools), history=tool_runner.history,
                    budget_exhausted=budget.exhausted,
                )
                if decision.progress:
                    _emit_agent_progress(decision.progress)
                if decision.action == "reprompt":
                    transcript.close_unanswered_calls(
                        "response_reprompted", explanation="The final answer needs revision before completion.",
                    )
                transcript.messages.extend(decision.messages)
                if decision.action == "reprompt":
                    continue
                if decision.action in {"final", "pause"}:
                    final_text = decision.text
                    final_attachment_paths = decision.attachment_paths
                    completed_normally = decision.action == "final"
                    terminal_error = decision.action == "pause"
                    incomplete_reason_code = decision.reason_code
                    if decision.emit_error:
                        _put({"event": "error", "data": {
                            "code": incomplete_reason_code, "message": final_text,
                            "limit": budget.max_iterations, "consumed": budget.consumed,
                        }})
                    break
                call_dicts = decision.calls
                if budget.exhausted:
                    iteration_limit_hit = True
                    budget_exhausted = True
                    incomplete_reason_code = "iteration_limit_reached_before_more_tools"
                    final_text = (
                        "Paused before executing more tools because the iteration limit is reached. "
                        "Continue to proceed from the current state."
                    )
                    logger.warning(
                        "turn_loop paused: iteration limit reached before more tools "
                        "conversation_id=%s limit=%s consumed=%s requested_tools=%s",
                        conversation_id,
                        budget.max_iterations,
                        budget.consumed,
                        [str(call.get("name") or "") for call in call_dicts],
                    )
                    pause_error_event = {
                        "event": "error",
                        "data": {
                            "code": incomplete_reason_code,
                            "message": final_text,
                            "limit": budget.max_iterations,
                            "consumed": budget.consumed,
                        },
                    }
                    _obs_event(
                        "guardrail_triggered",
                        level="warning",
                        status="error",
                        error_code=incomplete_reason_code,
                        error_message=final_text,
                        metadata={
                            "limit": budget.max_iterations,
                            "consumed": budget.consumed,
                            "requested_tools": [str(call.get("name") or "") for call in call_dicts],
                        },
                    )
                    break
                if lifecycle.pause_requested:
                    transcript.close_unanswered_calls(
                        "user_pause", explanation="Not run: the turn was paused before the next tool action."
                    )
                    stop_requested = True
                    incomplete_reason_code = "user_pause"
                    final_text = "Paused at a safe point. Continue to resume from the saved conversation."
                    break
                batch_outcome = await tool_runner.execute(call_dicts)
                if lifecycle.review_requested:
                    stop_requested = True
                    terminal_error = True
                    incomplete_reason_code = lifecycle.review_reason_code or "external_effect_unknown"
                    final_text = "This run needs review before it can safely continue."
                    break
                if lifecycle.pause_requested and batch_outcome.pause is None:
                    stop_requested = True
                    incomplete_reason_code = "user_pause"
                    final_text = "Paused at a safe point. Continue to resume from the saved conversation."
                    break
                if batch_outcome.pause is not None:
                    stop_requested = True
                    terminal_error = True
                    incomplete_reason_code = batch_outcome.pause.reason_code
                    final_text = batch_outcome.pause.summary
                transcript.close_unanswered_calls(
                    incomplete_reason_code or "final_answer_deferred",
                    explanation="Not run: the turn stopped first." if incomplete_reason_code else "The final answer was deferred while requested tools ran.",
                )

            if (
                not completed_normally
                and not stop_requested
                and not budget_exhausted
                and not terminal_error
                and not iteration_limit_hit
            ):
                iteration_limit_hit = True
                budget_exhausted = True
                if tool_runner.history:
                    tool_runner.last_tool_name = str(tool_runner.history[-1].get("tool_name") or tool_runner.last_tool_name)
                if tool_runner.last_tool_name:
                    incomplete_reason_code = "iteration_limit_reached_after_tool"
                    final_text = (
                        "Paused: iteration limit reached after the last tool result. "
                        "The task may be incomplete. Continue to resume from the current state."
                    )
                else:
                    incomplete_reason_code = "iteration_limit_reached_without_response"
                    final_text = (
                        "Paused: iteration limit reached before the agent produced a final answer. "
                        "Continue to resume from the current state."
                    )
                logger.warning(
                    "turn_loop paused: loop ended without final answer conversation_id=%s limit=%s "
                    "consumed=%s last_tool=%s",
                    conversation_id,
                    budget.max_iterations,
                    budget.consumed,
                    tool_runner.last_tool_name,
                )
                pause_error_event = {
                    "event": "error",
                    "data": {
                        "code": incomplete_reason_code,
                        "message": final_text,
                        "limit": budget.max_iterations,
                        "consumed": budget.consumed,
                        "last_tool": tool_runner.last_tool_name,
                    },
                }
                _obs_event(
                    "guardrail_triggered",
                    level="warning",
                    status="error",
                    error_code=incomplete_reason_code,
                    error_message=final_text,
                    metadata={
                        "limit": budget.max_iterations,
                        "consumed": budget.consumed,
                        "last_tool": tool_runner.last_tool_name,
                    },
                )

            if (
                not completed_normally
                and incomplete_reason_code in GUARD_WRAP_UP_REASON_CODES
                and budget.consumed > 0
            ):
                # A guard ended the loop, but the model is healthy: give it one
                # tool-less call to tell the user what it did and what stopped it.
                transcript.close_unanswered_calls(incomplete_reason_code)
                transcript.messages.append({"role": "user", "content": guard_wrap_up_prompt(incomplete_reason_code, final_text)})
                streamed_answer_text = ""

                try:
                    wrap_up_result = await model_runner.request(transcript.snapshot(
                        tools=[], system_prompt=_current_system_prompt(), tool_choice="none", stream_reasoning=False,
                        metadata={"wrap_up": True, "reason_code": incomplete_reason_code},
                    ))
                    transcript.accept(wrap_up_result)
                    wrap_up_response = wrap_up_result.response
                    obs_usage_total = obs_usage_total.merge(wrap_up_result.usage)
                except ModelRequestError:
                    wrap_up_response = None
                streamed_answer_text = model_runner.answer_text
                wrap_up_text = ""
                if wrap_up_response is not None:
                    wrap_up_text = str(wrap_up_response.content or "").strip()
                    if not wrap_up_text:
                        for call in wrap_up_response.tool_calls or []:
                            if call.tool_name == FINAL_ANSWER_TOOL_NAME:
                                call_args = call.arguments if isinstance(call.arguments, dict) else {}
                                wrap_up_text = final_answer_text(call_args, "").strip()
                if wrap_up_text:
                    final_text = wrap_up_text
                    pause_error_event = None
                    if not streamed_answer_text:
                        await emit_typewriter_tokens(_emit_response_event, final_text)
                    streamed_answer_text = final_text
                elif streamed_answer_text:
                    final_text = streamed_answer_text.strip() or final_text
                    pause_error_event = None
            if pause_error_event is not None:
                _put(pause_error_event)
            if lifecycle.review_requested:
                _put({"event": "error", "data": {
                    "code": lifecycle.review_reason_code or "external_effect_unknown",
                    "message": "This run needs review before it can safely continue.",
                }})

            if steering is not None:
                steering.closed = True
            run_status = "error" if lifecycle.review_requested else turn_status(
                stop_requested=stop_requested,
                iteration_limit_hit=iteration_limit_hit,
                budget_exhausted=budget_exhausted,
                terminal_error=terminal_error,
            )
            if (
                final_text
                and not streamed_answer_text
                and not budget_exhausted
                and not terminal_error
                and not iteration_limit_hit
            ):
                await emit_typewriter_tokens(_emit_response_event, final_text)
                streamed_answer_text = final_text

            response_attachments = collect_response_attachments(
                content=final_text,
                explicit_paths=final_attachment_paths,
                tool_calls=tool_runner.history,
            )
            await lifecycle.finalize(
                TurnOutcome(final_text or "Done.", run_status, incomplete_reason_code, response_attachments),
                persist=_persist_turn,
                usage=obs_usage_total,
                tool_calls=_tool_calls_for_persist(),
                metadata={
                    "completed_normally": completed_normally,
                    "budget_consumed": budget.consumed,
                    "budget_limit": budget.max_iterations,
                },
                clear_progress=lambda: self.memory.clear_task_progress(conversation_id),
            )

            # Memory learning reads only the user's words (including steering sent
            # mid-turn) and runs as a background task after the user-visible `done`,
            # so it never delays the turn.
            schedule_memory_learning = getattr(self.memory, "schedule_memory_learning", None)
            schedule_with_refs = getattr(self.memory, "schedule_memory_learning_with_refs", None)
            if callable(schedule_memory_learning) and not lifecycle.is_continuation:
                try:
                    if callable(schedule_with_refs) and persisted_user_ids:
                        schedule_with_refs(user_message=_current_user_request(), workspace=selected_workspace_dir(), conversation_id=conversation_id, message_ids=persisted_user_ids)
                    else:
                        schedule_memory_learning(user_message=_current_user_request(), workspace=selected_workspace_dir())
                except Exception:
                    logger.exception("schedule_memory_learning failed")
        except asyncio.CancelledError:
            reason = lifecycle.stop_reason or TurnStopReason.CANCELLED
            await tool_runner.cancel("turn_timeout" if reason == TurnStopReason.TIMEOUT else "user_stopped")
            if reason == TurnStopReason.TIMEOUT:
                partial_text = "Paused: task timed out."
                progress = _assistant_message_on_cancel()
                if progress != "Stopped before final answer.":
                    partial_text += f"\n\n{progress}"
                _put({"event": "error", "data": {
                    "code": reason.value,
                    "message": f"Turn exceeded {self.max_turn_seconds}s limit.",
                }})
                _obs_error(f"Turn exceeded {self.max_turn_seconds}s limit.", error_type=reason.value)
            else:
                partial_text = _assistant_message_on_cancel()
            if unfinished_workers(conversation_id):
                partial_text += "\n\nA tool is still stopping. Further actions are blocked until it finishes; verify its last action before retrying."
            await lifecycle.finalize(
                TurnOutcome(partial_text, "paused", reason.value),
                persist=_persist_turn, usage=obs_usage_total, tool_calls=_tool_calls_for_persist(),
            )
            raise
        except Exception as exc:
            await tool_runner.cancel()
            logger.exception("react_worker error: %s", exc)
            _obs_error(str(exc), error_type=type(exc).__name__, metadata={"phase": "turn_loop"})
            failure_text = "Paused after an internal error. The saved tool results can be used to continue."
            partial_text = final_text.strip() or streamed_answer_text.strip() or latest_progress_text.strip()
            if partial_text:
                failure_text += f"\n\nLast response: {partial_text}"
            _put({"event": "error", "data": {"code": "internal_error", "message": str(exc)}})
            await lifecycle.finalize(
                TurnOutcome(failure_text, "error", "internal_error"),
                persist=_persist_turn, usage=obs_usage_total, tool_calls=_tool_calls_for_persist(),
            )
        finally:
            memory_store = getattr(self.memory, "memory_store", None)
            if memory_store is not None and obs_run_id:
                memory_store.discard_pending_sources(obs_run_id)
            reset_tool_scope(tool_scope_token)
            if obs_context_token is not None:
                reset_current_run_id(obs_context_token)
            reset_current_conversation_id(context_token)

    async def run(
        self,
        message: str,
        conversation_id: str,
        *,
        system_prompt: str | Callable[[], str],
        attachments: list[dict] | None = None,
        steering: SteeringInbox | None = None,
        durable_run=None,
        durable_run_factory=None,
    ) -> AsyncIterator[dict]:
        async with _SESSION_LOCKS[conversation_id]:
            if durable_run is None and callable(durable_run_factory):
                durable_run = durable_run_factory()
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            last_flush: list[float] = [loop.time()]
            publisher = RunEventPublisher(queue, last_flush, loop=loop)
            lifecycle = TurnLifecycle(
                conversation_id,
                publisher,
                self.observability,
                durable_run=durable_run,
            )
            durable_run_id = str(getattr(durable_run, "run_id", "") or "")
            register_active_lifecycle(durable_run_id, lifecycle)
            worker_task = asyncio.create_task(lifecycle.drive(
                self._react_worker(
                    message, conversation_id, system_prompt, publisher, attachments,
                    steering=steering, lifecycle=lifecycle,
                ),
                timeout_seconds=self.max_turn_seconds,
            ))

            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    last_flush[0] = loop.time()
                    yield event
            finally:
                if not worker_task.done():
                    worker_task.cancel()
                await drain_task(worker_task)
                unregister_active_lifecycle(durable_run_id, lifecycle)
