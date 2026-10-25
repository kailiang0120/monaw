"""The local WebSocket the Monaw extension connects to.

Only the Monaw extension may connect: the listener is on loopback, the
``Origin`` header must be the extension's fixed origin (browsers always send
the real one, so web pages cannot pass), and the first message must carry the
pairing token. One extension connection is served at a time.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from .common import EXTENSION_ORIGIN, PROTOCOL_VERSION

logger = logging.getLogger("monaw.browser_extension")

CLOSE_UNAUTHORIZED = 4401
CLOSE_BUSY = 4409
HELLO_TIMEOUT_S = 10.0
DEFAULT_REQUEST_TIMEOUT_S = 30.0
# Screenshots of long pages arrive as one message.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

EventListener = Callable[[dict[str, Any]], None]


class ExtensionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExtensionBridge:
    def __init__(
        self,
        port: int,
        *,
        token_provider: Callable[[], str | None],
        expected_version_provider: Callable[[], str] = lambda: "",
        on_status: Callable[[dict[str, Any]], None] = lambda status: None,
        host: str = "127.0.0.1",
    ) -> None:
        self.port = port
        self.host = host
        self._token_provider = token_provider
        self._expected_version_provider = expected_version_provider
        self._on_status = on_status
        self._server: Server | None = None
        self._connection: ServerConnection | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._listeners: list[EventListener] = []
        self._connected = asyncio.Event()
        self.extension_version = ""
        self.user_agent = ""
        self.last_error = ""

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        try:
            self._server = await serve(
                self._handle_connection,
                self.host,
                self.port,
                process_request=self._process_request,
                max_size=MAX_MESSAGE_BYTES,
            )
        except OSError as exc:
            self.last_error = f"Port {self.port} is already in use; change it in Monaw settings."
            self._publish()
            raise ExtensionError("extension_port_unavailable", self.last_error) from exc
        self._publish()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def listening(self) -> bool:
        return self._server is not None

    @property
    def connected(self) -> bool:
        return self._connection is not None

    async def wait_connected(self, timeout: float) -> bool:
        if self.connected:
            return True
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return self.connected

    def add_listener(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    # -- requests ------------------------------------------------------------

    async def request(self, method: str, params: dict[str, Any] | None = None,
                      *, timeout: float = DEFAULT_REQUEST_TIMEOUT_S) -> Any:
        connection = self._connection
        if connection is None:
            raise ExtensionError("extension_not_connected", "The Monaw Chrome extension is not connected.")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await connection.send(json.dumps(
                {"type": "request", "id": request_id, "method": method, "params": params or {}}
            ))
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise ExtensionError("extension_timeout", f"Chrome did not answer {method} in time.") from exc
        except ConnectionClosed as exc:
            raise ExtensionError("extension_disconnected", "The Monaw Chrome extension disconnected.") from exc
        finally:
            self._pending.pop(request_id, None)

    # -- connection handling -------------------------------------------------

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        if request.path == "/health":
            # Lets the extension check the port quietly before opening a socket.
            return connection.respond(HTTPStatus.OK, "ok\n")
        if request.path != "/extension":
            return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")
        if request.headers.get("Origin") != EXTENSION_ORIGIN:
            return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden\n")
        return None

    async def _handle_connection(self, connection: ServerConnection) -> None:
        if self._connection is not None:
            await connection.close(CLOSE_BUSY, "Another extension is already connected")
            return
        hello = await self._read_hello(connection)
        if hello is None:
            return
        version = str(hello.get("version") or "")
        expected = self._expected_version_provider()
        reload = bool(expected and version and expected != version)
        await connection.send(json.dumps({"type": "welcome", "protocol": PROTOCOL_VERSION, "reload": reload}))
        if reload:
            # The extension reloads itself to pick up the updated files and
            # connects again with the new version.
            await connection.wait_closed()
            return

        self._connection = connection
        self.extension_version = version
        self.user_agent = str(hello.get("userAgent") or "")
        self.last_error = ""
        self._connected.set()
        self._publish()
        try:
            async for raw in connection:
                self._handle_message(connection, raw)
        except ConnectionClosed:
            pass
        finally:
            self._connection = None
            self._connected.clear()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ExtensionError(
                        "extension_disconnected", "The Monaw Chrome extension disconnected."
                    ))
            self._emit({"type": "event", "event": "disconnected"})
            self._publish()

    async def _read_hello(self, connection: ServerConnection) -> dict[str, Any] | None:
        try:
            raw = await asyncio.wait_for(connection.recv(), HELLO_TIMEOUT_S)
            hello = json.loads(raw)
        except (asyncio.TimeoutError, ConnectionClosed, ValueError, TypeError):
            await connection.close(CLOSE_UNAUTHORIZED, "Expected hello")
            return None
        token = self._token_provider() or ""
        offered = hello.get("token") if isinstance(hello, dict) else None
        if (
            not isinstance(hello, dict)
            or hello.get("type") != "hello"
            or hello.get("protocol") != PROTOCOL_VERSION
            or not token
            or not isinstance(offered, str)
            or not hmac.compare_digest(offered.encode(), token.encode())
        ):
            self.last_error = "The extension's pairing did not match. Run Set up extension again."
            self._publish()
            await connection.close(CLOSE_UNAUTHORIZED, "Pairing rejected")
            return None
        return hello

    def _handle_message(self, connection: ServerConnection, raw: Any) -> None:
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "response":
            future = self._pending.get(message.get("id"))
            if future is None or future.done():
                return
            error = message.get("error")
            if error:
                text = str(error.get("message") if isinstance(error, dict) else error)
                future.set_exception(ExtensionError("chrome_error", text))
            else:
                future.set_result(message.get("result"))
        elif kind == "event":
            self._emit(message)
        elif kind == "ping":
            asyncio.ensure_future(self._send_quietly(connection, {"type": "pong"}))

    async def _send_quietly(self, connection: ServerConnection, payload: dict[str, Any]) -> None:
        try:
            await connection.send(json.dumps(payload))
        except ConnectionClosed:
            pass

    def _emit(self, event: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # A broken listener must not drop the socket.
                logger.exception("Browser extension event listener failed")

    def _publish(self) -> None:
        self._on_status({
            "connected": self.connected,
            "version": self.extension_version if self.connected else "",
            "user_agent": self.user_agent if self.connected else "",
            "port": self.port,
            "error": self.last_error,
        })
