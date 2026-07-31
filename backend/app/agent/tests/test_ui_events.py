import asyncio

from app.agent.ui_events import publish_ui_event, reset_ui_events_for_tests, subscribe_ui_events


def test_ui_events_replay_and_live_delivery():
    async def collect():
        reset_ui_events_for_tests()
        first = publish_ui_event("conversation.changed", {"conversation_id": "conv-1"})
        replay = subscribe_ui_events(last_event_id=0)
        ready = await anext(replay)
        replayed = await anext(replay)
        live_task = asyncio.create_task(anext(replay))
        second = publish_ui_event("usage.changed", {"conversation_id": "conv-1"})
        live = await asyncio.wait_for(live_task, timeout=1)
        await replay.aclose()
        return first, second, ready, replayed, live

    first, second, ready, replayed, live = asyncio.run(collect())

    assert first.seq == 1
    assert second.seq == 2
    assert ready.event == "backend.ready"
    assert replayed.event == "conversation.changed"
    assert replayed.data == {"conversation_id": "conv-1"}
    assert live.event == "usage.changed"
    assert live.data == {"conversation_id": "conv-1"}
