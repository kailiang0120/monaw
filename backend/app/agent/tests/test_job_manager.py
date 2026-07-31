import asyncio

from app.agent.job_manager import JobState, RunState


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



def test_job_state_uses_typed_terminal_transitions():
    job = JobState(job_id="job-1", conversation_id="conv-1")

    job.transition(RunState.CANCELLED)
    job.transition(RunState.DONE)

    assert job.status == RunState.CANCELLED
    assert job.status == "cancelled"


def test_job_append_event_counts_bounded_subscriber_overflow():
    job = JobState(job_id="job-1", conversation_id="conv-1")
    queue = asyncio.Queue(maxsize=1)
    job._subscribers.append(queue)

    asyncio.run(job.append_event({"event": "token", "data": {"content": "one"}}))
    asyncio.run(job.append_event({"event": "token", "data": {"content": "two"}}))

    assert queue.qsize() == 1
    assert job.dropped_subscriber_events == 1
