"""A desktop turn outlives its HTTP stream; only an explicit Stop ends it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agent.steering import _inboxes, conversation_is_running
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
