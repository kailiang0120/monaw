from __future__ import annotations

import json
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _limit_for_path(path: str) -> int:
    if path == "/api/uploads":
        return 70 * 1024 * 1024
    if path.startswith("/api/chat"):
        return 4 * 1024 * 1024
    if path.startswith("/api/settings"):
        return 2 * 1024 * 1024
    return 10 * 1024 * 1024


class RequestSizeLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path") or "").startswith("/api"):
            await self.app(scope, receive, send)
            return

        limit = _limit_for_path(str(scope["path"]))
        request_id = uuid.uuid4().hex
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_content_length = headers.get(b"content-length")
        if raw_content_length:
            try:
                if int(raw_content_length) > limit:
                    await self._reject(send, request_id)
                    return
            except ValueError:
                await self._respond(
                    send,
                    status=400,
                    detail="Invalid Content-Length header",
                    request_id=request_id,
                )
                return

        consumed = 0
        buffered: list[Message] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    await self._reject(send, request_id)
                    return
                buffered.append(message)
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                buffered.append(message)
                break

        async def replay_receive() -> Message:
            if buffered:
                return buffered.pop(0)
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)

    @classmethod
    async def _reject(cls, send: Send, request_id: str) -> None:
        await cls._respond(
            send,
            status=413,
            detail="Request body is too large",
            request_id=request_id,
        )

    @staticmethod
    async def _respond(
        send: Send,
        *,
        status: int,
        detail: str,
        request_id: str,
    ) -> None:
        body = json.dumps({"detail": detail, "request_id": request_id}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"x-request-id", request_id.encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
