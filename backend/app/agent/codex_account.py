"""ChatGPT account sign-in and model calls through the bundled Codex app-server.

Codex is used only as the connection to the model. Each Monaw run keeps one
Codex turn open while tools execute: when the model calls a tool, Codex sends
an `item/tool/call` request, Monaw returns it to the turn loop like any other
provider tool call, and the result is sent back on the next model call.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.codex_app_server import CodexAppServer, CodexProcessExited, default_server_response
from app.agent.runtime_paths import MONAW_HOME_DIR, WORKSPACE_DIR


logger = logging.getLogger(__name__)
_CODEX_HOME = MONAW_HOME_DIR / "codex-account"
_DEFAULT_INSTRUCTIONS = "You are Monaw, a helpful assistant."
_CONTINUE_TEXT = "Continue from the tool results above."
_SKIPPED_TOOL_TEXT = "This tool call was not run."
_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
# Further tool calls from one model response arrive back to back.
_TOOL_BATCH_WINDOW_SECONDS = 0.2
_IDLE_SESSION_SECONDS = 15 * 60
_MAX_OPEN_SESSIONS = 16


def _rate_limit_windows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    buckets = payload.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = payload.get("rateLimits")
        buckets = {str(legacy.get("limitId") or "codex"): legacy} if isinstance(legacy, dict) else {}

    windows: list[dict[str, Any]] = []
    for bucket_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        limit_id = str(bucket.get("limitId") or bucket_id)
        name = str(bucket.get("limitName") or limit_id).replace("_", " ").strip()
        for kind in ("primary", "secondary"):
            window = bucket.get(kind)
            if not isinstance(window, dict):
                continue
            raw_used = window.get("usedPercent")
            raw_duration = window.get("windowDurationMins")
            raw_reset = window.get("resetsAt")
            try:
                used = max(0.0, min(100.0, float(raw_used))) if raw_used is not None else None
                duration = int(raw_duration) if raw_duration is not None else None
                resets_at = int(raw_reset) if raw_reset is not None else None
            except (TypeError, ValueError, OverflowError):
                continue
            if duration is None and used is None and resets_at is None:
                continue
            windows.append({
                "limit_id": limit_id,
                "limit_name": name,
                "window": kind,
                "used_percent": used,
                "window_duration_mins": duration,
                "resets_at": resets_at,
            })
    return windows


@dataclass
class CodexToolCall:
    call_id: str
    name: str
    arguments: dict


@dataclass
class CodexModelResult:
    content: str
    tool_calls: list[CodexToolCall] = field(default_factory=list)
    reasoning: str = ""
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class _TurnSession:
    thread_id: str
    queue: asyncio.Queue
    signature: str
    turn_id: str = ""
    pending: dict[str, Any] = field(default_factory=dict)
    usage_total: dict[str, int] = field(default_factory=dict)
    usage_mark: dict[str, int] = field(default_factory=dict)
    last_active: float = field(default_factory=time.monotonic)


def _tool_specs(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "name": str(tool.get("name") or ""),
            "description": str(tool.get("description") or ""),
            "inputSchema": tool.get("parameters") or {"type": "object", "properties": {}},
        }
        for tool in tools
    ]


def _signature(model: str, effort: str, system_prompt: str, specs: list[dict]) -> str:
    raw = json.dumps([model, effort, system_prompt, specs], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _image_data_urls(message: dict) -> list[str]:
    urls: list[str] = []
    for image in message.get("images") or []:
        path = str(image.get("path") or "") if isinstance(image, dict) else ""
        if not path:
            continue
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue
        mime = str(image.get("mime_type") or "") or mimetypes.guess_type(path)[0] or "image/png"
        urls.append(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}")
    return urls


def _is_plain_user_message(message: dict) -> bool:
    return not message.get("type") and message.get("role", "user") in {"user", "human"}


def _history_item(message: dict) -> dict | None:
    """Convert a Monaw message to a Responses item for `thread/inject_items`."""
    item_type = message.get("type")
    if item_type == "function_call":
        arguments = message.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False)
        return {
            "type": "function_call",
            "call_id": str(message.get("call_id") or ""),
            "name": str(message.get("name") or ""),
            "arguments": arguments,
        }
    if item_type == "function_call_output":
        output = str(message.get("output") or "")
        images = _image_data_urls(message)
        if images:
            return {
                "type": "function_call_output",
                "call_id": str(message.get("call_id") or ""),
                "output": [{"type": "input_text", "text": output}]
                + [{"type": "input_image", "image_url": url} for url in images],
            }
        return {"type": "function_call_output", "call_id": str(message.get("call_id") or ""), "output": output}
    if item_type:
        # Encrypted reasoning from another provider cannot be replayed here.
        return None
    role = message.get("role", "user")
    text = str(message.get("content") or "")
    if role in {"assistant", "model"}:
        if not text:
            return None
        return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    if role not in {"user", "human"}:
        return None
    content = [{"type": "input_text", "text": text}]
    content += [{"type": "input_image", "image_url": url} for url in _image_data_urls(message)]
    return {"type": "message", "role": "user", "content": content}


def _split_history(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (history items to inject, input for the new turn)."""
    if messages and _is_plain_user_message(messages[-1]):
        history, last = messages[:-1], messages[-1]
        turn_input: list[dict] = [{"type": "text", "text": str(last.get("content") or "")}]
        for image in last.get("images") or []:
            path = str(image.get("path") or "") if isinstance(image, dict) else ""
            if path and Path(path).is_file():
                turn_input.append({"type": "localImage", "path": path})
    else:
        history, turn_input = messages, [{"type": "text", "text": _CONTINUE_TEXT}]
    items = [item for item in (_history_item(message) for message in history) if item is not None]
    return items, turn_input


def _trailing_tool_exchange(messages: list[dict]) -> tuple[list[str], dict[str, dict], list[str]]:
    """Find the latest function calls plus the results and notes after them."""
    outputs: dict[str, dict] = {}
    notes: list[str] = []
    index = len(messages) - 1
    while index >= 0:
        message = messages[index]
        if message.get("type") == "function_call_output":
            outputs[str(message.get("call_id") or "")] = message
        elif _is_plain_user_message(message):
            notes.insert(0, str(message.get("content") or ""))
        else:
            break
        index -= 1
    call_ids: list[str] = []
    while index >= 0 and messages[index].get("type") == "function_call":
        call_ids.append(str(messages[index].get("call_id") or ""))
        index -= 1
    return call_ids, outputs, notes


def _usage_numbers(raw: Any) -> dict[str, int]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "input_tokens": int(raw.get("inputTokens") or 0),
        "output_tokens": int(raw.get("outputTokens") or 0),
        "reasoning_tokens": int(raw.get("reasoningOutputTokens") or 0),
        "cached_tokens": int(raw.get("cachedInputTokens") or 0),
        "total_tokens": int(raw.get("totalTokens") or 0),
    }


class CodexAccountService:
    def __init__(self, home: Path = _CODEX_HOME) -> None:
        self.home = home
        self.server = CodexAppServer(home)
        self._sessions: dict[str, _TurnSession] = {}
        self._pending_index: dict[str, _TurnSession] = {}
        self._cleanup_tasks: set[asyncio.Task] = set()

    async def status(self, *, include_limits: bool = True) -> dict[str, Any]:
        await self.server.ensure_started()
        response = await self.server.request("account/read", {"refreshToken": False})
        account = response.get("account") if isinstance(response, dict) else None
        connected = isinstance(account, dict) and account.get("type") == "chatgpt"
        result = {
            "connected": connected,
            "plan": str(account.get("planType") or "") if connected else "",
            "limits": [],
            "usage_error": "",
        }
        if connected and include_limits:
            try:
                limits = await self.server.request("account/rateLimits/read")
                result["limits"] = _rate_limit_windows(limits if isinstance(limits, dict) else {})
            except Exception:
                logger.warning("Could not load Codex account rate limits")
                result["usage_error"] = "Usage limits are unavailable right now."
        return result

    async def start_login(self) -> dict[str, str]:
        await self.server.ensure_started()
        response = await self.server.request("account/login/start", {"type": "chatgpt"})
        return {"auth_url": str(response.get("authUrl") or ""), "login_id": str(response.get("loginId") or "")}

    async def logout(self) -> None:
        await self.server.ensure_started()
        await self.server.request("account/logout")

    async def model_call(
        self,
        *,
        model: str,
        effort: str,
        messages: list[dict],
        tools: list[dict],
        system_prompt: str,
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
        reasoning_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> CodexModelResult:
        await self.server.ensure_started(require_catalog=True)
        self._close_idle_sessions()
        effort = effort if effort in _EFFORTS else "low"
        specs = _tool_specs(tools)
        signature = _signature(model, effort, system_prompt, specs)

        session = self._resume_target(messages, signature)
        try:
            if session is not None:
                await self._send_tool_results(session, messages)
            else:
                session = await self._start_session(
                    model, effort, messages, specs, system_prompt, signature, reasoning_callback is not None,
                )
            return await self._collect(session, stream_callback, reasoning_callback)
        except BaseException:
            if session is not None:
                self._schedule_close(session)
            raise

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def _resume_target(self, messages: list[dict], signature: str) -> _TurnSession | None:
        call_ids, _outputs, _notes = _trailing_tool_exchange(messages)
        if not call_ids:
            return None
        session = self._pending_index.get(call_ids[0])
        if session is None:
            return None
        if set(session.pending) != set(call_ids) or session.signature != signature:
            # The tool set or instructions changed; replay history on a new thread.
            self._schedule_close(session)
            return None
        return session

    async def _start_session(
        self,
        model: str,
        effort: str,
        messages: list[dict],
        specs: list[dict],
        system_prompt: str,
        signature: str,
        want_summary: bool,
    ) -> _TurnSession:
        account = await self.server.request("account/read", {"refreshToken": False})
        if not (isinstance(account, dict) and isinstance(account.get("account"), dict)
                and account["account"].get("type") == "chatgpt"):
            raise ValueError("OpenAI account is not connected. Sign in under Settings > Connections.")

        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        started = await self.server.request("thread/start", {
            "model": model,
            "cwd": str(WORKSPACE_DIR),
            "ephemeral": True,
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "baseInstructions": system_prompt or _DEFAULT_INSTRUCTIONS,
            "dynamicTools": specs,
        })
        thread_id = str(started["thread"]["id"])
        session = _TurnSession(thread_id=thread_id, queue=self.server.subscribe(thread_id), signature=signature)
        self._sessions[thread_id] = session
        history, turn_input = _split_history(messages)
        if history:
            await self.server.request("thread/inject_items", {"threadId": thread_id, "items": history})
        turn_params: dict[str, Any] = {"threadId": thread_id, "input": turn_input, "effort": effort}
        if want_summary:
            turn_params["summary"] = "auto"
        turn = await self.server.request("turn/start", turn_params)
        session.turn_id = str((turn.get("turn") or {}).get("id") or "")
        return session

    async def _send_tool_results(self, session: _TurnSession, messages: list[dict]) -> None:
        _call_ids, outputs, notes = _trailing_tool_exchange(messages)
        pending = list(session.pending.items())
        session.pending.clear()
        for position, (call_id, request_id) in enumerate(pending):
            self._pending_index.pop(call_id, None)
            message = outputs.get(call_id)
            if message is None:
                items = [{"type": "inputText", "text": _SKIPPED_TOOL_TEXT}]
            else:
                items = [{"type": "inputText", "text": str(message.get("output") or "")}]
                items += [{"type": "inputImage", "imageUrl": url} for url in _image_data_urls(message)]
            if position == len(pending) - 1:
                # Notes Monaw adds after tool results travel with the last one.
                items += [{"type": "inputText", "text": note} for note in notes if note]
            await self.server.respond(request_id, {"contentItems": items, "success": message is not None})
        session.last_active = time.monotonic()

    async def _collect(
        self,
        session: _TurnSession,
        stream_callback: Callable[[str], Awaitable[None]] | None,
        reasoning_callback: Callable[[str], Awaitable[None]] | None,
    ) -> CodexModelResult:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        streamed_summaries: dict[str, str] = {}
        tool_calls: list[CodexToolCall] = []
        finished = False

        while True:
            try:
                if tool_calls:
                    message = await asyncio.wait_for(session.queue.get(), _TOOL_BATCH_WINDOW_SECONDS)
                else:
                    message = await session.queue.get()
            except asyncio.TimeoutError:
                break
            method = str(message.get("method") or "")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}

            if "id" in message:
                if method == "item/tool/call":
                    call_id = str(params.get("callId") or f"call_{uuid.uuid4().hex}")
                    arguments = params.get("arguments")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except ValueError:
                            arguments = {}
                    tool_calls.append(CodexToolCall(
                        call_id=call_id,
                        name=str(params.get("tool") or ""),
                        arguments=arguments if isinstance(arguments, dict) else {},
                    ))
                    session.pending[call_id] = message["id"]
                    self._pending_index[call_id] = session
                else:
                    result, error = default_server_response(method)
                    await self.server.respond(message["id"], result, error)
                continue

            if method == "__exited__":
                raise CodexProcessExited(str(params.get("message") or "Codex exited."))
            if method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                if delta and stream_callback is not None:
                    await stream_callback(delta)
            elif method == "item/reasoning/summaryTextDelta":
                delta = str(params.get("delta") or "")
                if delta:
                    item_id = str(params.get("itemId") or "")
                    streamed_summaries[item_id] = streamed_summaries.get(item_id, "") + delta
                    reasoning_parts.append(delta)
                    if reasoning_callback is not None:
                        await reasoning_callback(delta)
            elif method == "item/completed":
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                if item.get("type") == "agentMessage":
                    text = str(item.get("text") or "")
                    if text:
                        content_parts.append(text)
                elif item.get("type") == "reasoning" and not streamed_summaries.get(str(item.get("id") or "")):
                    summary = "\n".join(str(part) for part in item.get("summary") or [])
                    if summary:
                        reasoning_parts.append(summary)
                        if reasoning_callback is not None:
                            await reasoning_callback(summary)
            elif method == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), dict) else {}
                session.usage_total = _usage_numbers(usage.get("total"))
            elif method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                status = str(turn.get("status") or "")
                if status != "completed":
                    error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
                    raise RuntimeError(str(error.get("message") or f"Codex turn {status or 'failed'}."))
                finished = True
                session.turn_id = ""
                break

        usage = {
            key: max(0, session.usage_total.get(key, 0) - session.usage_mark.get(key, 0))
            for key in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens", "total_tokens")
        }
        session.usage_mark = dict(session.usage_total)
        session.last_active = time.monotonic()
        if finished:
            self._schedule_close(session)
        return CodexModelResult(
            content="\n\n".join(content_parts),
            tool_calls=tool_calls,
            reasoning="".join(reasoning_parts),
            usage=usage,
        )

    def _close_idle_sessions(self) -> None:
        now = time.monotonic()
        by_age = sorted(self._sessions.values(), key=lambda item: item.last_active)
        for position, session in enumerate(by_age):
            too_many = len(by_age) - position > _MAX_OPEN_SESSIONS
            if too_many or now - session.last_active > _IDLE_SESSION_SECONDS:
                self._schedule_close(session)

    def _schedule_close(self, session: _TurnSession) -> None:
        if self._sessions.pop(session.thread_id, None) is None:
            return
        for call_id in session.pending:
            self._pending_index.pop(call_id, None)
        self.server.unsubscribe(session.thread_id)
        task = asyncio.ensure_future(self._close_thread(session))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _close_thread(self, session: _TurnSession) -> None:
        if not self.server.running:
            return
        try:
            if session.turn_id:
                await self.server.request(
                    "turn/interrupt", {"threadId": session.thread_id, "turnId": session.turn_id},
                )
        except Exception:
            pass
        try:
            await self.server.request("thread/unsubscribe", {"threadId": session.thread_id})
        except Exception:
            logger.debug("Codex thread cleanup failed", exc_info=True)


codex_account = CodexAccountService()
