"""Give Chrome's native consent time to complete before browser-use starts.

browser-use's CDP handshake expires after ten seconds. Establish the approved
connection first, then relay it to that library over a private loopback socket.
Chrome's consent stays intact; disconnecting closes sockets, never Chrome.
"""
from __future__ import annotations

import asyncio
import secrets
from http import HTTPStatus

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

_MAX_FRAME_SIZE = 200 * 1024 * 1024


class LocalChromeConnection:
    def __init__(self) -> None:
        self.endpoint = ""
        self._path = f"/devtools/browser/{secrets.token_urlsafe(32)}"
        self._upstream = None
        self._server = None
        self._client = None

    async def start(self, chrome_endpoint: str) -> str:
        try:
            # Chrome holds the handshake until the user chooses Allow/Cancel.
            self._upstream = await connect(
                chrome_endpoint, open_timeout=60, close_timeout=2,
                max_size=_MAX_FRAME_SIZE, proxy=None,
            )
            self._server = await serve(
                self._relay, "127.0.0.1", 0, process_request=self._authorize,
                origins=[None], max_size=_MAX_FRAME_SIZE, close_timeout=2,
            )
            port = self._server.sockets[0].getsockname()[1]
            self.endpoint = f"ws://127.0.0.1:{port}{self._path}"
            return self.endpoint
        except BaseException:
            await self.close()
            raise

    def _authorize(self, connection, request):
        if request.path != self._path:
            return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")
        if self._client is not None:
            return connection.respond(HTTPStatus.CONFLICT, "Already connected\n")
        return None

    async def _relay(self, client) -> None:
        if self._client is not None:
            await client.close(code=1008, reason="Already connected")
            return
        self._client = client

        async def forward(source, destination):
            async for message in source:
                await destination.send(message)

        tasks = [asyncio.create_task(forward(client, self._upstream)),
                 asyncio.create_task(forward(self._upstream, client))]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except ConnectionClosed:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.close()
            await self._upstream.close()
            self._client = None

    async def close(self) -> None:
        if self._upstream is not None:
            await self._upstream.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self._upstream = None
        self._server = None
        self.endpoint = ""
