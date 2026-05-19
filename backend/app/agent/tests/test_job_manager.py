import asyncio

from app.agent.job_manager import JobState


def test_job_done_event_with_incomplete_flag_sets_paused_status():
    job = JobState(job_id="job-1", conversation_id="conv-1")

    asyncio.run(job.append_event({
        "event": "done",
        "data": {
            "conversation_id": "conv-1",
            "summary": "Paused.",
            "incomplete": True,
            "reason_code": "iteration_limit_reached_after_tool",
        },
    }))

    assert job.status == "paused"
