"""Start and stop the Laya server that setup.bat installs for Monaw.

setup.bat installs ``laya[serve]`` into ``<MONAW_HOME>/laya/.venv`` and
downloads the English checkpoint. When the decision model is enabled and
managed, the backend starts that server as a hidden child process on startup
and stops it on shutdown. Electron stops the backend with ``taskkill /t``,
which also ends this child.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.agent.runtime_paths import MONAW_HOME_DIR, RUNTIME_DIR

logger = logging.getLogger(__name__)

LAYA_DIR = MONAW_HOME_DIR / "laya"
LAYA_LOG_PATH = RUNTIME_DIR / "launcher" / "laya-serve.log"
# Tests and anyone who wants the backend to leave Laya alone set this to 1.
DISABLE_ENV = "MONAW_DISABLE_MANAGED_LAYA"
_STOP_TIMEOUT_SECONDS = 5.0


def managed_laya_executable() -> Path | None:
    """The setup-installed ``laya-serve``, or ``None`` when it is missing or disabled."""
    if os.environ.get(DISABLE_ENV) == "1":
        return None
    scripts = "Scripts" if os.name == "nt" else "bin"
    name = "laya-serve.exe" if os.name == "nt" else "laya-serve"
    executable = LAYA_DIR / ".venv" / scripts / name
    return executable if executable.is_file() else None


def _loopback_host_port(endpoint: str) -> tuple[str, int] | None:
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return None
    return host, parsed.port or (443 if parsed.scheme == "https" else 80)


async def _healthy(endpoint: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
            response = await client.get(f"{endpoint.rstrip('/')}/health")
        return response.status_code == 200
    except httpx.HTTPError:
        return False


class ManagedLayaServer:
    """Owns at most one ``laya-serve`` child process for the backend."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self.status: dict[str, Any] = {"state": "disabled", "running": False}

    async def start(self, config: Any, *, api_key: str = "") -> dict[str, Any]:
        """Start the server if the settings ask for it. Never raises."""
        try:
            self.status = await self._start(config, api_key)
        except Exception as exc:
            logger.exception("decision model: failed to start laya-serve")
            self.status = {"state": "error", "running": False, "error": f"{type(exc).__name__}: {exc}"}
        return self.status

    async def _start(self, config: Any, api_key: str) -> dict[str, Any]:
        if not getattr(config, "enabled", False) or not getattr(config, "managed", False):
            return {"state": "disabled", "running": False}
        endpoint = str(getattr(config, "endpoint", "") or "")
        address = _loopback_host_port(endpoint)
        if address is None:
            return {"state": "external_endpoint", "running": False, "endpoint": endpoint}
        executable = managed_laya_executable()
        if executable is None:
            return {"state": "not_installed", "running": False, "install_dir": str(LAYA_DIR)}
        if await _healthy(endpoint):
            return {"state": "already_running", "running": True, "endpoint": endpoint}

        host, port = address
        env = {
            **os.environ,
            "USE_TF": "0",
            "LAYA_HOST": "127.0.0.1" if host == "localhost" else host,
            "LAYA_PORT": str(port),
            "LAYA_MODELS": "english",
            "LAYA_DEFAULT_MODEL": "english",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
        }
        if api_key:
            env["LAYA_API_KEY"] = api_key
        LAYA_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(LAYA_LOG_PATH, "ab")
        try:
            self._process = subprocess.Popen(
                [str(executable)],
                cwd=str(LAYA_DIR),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        finally:
            log_file.close()
        logger.info("decision model: started laya-serve pid=%s endpoint=%s", self._process.pid, endpoint)
        # Loading the checkpoint takes a while on CPU. Turns that arrive first
        # record the selector as unavailable, which never blocks them.
        return {"state": "starting", "running": True, "pid": self._process.pid, "endpoint": endpoint, "log": str(LAYA_LOG_PATH)}

    async def restart(self, config: Any, *, api_key: str = "") -> dict[str, Any]:
        """Apply changed settings without restarting the backend."""
        await self.stop()
        return await self.start(config, api_key=api_key)

    async def describe(self, config: Any) -> dict[str, Any]:
        """Live state for the settings UI.

        ``state`` is one of off, not_installed, starting, ready, unreachable or error.
        """
        enabled = bool(getattr(config, "enabled", False))
        managed = bool(getattr(config, "managed", False))
        endpoint = str(getattr(config, "endpoint", "") or "")
        installed = managed_laya_executable() is not None
        payload = {
            "enabled": enabled,
            "managed": managed,
            "endpoint": endpoint,
            "installed": installed,
            "error": str(self.status.get("error", "") or ""),
        }
        if not enabled:
            return {**payload, "state": "off"}
        if self.status.get("state") == "error":
            return {**payload, "state": "error"}
        if managed and _loopback_host_port(endpoint) is not None and not installed:
            return {**payload, "state": "not_installed"}
        if await _healthy(endpoint):
            return {**payload, "state": "ready"}
        process = self._process
        if process is not None:
            exit_code = process.poll()
            if exit_code is None:
                return {**payload, "state": "starting"}
            payload["error"] = f"laya-serve exited with code {exit_code}. See {LAYA_LOG_PATH}"
        return {**payload, "state": "unreachable"}

    async def stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, _STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
        logger.info("decision model: stopped laya-serve pid=%s", process.pid)
        self.status = {"state": "stopped", "running": False}
