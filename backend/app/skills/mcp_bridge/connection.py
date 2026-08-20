from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.agent.settings_store import MCPServerConfig
from app.agent.ui_events import publish_ui_event

logger = logging.getLogger(__name__)

MCP_SERVER_STATES = {"stopped", "starting", "connected", "unhealthy", "failed"}

_ACTIVE_MANAGERS: dict[str, "ServerManager"] = {}
_ACTIVE_LOCK = threading.RLock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ok(**payload) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _err(code: str, message: str, **details) -> str:
    return json.dumps(
        {
            "ok": False,
            "status": "error",
            "error": {
                "code": code,
                "message": message,
                "details": details,
            },
        },
        ensure_ascii=False,
    )


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _to_jsonable(model_dump())
    if hasattr(value, "__dict__"):
        return _to_jsonable(vars(value))
    return str(value)


def _resolve_command(command: str) -> str:
    if not command:
        return ""
    resolved = shutil.which(command)
    return resolved or command


def _child_process_ids() -> set[int]:
    try:
        import psutil

        return {process.pid for process in psutil.Process(os.getpid()).children(recursive=True)}
    except Exception:
        return set()


class _TailBuffer:
    def __init__(self, limit: int = 8192) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._text = ""

    def write(self, chunk: Any) -> int:
        if isinstance(chunk, bytes):
            text = chunk.decode("utf-8", errors="replace")
        else:
            text = str(chunk)
        with self._lock:
            self._text = (self._text + text)[-self.limit :]
        return len(text)

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        with self._lock:
            return self._text


class ServerManager:
    def __init__(self, cfg: MCPServerConfig) -> None:
        self.cfg = cfg
        self.connected = False
        self.state: Literal["stopped", "starting", "connected", "unhealthy", "failed"] = "stopped"
        self.last_error = ""
        self.unhealthy_reason = ""
        self.tool_count = 0
        self.tools: list[Any] = []
        self.reflected_tool_names: list[str] = []
        self.startup_phase = "idle"
        self.started_at: str | None = None
        self.connected_at: str | None = None
        self.disconnected_at: str | None = None
        self.last_call_started_at: str | None = None
        self.last_call_duration_ms: int | None = None
        self.failed_call_count = 0
        self.pid: int | None = None
        self.stderr_tail = _TailBuffer()
        self.resolved_executable = _resolve_command(cfg.command)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._session: Any | None = None
        self._stop: asyncio.Event | None = None
        self._serve_task: asyncio.Task | None = None
        self._liveness_task: asyncio.Task | None = None
        self._lifecycle_lock = threading.RLock()
        self._last_event_signature: tuple[Any, ...] | None = None
        self.liveness_interval_seconds = 5.0
        self._child_pids_before_start: set[int] = set()

    def start(self) -> bool:
        self._register_active()
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive() and self.connected:
                return True
            if not self._stop_current_runtime_locked(join_timeout=5):
                return False
            self._prepare_start_locked()

        if not self._ready.wait(timeout=self.cfg.startup_timeout_ms / 1000):
            with self._lifecycle_lock:
                self.last_error = f"Startup timed out after {self.cfg.startup_timeout_ms}ms"
                self.unhealthy_reason = self.last_error
                self.state = "failed"
                self.startup_phase = "startup_timeout"
                self.connected = False
                self._stop_current_runtime_locked(join_timeout=5)
                self._publish_changed()
            return False
        return self.connected

    def restart(self) -> bool:
        with self._lifecycle_lock:
            if not self._stop_current_runtime_locked(join_timeout=5):
                return False
        return self.start()

    def close(self) -> None:
        stopped = True
        with self._lifecycle_lock:
            stopped = self._stop_current_runtime_locked(join_timeout=5)
            self.connected = False
            self.disconnected_at = _utcnow()
            if stopped:
                self.state = "stopped"
                self.startup_phase = "idle"
        if stopped:
            self._unregister_active()

    def call_tool_sync(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if (self._loop is None or self._session is None or not self.connected) and not self._recover_before_call():
            return _err(
                "mcp_disconnected",
                "The MCP server is not connected.",
                server=self.cfg.name,
                tool=tool_name,
                state=self.state,
                last_error=self.last_error,
            )

        assert self._loop is not None
        assert self._session is not None

        started = datetime.now(timezone.utc)
        self.last_call_started_at = started.isoformat()
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(tool_name, arguments=arguments),
                self._loop,
            )
            result = future.result(timeout=self.cfg.call_timeout_ms / 1000)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._mark_unhealthy(
                f"Tool call timed out after {self.cfg.call_timeout_ms}ms",
                failed_call=True,
            )
            with self._lifecycle_lock:
                self._stop_current_runtime_locked(join_timeout=5)
            return _err(
                "mcp_timeout",
                "The MCP tool call timed out.",
                server=self.cfg.name,
                tool=tool_name,
                timeout_ms=self.cfg.call_timeout_ms,
            )
        except Exception as exc:
            self._mark_unhealthy(str(exc), failed_call=True)
            return _err(
                "mcp_call_failed",
                "The MCP tool call failed.",
                server=self.cfg.name,
                tool=tool_name,
                raw_error=str(exc),
            )
        finally:
            elapsed = datetime.now(timezone.utc) - started
            self.last_call_duration_ms = int(elapsed.total_seconds() * 1000)

        payload = _to_jsonable(result)
        self.last_error = ""
        self.unhealthy_reason = ""
        return _ok(
            status="ok",
            server=self.cfg.name,
            tool=tool_name,
            is_error=bool(getattr(result, "isError", False)),
            structured_content=payload.get("structuredContent"),
            content=payload.get("content", []),
            raw=payload,
        )

    def refresh_tools(self) -> bool:
        if (self._loop is None or self._session is None or not self.connected) and not self.restart():
            return False

        assert self._loop is not None
        assert self._session is not None
        try:
            future = asyncio.run_coroutine_threadsafe(self._session.list_tools(), self._loop)
            tools_response = future.result(timeout=self.cfg.call_timeout_ms / 1000)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._mark_unhealthy(
                f"Tool refresh timed out after {self.cfg.call_timeout_ms}ms",
                failed_call=True,
            )
            with self._lifecycle_lock:
                self._stop_current_runtime_locked(join_timeout=5)
            return False
        except Exception as exc:
            self._mark_unhealthy(str(exc), failed_call=True)
            return False

        self.tools = list(getattr(tools_response, "tools", []) or [])
        self.tool_count = len(self.tools)
        self.last_error = ""
        self.unhealthy_reason = ""
        self.state = "connected"
        self.connected = True
        return True

    def status(self) -> dict[str, Any]:
        return {
            "name": self.cfg.name,
            "enabled": self.cfg.enabled,
            "transport": self.cfg.transport,
            "connected": self.connected,
            "state": self.state,
            "tool_count": self.tool_count,
            "last_error": self.last_error or None,
            "unhealthy_reason": self.unhealthy_reason or None,
            "description": self.cfg.description,
            "command": self.cfg.command,
            "args": list(self.cfg.args),
            "cwd": self.cfg.cwd,
            "url": self.cfg.url,
            "resolved_executable": self.resolved_executable,
            "startup_phase": self.startup_phase,
            "pid": self.pid,
            "started_at": self.started_at,
            "connected_at": self.connected_at,
            "disconnected_at": self.disconnected_at,
            "stderr_tail": self.stderr_tail.getvalue(),
            "last_call_started_at": self.last_call_started_at,
            "last_call_duration_ms": self.last_call_duration_ms,
            "failed_call_count": self.failed_call_count,
            "remote_tool_names": [str(getattr(tool, "name", "")) for tool in self.tools],
            "reflected_tool_names": list(self.reflected_tool_names),
        }

    def _recover_before_call(self) -> bool:
        if not self.cfg.reconnect_on_unhealthy and self.state in {"unhealthy", "failed"}:
            return False
        return self.restart()

    def _prepare_start_locked(self) -> None:
        self.connected = False
        self.last_error = ""
        self.unhealthy_reason = ""
        self.tool_count = 0
        self.tools = []
        self.reflected_tool_names = []
        self._session = None
        self._ready.clear()
        self.state = "starting"
        self.startup_phase = "thread_start"
        self.started_at = _utcnow()
        self.disconnected_at = None
        self.resolved_executable = _resolve_command(self.cfg.command)
        self._child_pids_before_start = _child_process_ids()
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"mcp-{self.cfg.name}",
            daemon=True,
        )
        self._publish_changed()
        self._thread.start()

    def _stop_current_runtime_locked(self, join_timeout: float) -> bool:
        loop = self._loop
        stop_event = self._stop
        task = self._serve_task
        if loop is not None:
            try:
                if stop_event is not None:
                    loop.call_soon_threadsafe(stop_event.set)
                if task is not None:
                    loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)
            if thread.is_alive():
                self.connected = False
                self.state = "unhealthy"
                self.startup_phase = "stop_timeout"
                self.last_error = f"MCP server thread did not stop within {join_timeout}s"
                self.unhealthy_reason = self.last_error
                self.disconnected_at = _utcnow()
                return False

        self.connected = False
        self._thread = None
        self._loop = None
        self._session = None
        self._stop = None
        self._serve_task = None
        self.pid = None
        return True

    def _mark_unhealthy(self, reason: str, *, failed_call: bool = False) -> None:
        self.last_error = reason
        self.unhealthy_reason = reason
        self.connected = False
        self.state = "unhealthy"
        self.disconnected_at = _utcnow()
        if failed_call:
            self.failed_call_count += 1
        self._publish_changed()

    def _register_active(self) -> None:
        with _ACTIVE_LOCK:
            _ACTIVE_MANAGERS[self.cfg.name] = self

    def _unregister_active(self) -> None:
        with _ACTIVE_LOCK:
            current = _ACTIVE_MANAGERS.get(self.cfg.name)
            if current is self:
                _ACTIVE_MANAGERS.pop(self.cfg.name, None)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._stop = asyncio.Event()
        try:
            self._serve_task = loop.create_task(self._serve())
            loop.run_until_complete(self._serve_task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.last_error = str(exc)
            self.unhealthy_reason = str(exc)
            self.state = "failed"
            logger.exception("Failed to start MCP server '%s'", self.cfg.name)
            self._ready.set()
            self._publish_changed()
        finally:
            self.connected = False
            self._session = None
            self._stop = None
            self._serve_task = None
            self._liveness_task = None
            self._loop = None
            self.pid = None
            if self.state == "connected":
                self.state = "stopped"
            self._publish_changed()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    async def _serve(self) -> None:
        from mcp import ClientSession

        self.startup_phase = "opening_transport"
        if self.cfg.transport == "streamable_http":
            from mcp.client.streamable_http import streamablehttp_client

            if not self.cfg.url:
                raise ValueError("streamable_http MCP server requires a url")

            async with streamablehttp_client(
                self.cfg.url,
                headers=dict(self.cfg.headers) or None,
                timeout=max(1, self.cfg.startup_timeout_ms / 1000),
                sse_read_timeout=max(1, self.cfg.call_timeout_ms / 1000),
            ) as (read_stream, write_stream, _get_session_id):
                await self._initialize_session(ClientSession, read_stream, write_stream)
        else:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=_resolve_command(self.cfg.command),
                args=list(self.cfg.args),
                env={**os.environ, **self.cfg.env},
                cwd=self.cfg.cwd or None,
            )

            async with stdio_client(params, errlog=self.stderr_tail) as (read_stream, write_stream):
                await self._initialize_session(ClientSession, read_stream, write_stream)

    async def _initialize_session(self, client_session_cls, read_stream, write_stream) -> None:
        self.startup_phase = "initializing_session"
        async with client_session_cls(read_stream, write_stream) as session:
            await session.initialize()
            self.startup_phase = "listing_tools"
            tools_response = await session.list_tools()
            self._session = session
            self.tools = list(getattr(tools_response, "tools", []) or [])
            self.tool_count = len(self.tools)
            self.connected = True
            self.state = "connected"
            self.startup_phase = "ready"
            self.connected_at = _utcnow()
            self.pid = self._find_child_pid()
            self.last_error = ""
            self.unhealthy_reason = ""
            self._ready.set()
            self._publish_changed()
            assert self._stop is not None
            self._liveness_task = asyncio.create_task(
                self._probe_liveness(session),
                name=f"mcp-liveness-{self.cfg.name}",
            )
            try:
                await self._stop.wait()
            finally:
                self._liveness_task.cancel()
                await asyncio.gather(self._liveness_task, return_exceptions=True)
                self._liveness_task = None

    def _find_child_pid(self) -> int | None:
        """Best-effort PID capture for stdio transports.

        The MCP SDK exposes streams, not its subprocess handle. Use the new
        child-process set captured immediately before startup and match the
        configured command when psutil is available. HTTP transports correctly
        remain PID-less.
        """

        if self.cfg.transport != "stdio":
            return None
        try:
            import psutil

            command = Path(self.resolved_executable or self.cfg.command).name.lower()
            candidates = []
            for process in psutil.Process(os.getpid()).children(recursive=True):
                if process.pid in self._child_pids_before_start:
                    continue
                try:
                    cmdline = [str(part).lower() for part in (process.cmdline() or [])]
                    name = str(process.name() or "").lower()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
                haystack = " ".join([name, *cmdline])
                if command and (command in haystack or Path(command).stem in haystack):
                    candidates.append(process.pid)
            return candidates[0] if candidates else None
        except Exception:
            return None

    async def _probe_liveness(self, session: Any) -> None:
        """Detect a child or transport that died without a tool call.

        ``list_tools`` is the MCP-level health check and is safe for both
        stdio and streamable HTTP transports. The probe owns no reconnect
        policy: it records an unhealthy transition and lets the next call or
        explicit UI reconnect decide whether to restart the server.
        """

        assert self._stop is not None
        interval = max(0.01, float(self.liveness_interval_seconds))
        timeout = max(1.0, self.cfg.call_timeout_ms / 1000)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass

            if not self.connected:
                return
            try:
                response = await asyncio.wait_for(session.list_tools(), timeout=timeout)
                tools = list(getattr(response, "tools", []) or [])
                if len(tools) != self.tool_count:
                    self.tools = tools
                    self.tool_count = len(tools)
                    self._publish_changed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._mark_unhealthy(f"MCP liveness check failed: {exc}")
                self._stop.set()
                return

    def _publish_changed(self) -> None:
        status = self.status()
        signature = (
            status["name"],
            status["connected"],
            status["state"],
            status["tool_count"],
            status["last_error"],
            status["unhealthy_reason"],
            status["startup_phase"],
            status["pid"],
        )
        if signature == self._last_event_signature:
            return
        self._last_event_signature = signature
        publish_ui_event(
            "mcp.changed",
            {
                "name": status["name"],
                "connected": status["connected"],
                "state": status["state"],
                "tool_count": status["tool_count"],
                "last_error": status["last_error"],
                "unhealthy_reason": status["unhealthy_reason"],
                "startup_phase": status["startup_phase"],
                "pid": status["pid"],
            },
        )


def get_active_mcp_manager(name: str) -> ServerManager | None:
    with _ACTIVE_LOCK:
        return _ACTIVE_MANAGERS.get(name)


def get_mcp_runtime_diagnostics() -> list[dict[str, Any]]:
    with _ACTIVE_LOCK:
        return [manager.status() for manager in _ACTIVE_MANAGERS.values()]


def _iter_enabled_configs(agent_settings) -> list[MCPServerConfig]:
    mcp_settings = getattr(agent_settings, "mcp", object())
    if not bool(getattr(mcp_settings, "enabled", True)):
        return []
    raw_servers = getattr(mcp_settings, "servers", []) or []
    configs: list[MCPServerConfig] = []
    for raw in raw_servers:
        cfg = raw if isinstance(raw, MCPServerConfig) else MCPServerConfig.model_validate(raw)
        if cfg.enabled:
            configs.append(cfg)
    return configs


def ensure_mcp_manager(cfg: MCPServerConfig) -> ServerManager:
    with _ACTIVE_LOCK:
        manager = _ACTIVE_MANAGERS.get(cfg.name)
        if manager is not None and manager.cfg.model_dump() == cfg.model_dump():
            return manager
        if manager is not None:
            try:
                manager.close()
            except Exception:
                logger.exception("Failed to close stale MCP server '%s'", cfg.name)
        manager = ServerManager(cfg)
        _ACTIVE_MANAGERS[cfg.name] = manager
    return manager


def reconnect_mcp_server(name: str) -> dict[str, Any] | None:
    with _ACTIVE_LOCK:
        manager = _ACTIVE_MANAGERS.get(name)
    if manager is None:
        from app.agent.settings_store import load_agent_settings
        from app.config import settings as app_settings

        agent_settings = load_agent_settings(app_settings)
        cfg = next((s for s in agent_settings.mcp.servers if s.name == name), None)
        if cfg is None:
            return None
        manager = ensure_mcp_manager(cfg)
    manager.restart()
    return manager.status()


def restart_enabled_mcp_servers(agent_settings) -> list[dict[str, Any]]:
    if not bool(getattr(getattr(agent_settings, "mcp", object()), "enabled", True)):
        return []
    if importlib.util.find_spec("mcp") is None:
        return []

    statuses: list[dict[str, Any]] = []
    for cfg in _iter_enabled_configs(agent_settings):
        manager = ensure_mcp_manager(cfg)
        manager.start()
        statuses.append(manager.status())
    return statuses


def reset_mcp_runtime() -> None:
    from .registry import clear_all_reflected_tools

    clear_all_reflected_tools()
    with _ACTIVE_LOCK:
        managers = list(_ACTIVE_MANAGERS.values())
    for manager in managers:
        try:
            manager.close()
        except Exception:
            logger.exception("Failed to close MCP server '%s'", manager.cfg.name)
