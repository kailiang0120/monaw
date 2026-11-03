"""A desktop turn outlives its HTTP stream; only an explicit Stop ends it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agent.steering import RunStream, _inboxes, conversation_is_running, sse_chunk
from app.api.routes import chat as chat_routes
from app.schemas import ChatRequest, ChatStopRequest


def _request(session_id: str = "session-1"):
    return SimpleNamespace(state=SimpleNamespace(control_session=SimpleNamespace(session_id=session_id)))


@pytest.fixture
def turn(monkeypatch):
    """A scripted turn that waits on `release` before finishing."""
    from app.agent import runtime

    state = SimpleNamespace(release=asyncio.Event(), finished=False, cancelled=False)

    async def fake_stream(**kwargs):
        yield {"event": "token", "data": {"content": "working"}}
        try:
            await state.release.wait()
        except asyncio.CancelledError:
            state.cancelled = True
            raise
        state.finished = True
        yield {"event": "done", "data": {"conversation_id": kwargs["conversation_id"]}}

    monkeypatch.setattr(runtime, "run_agent_stream", fake_stream)
    monkeypatch.setattr(chat_routes, "_prepare_conversation_workspace", lambda req, conv_id: None)
    monkeypatch.setattr(chat_routes, "publish_ui_event", lambda *args, **kwargs: None)
    return state


def test_closing_the_stream_leaves_the_turn_running(turn):
    async def scenario():
        response = await chat_routes.chat(ChatRequest(message="hi", conversation_id="chat-a", run_id="run-a"), _request())
        body = response.body_iterator
        assert b"turn_started" in await body.__anext__()
        assert b"working" in await body.__anext__()
        await body.aclose()  # the window switched chats or closed

        assert conversation_is_running("chat-a")
        turn.release.set()
        await _inboxes.get("run-a").task if "run-a" in _inboxes else None
        await asyncio.sleep(0)
        assert turn.finished and not turn.cancelled
        assert not conversation_is_running("chat-a")

    asyncio.run(scenario())


def test_stop_ends_the_turn_even_when_nobody_is_watching(turn):
    async def scenario():
        response = await chat_routes.chat(ChatRequest(message="hi", conversation_id="chat-b", run_id="run-b"), _request())
        body = response.body_iterator
        await body.__anext__()
        await body.aclose()

        assert await chat_routes.stop_chat(ChatStopRequest(run_id="run-b"), _request()) == {"ok": True}
        assert turn.cancelled and not turn.finished
        assert not conversation_is_running("chat-b")
        assert await chat_routes.stop_chat(ChatStopRequest(run_id="run-b"), _request()) == {"ok": False}

    asyncio.run(scenario())


def test_only_the_session_that_started_a_turn_can_stop_it(turn):
    async def scenario():
        response = await chat_routes.chat(ChatRequest(message="hi", conversation_id="chat-c", run_id="run-c"), _request())
        assert await chat_routes.stop_chat(ChatStopRequest(run_id="run-c"), _request("other-session")) == {"ok": False}
        assert conversation_is_running("chat-c")
        turn.release.set()
        async for _chunk in response.body_iterator:
            pass
        assert turn.finished

    asyncio.run(scenario())


def test_a_reopened_chat_replays_the_running_turn_and_follows_it(turn):
    from fastapi import HTTPException

    async def scenario():
        response = await chat_routes.chat(ChatRequest(message="Summarize", conversation_id="chat-f", run_id="run-f"), _request())
        body = response.body_iterator
        await body.__anext__()
        await body.__anext__()
        await body.aclose()  # switched to another chat

        runs = await chat_routes.list_chat_runs(_request())
        assert [(run["run_id"], run["conversation_id"], run["message"]) for run in runs] == [("run-f", "chat-f", "Summarize")]
        assert await chat_routes.list_chat_runs(_request("other-session")) == []
        with pytest.raises(HTTPException) as caught:
            await chat_routes.follow_chat_run("run-f", _request("other-session"))
        assert caught.value.status_code == 404

        # Back in the chat: everything so far, then the rest live.
        followed = await chat_routes.follow_chat_run("run-f", _request())
        chunks = followed.body_iterator
        assert b"turn_started" in await chunks.__anext__()
        assert b"working" in await chunks.__anext__()
        turn.release.set()
        rest = [chunk async for chunk in chunks]
        assert any(b"event: done" in chunk for chunk in rest)
        assert await chat_routes.list_chat_runs(_request()) == []
        with pytest.raises(HTTPException):
            await chat_routes.follow_chat_run("run-f", _request())

    asyncio.run(scenario())


def _drain(subscription) -> list[bytes]:
    async def collect():
        chunks = []
        while not subscription._queue.empty():
            chunks.append(await subscription.get())
        return chunks

    return asyncio.run(collect())


def test_the_replay_merges_text_deltas_into_one_event():
    stream = RunStream()
    stream.publish("turn_started", {"run_id": "r"})
    for piece in ("Hel", "lo", " there"):
        stream.publish("token", {"content": piece})
    stream.publish("tool_start", {"tool": "read_file", "call_id": "c1"})
    stream.publish("token", {"content": "Done"})
    stream.close()

    chunks = _drain(stream.subscribe())
    assert chunks == [
        sse_chunk("turn_started", {"run_id": "r"}),
        sse_chunk("token", {"content": "Hello there"}),
        sse_chunk("tool_start", {"tool": "read_file", "call_id": "c1"}),
        sse_chunk("token", {"content": "Done"}),
        None,
    ]


def test_the_replay_keeps_the_start_and_the_newest_events_within_its_limit():
    stream = RunStream(max_replay_bytes=600)
    stream.publish("turn_started", {"run_id": "r"})
    for index in range(40):
        stream.publish("tool_end", {"call_id": f"c{index}", "output": "x" * 50})

    assert stream.replay_bytes <= 600
    assert stream.dropped_events > 0
    chunks = _drain(stream.subscribe())
    assert chunks[0] == sse_chunk("turn_started", {"run_id": "r"})
    assert b"Earlier steps of this task are not shown" in chunks[1]
    assert b'"c39"' in chunks[-1]
    assert not any(b'"c0"' in chunk for chunk in chunks)


def test_a_viewer_that_falls_behind_is_disconnected_with_an_error():
    stream = RunStream(max_replay_bytes=10_000, max_backlog_bytes=500)
    stream.publish("turn_started", {"run_id": "r"})
    slow = stream.subscribe()
    for index in range(20):
        stream.publish("tool_end", {"call_id": f"c{index}", "output": "x" * 50})

    chunks = _drain(slow)
    assert chunks[-1] is None
    assert b"stream_lagging" in chunks[-2]
    assert len(chunks) == 2
    # The turn keeps going and a new viewer gets the replay.
    fresh = stream.subscribe()
    assert fresh.pending_bytes > 500


def test_a_run_id_already_in_use_is_refused(turn):
    from fastapi import HTTPException

    async def scenario():
        first = await chat_routes.chat(ChatRequest(message="hi", conversation_id="chat-d", run_id="run-d"), _request())
        with pytest.raises(HTTPException) as caught:
            await chat_routes.chat(ChatRequest(message="again", conversation_id="chat-e", run_id="run-d"), _request())
        assert caught.value.status_code == 409
        turn.release.set()
        async for _chunk in first.body_iterator:
            pass

    asyncio.run(scenario())


def test_runtime_creates_default_conversation_before_durable_run(tmp_path, monkeypatch):
    from app.agent import runtime
    from app.agent.database import Database
    from app.agent.durable import turn_run

    database = Database(tmp_path / "runtime.sqlite")
    database.init_db()

    class FakeRuntime:
        async def run(
            self, message, conversation_id, *, attachments=None, steering=None,
            durable_run=None, durable_run_factory=None,
        ):  # noqa: ARG002
            if durable_run is None and callable(durable_run_factory):
                durable_run = durable_run_factory()
            assert durable_run is not None
            yield {"event": "done", "data": {"conversation_id": conversation_id, "summary": message}}

    monkeypatch.setattr(runtime, "get_db", lambda: database)
    monkeypatch.setattr(runtime, "get_runtime", lambda _settings: FakeRuntime())
    monkeypatch.setattr(turn_run, "get_db", lambda: database)
    monkeypatch.setattr(turn_run, "_owner_instance_id", lambda: "owner-test")

    async def collect():
        return [event async for event in runtime.run_agent_stream(
            message="Summarize this task",
            conversation_id="new-default-chat",
            settings=object(),
            control_session_id="session-1",
            principal_id="session-1",
            execution_source="desktop",
            durable_run_id="new-default-run",
        )]

    events = asyncio.run(collect())
    conversation = database.get_conversation("new-default-chat")
    run = database.fetchone("SELECT * FROM agent_runs WHERE run_id = ?", ("new-default-run",))

    assert events[-1]["event"] == "done"
    assert conversation is not None
    assert conversation["workspace_dir"] == ""
    assert conversation["title"] == "Summarize this task"
    assert run is not None
    assert run["conversation_id"] == "new-default-chat"
    assert run["owner_principal_id"] == "session-1"
    assert run["control_session_id"] == "session-1"


def test_resume_claim_keeps_original_input_across_empty_synthetic_checkpoint(tmp_path, monkeypatch):
    from app.agent.database import Database
    from app.agent.durable import turn_run
    from types import SimpleNamespace
    from app.agent.memory_manager import MemoryManager

    database = Database(tmp_path / "resume.sqlite")
    database.init_db()
    database.create_conversation("resume-chat", "Original task", "")
    monkeypatch.setattr(turn_run, "_owner_instance_id", lambda: "owner-test")

    scope = {
        "conversation_id": "resume-chat",
        "execution_source": "desktop",
        "principal_id": "session-1",
        "control_session_id": "session-1",
        "workspace_dir": "",
    }
    resume_scope = {key: value for key, value in scope.items() if key != "conversation_id"}
    first = turn_run.create_durable_run(**scope, run_id="resume-run", db=database)
    original_ids, _ = database.persist_turn_messages(first.turn_id, "resume-chat", [
        {"role": "user", "content": "Original instruction", "attachments": []},
        {"role": "assistant", "content": "Stopped before finishing", "status": "paused", "attachments": []},
    ])
    assert first.finalize("paused", "iteration_budget_exhausted")
    statuses = [event["payload"].get("status") for event in first.journal.read_events("resume-run")["events"]]
    assert statuses[-2:] == ["pausing", "paused"]

    second, original_checkpoint = turn_run.resume_run("resume-run", **resume_scope, db=database)
    assert second.attempt == 2
    assert second.turn_id != first.turn_id
    assert original_checkpoint["user_message_ids"] == original_ids

    # Continuation text goes to the transcript only. Its checkpoint can have an
    # empty user-ID list while the recovery reference still resolves the
    # original saved instruction.
    memory = MemoryManager.__new__(MemoryManager)
    memory._db = database
    memory.memory_store = None
    state = SimpleNamespace(recent_messages=[], all_messages=[], updated_at=None)
    memory.get_or_create = lambda _conversation_id: state
    memory.estimate_context_tokens = lambda _conversation_id: 10
    continuation_ids = asyncio.run(memory.persist_turn(
        "resume-chat",
        "synthetic continuation prompt",
        "Continued safely",
        tool_calls=[],
        status="paused",
        turn_id=second.turn_id,
        persist_user_message=False,
    ))
    assert continuation_ids == []
    second.begin_pause()
    assert second.finalize("paused", "user_pause")
    third, checkpoint = turn_run.resume_run("resume-run", **resume_scope, db=database)

    user_rows = [row for row in database.get_messages("resume-chat", limit=20) if row["role"] == "user"]
    assert third.attempt == 3
    assert checkpoint["user_message_ids"] == original_ids
    assert len(user_rows) == 1
    assert user_rows[0]["content"] == "Original instruction"


def test_opaque_returned_effect_does_not_advertise_resume(tmp_path, monkeypatch):
    from app.agent.database import Database
    from app.agent.durable import turn_run
    from app.agent.durable.models import InvalidTransitionError

    database = Database(tmp_path / "opaque.sqlite")
    database.init_db()
    database.create_conversation("opaque-chat", "Opaque task", "")
    monkeypatch.setattr(turn_run, "_owner_instance_id", lambda: "owner-test")
    handle = turn_run.create_durable_run(
        conversation_id="opaque-chat",
        execution_source="desktop",
        principal_id="session-1",
        control_session_id="session-1",
        workspace_dir="",
        run_id="opaque-run",
        db=database,
    )
    database.persist_turn_messages(handle.turn_id, "opaque-chat", [
        {"role": "user", "content": "Run a command", "attachments": []},
        {"role": "assistant", "content": "Command returned", "status": "paused", "attachments": []},
    ])
    handle.journal.prepare_operation(
        handle.run_id,
        operation_id="opaque-op",
        owner_instance_id=handle.owner_instance_id,
        attempt=handle.attempt,
        operation_kind="shell_command",
        effect_class="opaque_effect",
        tool_name="shell_command",
    )
    for status in ("authorized", "executing", "returned"):
        handle.journal.transition_operation(
            handle.run_id,
            "opaque-op",
            to_status=status,
            owner_instance_id=handle.owner_instance_id,
            attempt=handle.attempt,
            reason_code="unknown" if status == "returned" else None,
        )
    handle.begin_pause()
    assert handle.finalize("paused", "user_pause")

    snapshot = handle.journal.snapshot(handle.run_id)
    assert snapshot is not None
    assert "resume" not in snapshot["available_actions"]
    with pytest.raises(InvalidTransitionError):
        turn_run.resume_run(
            handle.run_id,
            execution_source="desktop",
            principal_id="session-1",
            control_session_id="session-1",
            workspace_dir="",
            db=database,
        )


def test_resume_rejects_workspace_change_and_newer_conversation_turn(tmp_path, monkeypatch):
    from app.agent.database import Database
    from app.agent.durable import turn_run
    from app.agent.durable.models import InvalidTransitionError

    database = Database(tmp_path / "stale-resume.sqlite")
    database.init_db()
    database.create_conversation("stale-chat", "Old task", "C:/workspace")
    monkeypatch.setattr(turn_run, "_owner_instance_id", lambda: "owner-test")
    handle = turn_run.create_durable_run(
        conversation_id="stale-chat",
        execution_source="desktop",
        principal_id="session-1",
        control_session_id="session-1",
        workspace_dir="C:/workspace",
        run_id="stale-run",
        db=database,
    )
    database.persist_turn_messages(handle.turn_id, "stale-chat", [
        {"role": "user", "content": "Old instruction", "attachments": []},
        {"role": "assistant", "content": "Stopped", "status": "paused", "attachments": []},
    ])
    handle.begin_pause()
    assert handle.finalize("paused", "user_pause")

    database.update_conversation("stale-chat", workspace_dir="C:/new-workspace")
    stale_snapshot = handle.journal.snapshot("stale-run")
    assert stale_snapshot is not None
    assert "resume" not in stale_snapshot["available_actions"]
    with pytest.raises(InvalidTransitionError):
        turn_run.resume_run(
            "stale-run",
            execution_source="desktop",
            principal_id="session-1",
            control_session_id="session-1",
            workspace_dir="C:/new-workspace",
            db=database,
        )

    # A workspace restored to its original value is still stale once a newer
    # user turn has been added to the conversation.
    database.update_conversation("stale-chat", workspace_dir="C:/workspace")
    database.add_message("stale-chat", "user", "A newer unrelated request")
    newer_snapshot = handle.journal.snapshot("stale-run")
    assert newer_snapshot is not None
    assert "resume" not in newer_snapshot["available_actions"]
    with pytest.raises(InvalidTransitionError):
        turn_run.resume_run(
            "stale-run",
            execution_source="desktop",
            principal_id="session-1",
            control_session_id="session-1",
            workspace_dir="C:/workspace",
            db=database,
        )
    database.close()


def test_active_scope_conflict_is_rejected_before_a_second_run_row(tmp_path, monkeypatch):
    from app.agent.database import Database
    from app.agent.durable import turn_run
    from app.agent.durable.models import LeaseConflictError

    database = Database(tmp_path / "active-conflict.sqlite")
    database.init_db()
    database.create_conversation("active-chat", "Active", "C:/shared")
    database.create_conversation("other-chat", "Other", "C:/shared")
    owner = ["owner-one"]
    monkeypatch.setattr(turn_run, "_owner_instance_id", lambda: owner[0])
    first = turn_run.create_durable_run(
        conversation_id="active-chat",
        execution_source="desktop",
        principal_id="session-1",
        control_session_id="session-1",
        workspace_dir="C:/shared",
        run_id="active-first",
        db=database,
    )

    owner[0] = "owner-two"
    with pytest.raises(LeaseConflictError):
        turn_run.create_durable_run(
            conversation_id="active-chat",
            execution_source="desktop",
            principal_id="session-2",
            control_session_id="session-2",
            workspace_dir="C:/shared",
            run_id="active-same-conversation",
            db=database,
        )
    with pytest.raises(LeaseConflictError):
        turn_run.create_durable_run(
            conversation_id="other-chat",
            execution_source="desktop",
            principal_id="session-2",
            control_session_id="session-2",
            workspace_dir="C:/shared",
            run_id="active-same-workspace",
            db=database,
        )

    assert first.journal.snapshot("active-first")["status"] == "running"
    assert database.fetchone("SELECT COUNT(*) FROM agent_runs")[0] == 1
    database.close()
