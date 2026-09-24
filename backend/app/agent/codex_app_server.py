"""JSON-RPC client for the bundled `codex app-server`.

Monaw uses the ChatGPT account only as a model connection. Codex's own tools,
skills, plugins and context blocks are switched off, and Monaw's tools are
registered as dynamic tools so every call comes back here for the normal
permission checks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import uuid
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Codex features that give the model its own way to act or add hidden context.
_DISABLED_FEATURES = (
    "apps", "auth_elicitation", "browser_use", "browser_use_external", "code_mode",
    "code_mode_host", "computer_use", "goals", "guardian_approval", "hooks",
    "image_generation", "in_app_browser", "in_app_local_automation", "memories",
    "multi_agent", "multi_agent_v2", "plugin_sharing", "plugins", "realtime_conversation",
    "recommended_plugins", "remote_plugin", "request_permissions_tool", "shell_tool",
    "skill_mcp_dependency_install", "skill_search", "sleep_tool", "standalone_web_search",
    "tool_call_mcp_elicitation", "tool_suggest", "unified_exec", "view_image",
    "workspace_dependencies",
)
_CONFIG_OVERRIDES = (
    'web_search="disabled"',
    "include_permissions_instructions=false",
    "include_apps_instructions=false",
    "include_collaboration_mode_instructions=false",
    "include_environment_context=false",
    "skills.include_instructions=false",
    "orchestrator.skills.enabled=false",
    "tools.experimental_request_user_input.enabled=false",
    "tools.update_plan.enabled=false",
    "project_doc_max_bytes=0",
)
# Catalog fields that route tools through Codex's JavaScript host or add
# built-in tools. Without them, dynamic tools are plain function tools.
_CATALOG_FIELDS_REMOVED = ("tool_mode", "multi_agent_version", "apply_patch_tool_type")
_CATALOG_FILE = "monaw-model-catalog.json"


class CodexRPCError(RuntimeError):
    def __init__(self, method: str, error: Any) -> None:
        message = error.get("message") if isinstance(error, dict) else str(error)
        super().__init__(f"Codex {method} failed: {message or 'unknown error'}")
        self.error = error


class CodexProcessExited(RuntimeError):
    pass


def default_server_response(method: str) -> tuple[dict | None, dict | None]:
    """Refuse every Codex-side action Monaw did not ask for."""
    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
        return {"decision": "decline"}, None
    if method in {"execCommandApproval", "applyPatchApproval"}:
        return {"decision": "denied"}, None
    if method == "item/tool/requestUserInput":
        return {"answers": {}}, None
    if method == "mcpServer/elicitation/request":
        return {"action": "decline"}, None
    return None, {"code": -32601, "message": f"Monaw does not handle {method}."}


def write_model_catalog(home: Path) -> Path | None:
    """Copy Codex's fetched model catalog with direct tool exposure."""
    try:
        cache = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    models = cache.get("models") if isinstance(cache, dict) else None
    if not isinstance(models, list) or not models:
        return None
    patched = []
    for model in models:
        if not isinstance(model, dict):
            continue
        model = {key: value for key, value in model.items() if key not in _CATALOG_FIELDS_REMOVED}
        model["experimental_supported_tools"] = []
        model["supports_search_tool"] = False
        patched.append(model)
    path = home / _CATALOG_FILE
    path.write_text(json.dumps({"models": patched}), encoding="utf-8")
    return path


def _codex_binary() -> tuple[Path, Path | None]:
    from codex_cli_bin import bundled_codex_path

    try:
        from codex_cli_bin import bundled_path_dir
        path_dir = bundled_path_dir()
    except (ImportError, AttributeError):
        path_dir = None
    return Path(bundled_codex_path()), path_dir


class CodexAppServer:
    """One long-lived app-server process shared by sign-in and model calls."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_reader: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._waiters: dict[str, asyncio.Future] = {}
        self._thread_queues: dict[str, asyncio.Queue] = {}
        self._stderr: deque[str] = deque(maxlen=200)
        self.catalog_applied = False

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def ensure_started(self, *, require_catalog: bool = False) -> None:
        async with self._start_lock:
            if self.running and (self.catalog_applied or not require_catalog):
                return
            if not self.running:
                await self._launch()
            if require_catalog and not self.catalog_applied:
                # Codex caches the account's model list on demand. Relaunch
                # once it exists so the patched copy is used.
                await self.request("model/list", {})
                await self._stop()
                await self._launch()
                if not self.catalog_applied:
                    raise RuntimeError("Could not load the OpenAI account model list. Try again shortly.")

    async def _launch(self) -> None:
        binary, path_dir = _codex_binary()
        self.home.mkdir(parents=True, exist_ok=True)
        args = [str(binary)]
        for feature in _DISABLED_FEATURES:
            args += ["-c", f"features.{feature}=false"]
        for override in _CONFIG_OVERRIDES:
            args += ["-c", override]
        catalog = write_model_catalog(self.home)
        self.catalog_applied = catalog is not None
        if catalog is not None:
            args += ["-c", f"model_catalog_json={json.dumps(str(catalog))}"]
        args += ["app-server", "--listen", "stdio://"]

        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.home)
        env["OPENAI_API_KEY"] = ""
        if path_dir is not None:
            env["PATH"] = f"{path_dir}{os.pathsep}{env.get('PATH', '')}"
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=64 * 1024 * 1024,
            **kwargs,
        )
        self._reader = asyncio.create_task(self._read_loop(self._proc))
        self._stderr_reader = asyncio.create_task(self._drain_stderr(self._proc))
        await self.request("initialize", {
            "clientInfo": {"name": "monaw", "title": "Monaw", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        await self._send({"method": "initialized"})

    async def _stop(self) -> None:
        proc, self._proc = self._proc, None
        self.catalog_applied = False
        if proc is None:
            return
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
        self._fail_all(CodexProcessExited("Codex stopped."))

    async def close(self) -> None:
        async with self._start_lock:
            await self._stop()

    async def request(self, method: str, params: dict | None = None) -> Any:
        if not self.running:
            raise CodexProcessExited("Codex is not running.")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._waiters[request_id] = future
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._send(message)
            ok, value = await future
        finally:
            self._waiters.pop(request_id, None)
        if not ok:
            raise CodexRPCError(method, value)
        return value

    async def respond(self, request_id: Any, result: dict | None = None, error: dict | None = None) -> None:
        message: dict[str, Any] = {"id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result if result is not None else {}
        await self._send(message)

    def subscribe(self, thread_id: str) -> asyncio.Queue:
        return self._thread_queues.setdefault(thread_id, asyncio.Queue())

    def unsubscribe(self, thread_id: str) -> None:
        self._thread_queues.pop(thread_id, None)

    def stderr_tail(self) -> str:
        return "\n".join(list(self._stderr)[-20:])

    async def _send(self, message: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise CodexProcessExited("Codex is not running.")
        async with self._write_lock:
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await proc.stdin.drain()

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                await self._dispatch(message)
        except Exception:
            logger.exception("Codex reader stopped")
        finally:
            if self._proc is proc or self._proc is None:
                self._fail_all(CodexProcessExited(
                    f"Codex exited unexpectedly. {self.stderr_tail()}".strip()
                ))

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            self._stderr.append(line.decode("utf-8", "replace").rstrip())

    async def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        if method is None:
            future = self._waiters.get(str(message.get("id")))
            if future is not None and not future.done():
                if "error" in message:
                    future.set_result((False, message["error"]))
                else:
                    future.set_result((True, message.get("result")))
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        queue = self._thread_queues.get(str(params.get("threadId") or ""))
        if queue is not None:
            queue.put_nowait(message)
            return
        if "id" in message:
            result, error = default_server_response(str(method))
            await self.respond(message["id"], result, error)

    def _fail_all(self, exc: Exception) -> None:
        for future in list(self._waiters.values()):
            if not future.done():
                future.set_result((False, {"message": str(exc)}))
        self._waiters.clear()
        for queue in list(self._thread_queues.values()):
            queue.put_nowait({"method": "__exited__", "params": {"message": str(exc)}})
