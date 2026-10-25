import asyncio
import json
import socket

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.skills.browser_use.extension.bridge import CLOSE_BUSY, CLOSE_UNAUTHORIZED, ExtensionBridge, ExtensionError
from app.skills.browser_use.extension.common import EXTENSION_ORIGIN, PROTOCOL_VERSION

TOKEN = "t" * 43


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _hello(**overrides):
    return json.dumps({"type": "hello", "protocol": PROTOCOL_VERSION, "token": TOKEN, "version": "1.0.0",
                       "userAgent": "Chrome/140", **overrides})


async def _with_bridge(scenario, *, expected_version: str = "1.0.0"):
    port = _free_port()
    statuses: list[dict] = []
    bridge = ExtensionBridge(port, token_provider=lambda: TOKEN,
                             expected_version_provider=lambda: expected_version, on_status=statuses.append)
    await bridge.start()
    try:
        return await scenario(bridge, f"ws://127.0.0.1:{port}/extension", statuses)
    finally:
        await bridge.close()


def test_rejects_connections_that_are_not_from_the_extension():
    async def scenario(bridge, url, _statuses):
        with pytest.raises(InvalidStatus) as excinfo:
            async with connect(url, origin="https://evil.example"):
                pass
        assert excinfo.value.response.status_code == 403
        with pytest.raises(InvalidStatus):
            async with connect(url):
                pass
        assert not bridge.connected

    asyncio.run(_with_bridge(scenario))


@pytest.mark.parametrize("hello", [
    _hello(token="wrong" * 10),
    _hello(protocol=99),
    json.dumps({"type": "request", "id": 1}),
    "not json",
])
def test_rejects_a_bad_hello(hello):
    async def scenario(bridge, url, statuses):
        async with connect(url, origin=EXTENSION_ORIGIN) as ws:
            await ws.send(hello)
            with pytest.raises(ConnectionClosed) as excinfo:
                await ws.recv()
        assert excinfo.value.rcvd.code == CLOSE_UNAUTHORIZED
        assert not bridge.connected

    asyncio.run(_with_bridge(scenario))


def test_health_endpoint_answers_plain_http():
    async def scenario(bridge, url, _statuses):
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
        writer.write(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        line = await reader.readline()
        writer.close()
        return line

    assert b"200" in asyncio.run(_with_bridge(scenario))


def test_round_trips_requests_and_events_and_reports_status():
    async def scenario(bridge, url, statuses):
        events: list[dict] = []
        bridge.add_listener(events.append)
        async with connect(url, origin=EXTENSION_ORIGIN) as ws:
            await ws.send(_hello())
            welcome = json.loads(await ws.recv())
            assert welcome == {"type": "welcome", "protocol": PROTOCOL_VERSION, "reload": False}
            assert await bridge.wait_connected(1)

            async def extension():
                request = json.loads(await ws.recv())
                assert request["method"] == "tabs.list"
                await ws.send(json.dumps({"type": "event", "event": "tab.removed", "tabId": 4}))
                await ws.send(json.dumps({"type": "response", "id": request["id"], "result": [{"id": 1}]}))
                failing = json.loads(await ws.recv())
                await ws.send(json.dumps({"type": "response", "id": failing["id"], "error": {"message": "No tab with id: 9"}}))

            task = asyncio.create_task(extension())
            assert await bridge.request("tabs.list") == [{"id": 1}]
            with pytest.raises(ExtensionError) as excinfo:
                await bridge.request("tabs.close", {"tabId": 9})
            assert excinfo.value.code == "chrome_error"
            await task
            assert {"type": "event", "event": "tab.removed", "tabId": 4} in events
            assert statuses[-1]["connected"] is True and statuses[-1]["version"] == "1.0.0"
        for _ in range(50):
            if not bridge.connected:
                break
            await asyncio.sleep(0.02)
        assert not bridge.connected
        assert statuses[-1]["connected"] is False
        assert {"type": "event", "event": "disconnected"} in events

    asyncio.run(_with_bridge(scenario))


def test_second_extension_is_refused_while_one_is_connected():
    async def scenario(bridge, url, _statuses):
        async with connect(url, origin=EXTENSION_ORIGIN) as first:
            await first.send(_hello())
            await first.recv()
            async with connect(url, origin=EXTENSION_ORIGIN) as second:
                with pytest.raises(ConnectionClosed) as excinfo:
                    await second.recv()
            assert excinfo.value.rcvd.code == CLOSE_BUSY
            assert bridge.connected

    asyncio.run(_with_bridge(scenario))


def test_outdated_extension_is_asked_to_reload():
    async def scenario(bridge, url, _statuses):
        async with connect(url, origin=EXTENSION_ORIGIN) as ws:
            await ws.send(_hello(version="0.9.0"))
            welcome = json.loads(await ws.recv())
        assert welcome["reload"] is True
        assert not bridge.connected

    asyncio.run(_with_bridge(scenario, expected_version="1.0.0"))


def test_requests_fail_cleanly_without_an_extension():
    async def scenario(bridge, _url, _statuses):
        with pytest.raises(ExtensionError) as excinfo:
            await bridge.request("tabs.list")
        assert excinfo.value.code == "extension_not_connected"
        assert await bridge.wait_connected(0.05) is False

    asyncio.run(_with_bridge(scenario))


def test_reports_a_busy_port():
    async def scenario():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            bridge = ExtensionBridge(sock.getsockname()[1], token_provider=lambda: TOKEN)
            with pytest.raises(ExtensionError) as excinfo:
                await bridge.start()
        assert excinfo.value.code == "extension_port_unavailable"

    asyncio.run(scenario())
