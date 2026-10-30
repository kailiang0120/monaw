"""Shape how a turn ends: its status, its ``done`` event and its observed run."""

from __future__ import annotations

from typing import Any, Iterable

from app.agent.observability.ports import ObservabilityPort
from app.agent.observability.recorder import UsageStats

OK_TOOL_STATUSES = frozenset({"ok", "complete", "success"})


def tool_error_count(tool_calls: Iterable[dict]) -> int:
    return sum(1 for tool_call in tool_calls if str(tool_call.get("status") or "").lower() not in OK_TOOL_STATUSES)


def turn_status(
    *,
    stop_requested: bool,
    iteration_limit_hit: bool,
    budget_exhausted: bool,
    terminal_error: bool,
) -> str:
    """A turn that ended for any reason other than a final answer is paused, so it can resume."""
    if stop_requested or iteration_limit_hit or budget_exhausted or terminal_error:
        return "paused"
    return "complete"


def done_payload(
    conversation_id: str,
    summary: str,
    status: str,
    *,
    attachments: list[dict] | None = None,
    duration_ms: int | None = None,
    incomplete: bool = False,
    reason_code: str = "",
) -> dict[str, Any]:
    data: dict[str, Any] = {"conversation_id": conversation_id, "summary": summary, "status": status}
    if attachments is not None:
        data["attachments"] = attachments
    if duration_ms is not None:
        data["response_duration_ms"] = duration_ms
    if incomplete:
        data["incomplete"] = True
        if reason_code:
            data["reason_code"] = reason_code
    return data


def finish_observed_run(
    observability: ObservabilityPort,
    *,
    run_id: str,
    status: str,
    final_output: str,
    failure_reason: str = "",
    duration_ms: int | None = None,
    usage: UsageStats | None = None,
    tool_calls: list[dict] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Close the observed run; tool counts are reported only when the turn's calls are known."""
    kwargs: dict[str, Any] = {}
    if duration_ms is not None:
        kwargs["duration_ms"] = duration_ms
    if usage is not None:
        kwargs["usage"] = usage
    if tool_calls is not None:
        kwargs["tool_count"] = len(tool_calls)
        kwargs["tool_error_count"] = tool_error_count(tool_calls)
    if metadata is not None:
        kwargs["metadata"] = metadata
    observability.finish_run(
        run_id=run_id,
        status=status,
        final_output=final_output,
        failure_reason=failure_reason,
        **kwargs,
    )
