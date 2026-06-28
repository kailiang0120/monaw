import json
import logging

import pytest

from app.agent import run_context

from app.agent.secure_artifacts import decrypt_bytes
from app.agent.observability import recorder as recorder_module
from app.agent.observability.recorder import ObservabilityLoggingHandler, ObservabilityRecorder, UsageStats


def test_observability_recorder_writes_jsonl_sqlite_and_redacts_secrets(tmp_path):
    recorder = ObservabilityRecorder(root=tmp_path, capture_sensitive_content=True)

    run_id = recorder.start_run(
        conversation_id="conv-1",
        user_message="Use Bearer abcdefghijklmnopqrstuvwxyz",
        model="gpt-test",
        provider="openai",
    )
    recorder.log_event(
        run_id=run_id,
        conversation_id="conv-1",
        event_type="llm_call_finished",
        error_message=(
            "failed with Bearer abcdefghijklmnopqrstuvwxyz and sk-secretsecretsecretsecret "
            "password=hunter2 cookie=sessionid=abc123 ghp_abcdefghijklmnopqrstuvwxyz123456"
        ),
        input={"api_key": "sk-secretsecretsecretsecret", "prompt": "hello", "password": "hunter2"},
        output={"authorization": "Bearer abcdefghijklmnopqrstuvwxyz", "text": "cookie=sessionid=abc123"},
        tokens=UsageStats(input_tokens=10, output_tokens=5, total_tokens=15, source="provider"),
    )
    recorder.finish_run(
        run_id=run_id,
        status="complete",
        final_output="done",
        usage=UsageStats(input_tokens=10, output_tokens=5, total_tokens=15, source="provider"),
    )

    detail = recorder.get_run(run_id, include_sensitive=True)
    assert detail is not None
    assert detail["status"] == "complete"
    assert detail["events"][1]["input"]["api_key"] == "[REDACTED]"
    assert detail["events"][1]["input"]["password"] == "[REDACTED]"
    assert detail["events"][1]["output"]["authorization"] == "[REDACTED]"
    assert detail["events"][1]["output"]["text"] == "cookie=[REDACTED]"
    assert detail["events"][1]["error_message"] == (
        "failed with Bearer [REDACTED] and sk-[REDACTED] "
        "password=[REDACTED] cookie=[REDACTED] [REDACTED GITHUB TOKEN]"
    )

    event_files = list((tmp_path / "events").glob("*.jsonl"))
    assert event_files
    event_lines = [json.loads(line) for line in event_files[0].read_text(encoding="utf-8").splitlines()]
    assert event_lines[1]["input"]["encrypted"] == "fernet"
    assert "hunter2" not in event_files[0].read_text(encoding="utf-8")
    assert event_lines[1]["error_message"] == (
        "failed with Bearer [REDACTED] and sk-[REDACTED] "
        "password=[REDACTED] cookie=[REDACTED] [REDACTED GITHUB TOKEN]"
    )


def test_observability_default_run_detail_omits_prompt_and_tool_bodies(tmp_path):
    recorder = ObservabilityRecorder(root=tmp_path)

    run_id = recorder.start_run(
        conversation_id="conv-sensitive",
        user_message="secret prompt",
        model="gpt-test",
        provider="openai",
    )
    recorder.log_event(
        run_id=run_id,
        conversation_id="conv-sensitive",
        event_type="tool_call_finished",
        tool_name="example",
        input={"prompt": "secret prompt"},
        output={"text": "raw tool output"},
    )
    recorder.finish_run(run_id=run_id, status="complete", final_output="final secret")

    detail = recorder.get_run(run_id)
    runs = recorder.list_runs()

    assert detail is not None
    assert detail["user_message"] == ""
    assert detail["final_output"] == ""
    assert detail["events"][1]["input"] is None
    assert detail["events"][1]["output"] is None
    assert runs[0]["user_message"] == ""
    assert runs[0]["final_output"] == ""
    event_files = list((tmp_path / "events").glob("*.jsonl"))
    raw = event_files[0].read_text(encoding="utf-8")
    assert "secret prompt" not in raw
    assert "raw tool output" not in raw


def test_debug_bundle_export_is_encrypted_and_safely_bounded(tmp_path):
    recorder = ObservabilityRecorder(root=tmp_path, capture_sensitive_content=True)
    run_id = recorder.start_run(
        conversation_id="conv-export",
        user_message="Use token sk-secretsecretsecretsecret",
    )
    recorder.finish_run(run_id=run_id, status="complete", final_output="done")

    result = recorder.export_debug_bundle(run_id)
    bundle_path = tmp_path / "exports" / result["filename"]

    assert result["encrypted"] is True
    assert bundle_path.suffix == ".enc"
    raw = bundle_path.read_bytes()
    assert b"conv-export" not in raw
    decrypted = decrypt_bytes(tmp_path / "exports", raw)
    payload = json.loads(decrypted.decode("utf-8"))
    assert payload["run"]["conversation_id"] == "conv-export"
    assert payload["run"]["user_message"] == "Use token sk-[REDACTED]"


def test_debug_bundle_export_rejects_symlink_export_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = tmp_path / "linked_exports"
    try:
        symlink.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")

    recorder = ObservabilityRecorder(root=tmp_path / "obs", capture_sensitive_content=True)
    recorder.exports_dir = symlink
    run_id = recorder.start_run(conversation_id="conv-symlink", user_message="hello")
    recorder.finish_run(run_id=run_id, status="complete")

    with pytest.raises(ValueError):
        recorder.export_debug_bundle(run_id)


def test_observability_summary_exposes_storage_runtime_and_classification(tmp_path):
    recorder = ObservabilityRecorder(root=tmp_path)
    run_id = recorder.start_run(conversation_id="conv-summary", user_message="hello")
    recorder.log_event(run_id=run_id, conversation_id="conv-summary", event_type="tool_call_finished", duration_ms=25)
    recorder.finish_run(run_id=run_id, status="complete")

    summary = recorder.summary()

    assert summary["storage_metrics"]["retention_days"] > 0
    assert "dropped_events" in summary["runtime_metrics"]
    assert "field_classification" in summary
    assert summary["field_classification"]["run"]["user_message"] == "sensitive_content"


def test_turn_timeout_can_override_wait_for_cancelled_run(tmp_path):
    recorder = ObservabilityRecorder(root=tmp_path)

    run_id = recorder.start_run(
        conversation_id="conv-timeout",
        user_message="slow task",
        model="gpt-test",
        provider="openai",
    )
    recorder.finish_run(
        run_id=run_id,
        status="paused",
        final_output="Stopped before final answer.",
        failure_reason="cancelled",
    )
    recorder.finish_open_run_for_conversation(
        conversation_id="conv-timeout",
        status="paused",
        failure_reason="turn_timeout",
        final_output="Timed out.",
    )

    detail = recorder.get_run(run_id)
    assert detail is not None
    assert detail["status"] == "paused"
    assert detail["failure_reason"] == "turn_timeout"
    assert detail["failure_pattern"] == "timeout"


def test_observability_logging_handler_captures_logger_exception(tmp_path, monkeypatch):
    recorder = ObservabilityRecorder(root=tmp_path)
    monkeypatch.setattr(recorder_module, "_RECORDER", recorder)

    logger = logging.getLogger("test.observability.exception")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    logger.addHandler(ObservabilityLoggingHandler(level=logging.ERROR))

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("captured failure")

    errors = recorder.list_errors()
    assert len(errors) == 1
    assert errors[0]["message"] == "captured failure"
    assert errors[0]["error_type"] == "RuntimeError"
    assert "RuntimeError: boom" in errors[0]["traceback"]


def test_observability_recorder_stores_replay_comparison(tmp_path):
    recorder = ObservabilityRecorder(root=tmp_path)

    result = recorder.record_replay(
        source_run_id="run-source",
        replay_run_id="run-replay",
        conversation_id="replay-1",
        status_change="paused->complete",
        duration_delta_ms=-120,
        token_delta=42,
        tool_sequence_diff="different",
        failure_reason_diff="timeout->",
    )

    detail = recorder.get_run("run-source")
    assert detail is None
    assert result["source_run_id"] == "run-source"

    with recorder._connect() as conn:  # noqa: SLF001 - focused storage assertion
        row = conn.execute("SELECT * FROM observability_replays WHERE replay_id = ?", (result["replay_id"],)).fetchone()
    assert row["status_change"] == "paused->complete"
    assert row["token_delta"] == 42


def test_backend_log_tail_redacts_inline_secrets(tmp_path, monkeypatch):
    log_path = tmp_path / "backend.log"
    log_path.write_text(
        "ok\npassword=hunter2\ncookie=sessionid=abc123\nghp_abcdefghijklmnopqrstuvwxyz123456\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(recorder_module, "BACKEND_LOG_PATH", log_path)

    recorder = ObservabilityRecorder(root=tmp_path / "obs")
    payload = recorder.read_backend_log_tail(tail=10)

    assert payload["exists"] is True
    assert payload["lines"] == [
        "ok",
        "password=[REDACTED]",
        "cookie=[REDACTED]",
        "[REDACTED GITHUB TOKEN]",
    ]



def test_observability_records_execution_principal_metadata(tmp_path):
    recorder = ObservabilityRecorder(root=tmp_path)
    tokens = [
        (run_context.reset_current_interactive, run_context.set_current_interactive(False)),
        (run_context.reset_current_execution_source, run_context.set_current_execution_source("telegram")),
        (run_context.reset_current_conversation_id, run_context.set_current_conversation_id("conv-telegram")),
        (run_context.reset_current_principal_id, run_context.set_current_principal_id("telegram:1:kai")),
        (run_context.reset_current_permission_profile_id, run_context.set_current_permission_profile_id("telegram:1:restricted")),
    ]
    try:
        run_id = recorder.start_run(
            conversation_id="conv-telegram",
            user_message="hello",
            source="telegram",
            metadata={"custom": "value"},
        )
        recorder.log_event(
            run_id=run_id,
            conversation_id="conv-telegram",
            event_type="tool_call_finished",
            metadata={"policy": "restricted"},
        )
    finally:
        for reset, token in reversed(tokens):
            reset(token)

    detail = recorder.get_run(run_id, include_sensitive=True)
    assert detail is not None
    assert detail["metadata"]["custom"] == "value"
    assert detail["metadata"]["execution_source"] == "telegram"
    assert detail["metadata"]["principal_id"] == "telegram:1:kai"
    assert detail["metadata"]["permission_profile_id"] == "telegram:1:restricted"
    assert detail["metadata"]["interactive"] is False
    assert detail["events"][-1]["metadata"]["policy"] == "restricted"
    assert detail["events"][-1]["metadata"]["principal_id"] == "telegram:1:kai"
