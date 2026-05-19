import json
from datetime import datetime, timedelta, timezone

from app.agent.observability.trajectory import TrajectoryLogger


def test_trajectory_logger_lists_and_loads_runs(tmp_path):
    logger = TrajectoryLogger(output_dir=tmp_path)

    logger.start("conv-1", model="gpt-test", provider="openai")
    logger.add_entry("conv-1", "human", "Create a cron task")
    logger.add_entry("conv-1", "tool", '{"status":"ok"}', tool_name="scheduled_task_create")
    logger.add_entry("conv-1", "gpt", "Created.")
    logger.finish("conv-1", success=True)

    runs = logger.list_runs()
    payload = logger.get_run(runs[0]["run_id"])

    assert len(runs) == 1
    assert runs[0]["conversation_id"] == "conv-1"
    assert runs[0]["tool_count"] == 1
    assert runs[0]["last_user_message"] == "Create a cron task"
    assert payload is not None
    assert payload["model"] == "gpt-test"


def test_trajectory_logger_prunes_success_after_one_day_and_failures_after_three_days(tmp_path):
    now = datetime.now(timezone.utc)
    success_path = tmp_path / "trajectory_samples.jsonl"
    failed_path = tmp_path / "failed_trajectories.jsonl"

    old_success = _trajectory_payload("old-success", now - timedelta(days=1, minutes=1), success=True)
    recent_success = _trajectory_payload("recent-success", now - timedelta(hours=23), success=True)
    recent_failure = _trajectory_payload("recent-failure", now - timedelta(days=2, hours=23), success=False)
    old_failure = _trajectory_payload("old-failure", now - timedelta(days=3, minutes=1), success=False)

    success_path.write_text(
        "\n".join(json.dumps(item) for item in [old_success, recent_success]) + "\n",
        encoding="utf-8",
    )
    failed_path.write_text(
        "\n".join(json.dumps(item) for item in [recent_failure, old_failure]) + "\n",
        encoding="utf-8",
    )

    logger = TrajectoryLogger(output_dir=tmp_path)
    logger.cleanup_expired(now=now)

    run_ids = {run["run_id"] for run in logger.list_runs()}
    assert run_ids == {"recent-success", "recent-failure"}
    assert logger.get_run("old-success") is None
    assert logger.get_run("old-failure") is None


def _trajectory_payload(run_id: str, finished_at: datetime, *, success: bool) -> dict:
    return {
        "run_id": run_id,
        "conversation_id": f"conv-{run_id}",
        "model": "gpt-test",
        "provider": "openai",
        "conversations": [
            {"from": "human", "value": "Do work"},
            {"from": "gpt", "value": "Done"},
        ],
        "started_at": (finished_at - timedelta(minutes=5)).isoformat(),
        "finished_at": finished_at.isoformat(),
        "success": success,
        "error": "" if success else "failed",
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "metadata": {},
    }
