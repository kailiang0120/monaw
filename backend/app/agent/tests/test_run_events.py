import asyncio

from app.agent.run_events import RunEvent, RunEventPublisher, event_dict


def test_event_dict_preserves_event_name_and_normalizes_data():
    assert event_dict(RunEvent("token", {"content": "hi"})) == {
        "event": "token",
        "data": {"content": "hi"},
    }
    assert event_dict({"event": "done", "data": None}) == {"event": "done", "data": {}}


def test_publish_nowait_puts_event_and_updates_flush_time():
    async def run():
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        last_flush = [loop.time() - 100]
        publisher = RunEventPublisher(queue, last_flush, loop=loop)

        publisher.emit("token", {"content": "hello"})

        event = await queue.get()
        assert event == {"event": "token", "data": {"content": "hello"}}
        assert last_flush[0] > loop.time() - 1

    asyncio.run(run())


def test_heartbeat_emits_timestamped_idle_event():
    async def run():
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        last_flush = [loop.time() - 100]
        publisher = RunEventPublisher(queue, last_flush, loop=loop)

        await publisher.heartbeat()

        event = await queue.get()
        assert event["event"] == "heartbeat"
        assert event["data"]["phase"] == "idle"
        assert isinstance(event["data"]["t"], int)

    asyncio.run(run())
