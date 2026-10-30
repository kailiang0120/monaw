"""Contract tests for how a turn reports its end."""

from __future__ import annotations

from app.agent.harness.run_outcome import done_payload, finish_observed_run, tool_error_count, turn_status
from app.agent.observability.recorder import UsageStats


class _RecordingObservability:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def finish_run(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_tool_errors_count_every_status_except_success():
    calls = [
        {"status": "complete"},
        {"status": "OK"},
        {"status": "success"},
        {"status": "cancelled"},
        {"status": "error"},
        {},
    ]

    assert tool_error_count(calls) == 3


def test_any_early_stop_pauses_the_turn():
    clean = dict(stop_requested=False, iteration_limit_hit=False, budget_exhausted=False, terminal_error=False)

    assert turn_status(**clean) == "complete"
    for flag in clean:
        assert turn_status(**{**clean, flag: True}) == "paused"


def test_done_payload_includes_only_what_the_exit_knows():
    assert done_payload("c1", "Stopped.", "paused") == {
        "conversation_id": "c1",
        "summary": "Stopped.",
        "status": "paused",
    }
    assert done_payload(
        "c1", "Done.", "complete", attachments=[], duration_ms=12
    ) == {
        "conversation_id": "c1",
        "summary": "Done.",
        "status": "complete",
        "attachments": [],
        "response_duration_ms": 12,
    }


def test_done_payload_marks_incomplete_turns_with_their_reason():
    data = done_payload("c1", "Paused.", "paused", incomplete=True, reason_code="llm_timeout")
    assert data["incomplete"] is True
    assert data["reason_code"] == "llm_timeout"

    assert "reason_code" not in done_payload("c1", "Paused.", "paused", incomplete=True)
    assert "reason_code" not in done_payload("c1", "Done.", "complete", reason_code="ignored")


def test_finish_observed_run_reports_tool_counts_only_when_known():
    observability = _RecordingObservability()
    usage = UsageStats()

    finish_observed_run(
        observability,
        run_id="run-1",
        status="paused",
        final_output="Stopped.",
        failure_reason="cancelled",
        duration_ms=5,
        usage=usage,
        tool_calls=[{"status": "complete"}, {"status": "cancelled"}],
    )
    finish_observed_run(observability, run_id="run-2", status="paused", final_output="Waiting.")

    assert observability.calls[0] == {
        "run_id": "run-1",
        "status": "paused",
        "final_output": "Stopped.",
        "failure_reason": "cancelled",
        "duration_ms": 5,
        "usage": usage,
        "tool_count": 2,
        "tool_error_count": 1,
    }
    assert observability.calls[1] == {
        "run_id": "run-2",
        "status": "paused",
        "final_output": "Waiting.",
        "failure_reason": "",
    }
