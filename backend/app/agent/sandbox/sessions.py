from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.agent.sandbox.backends.local_direct import (
    _format_output,
    _stream_capture_limits,
    _write_artifact,
    _write_script,
    shell_command,
)
from app.agent.sandbox.models import (
    SandboxRunMetadata,
    SandboxSessionHandle,
    SandboxSessionStartRequest,
    SandboxSessionStatus,
)


class SandboxSessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def start_local_direct(self, request: SandboxSessionStartRequest) -> SandboxSessionHandle:
        return self._start_local_session(request, backend="local_direct")

    def start_local_restricted(self, request: SandboxSessionStartRequest) -> SandboxSessionHandle:
        return self._start_local_session(request, backend="local_restricted")

    def _start_local_session(self, request: SandboxSessionStartRequest, *, backend: str) -> SandboxSessionHandle:
        command_id = uuid.uuid4().hex[:12]
        script_path = _write_script(request.command, request.shell)
        is_restricted = backend == "local_restricted"
        cleanup_mode = (
            "taskkill_process_tree"
            if is_restricted and os.name == "nt"
            else "process_group" if is_restricted else "manual_stop"
        )
        metadata = SandboxRunMetadata(
            backend=backend,
            selected_backend=backend,
            security_label="advisory" if is_restricted else "none",
            filesystem_isolation="none",
            process_isolation="advisory" if is_restricted else "none",
            process_cleanup=cleanup_mode,
            network_isolation="none",
            env_inheritance=str(request.env_metadata.get("env_inheritance", "scrubbed")),
            profile=request.profile,
            mode=str(request.env_metadata.get("mode", "auto")),
            network=request.network,
            workdir=request.workdir,
            inherited_env_keys=list(request.env_metadata.get("inherited_env_keys", [])),
            explicit_env_keys=list(request.env_metadata.get("explicit_env_keys", [])),
            blocked_env_keys=list(request.env_metadata.get("blocked_env_keys", [])),
            env_keys=list(request.env_metadata.get("env_keys", [])),
            warnings=[
                *list(request.env_metadata.get("warnings", [])),
                *(["local_restricted_is_advisory"] if is_restricted else []),
            ],
        ).model_dump()
        stdout_limit, stderr_limit, _total_limit = _stream_capture_limits(request)
        popen_kwargs: dict[str, object] = {}
        if is_restricted:
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            shell_command(request.shell, script_path),
            cwd=request.workdir or None,
            env=request.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
            **popen_kwargs,
        )
        session = {
            "id": command_id,
            "backend": backend,
            "process": proc,
            "command": request.command,
            "shell": request.shell,
            "workdir": request.workdir,
            "script_path": script_path,
            "stdout": [],
            "stderr": [],
            "stdout_size": 0,
            "stderr_size": 0,
            "stdout_limit": stdout_limit,
            "stderr_limit": stderr_limit,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "started_at": time.monotonic(),
            "env_keys": list(request.env_metadata.get("explicit_env_keys", [])),
            "sandbox": metadata,
        }
        with self._lock:
            self._sessions[command_id] = session
        threading.Thread(target=self._read_stream, args=(proc.stdout, command_id, "stdout"), daemon=True).start()
        threading.Thread(target=self._read_stream, args=(proc.stderr, command_id, "stderr"), daemon=True).start()
        return SandboxSessionHandle(session_id=command_id, backend=backend, pid=proc.pid, sandbox=metadata)

    def poll(
        self,
        session_id: str,
        *,
        max_output: int = 8192,
        tail_lines: int = 200,
        return_mode: str = "tail",
    ) -> SandboxSessionStatus:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return SandboxSessionStatus(
                status="error",
                command_id=session_id,
                reason_code="unknown_command",
                error=f"Unknown command_id '{session_id}'",
            )
        payload = self._session_payload(
            session,
            max_output=max_output,
            tail_lines=tail_lines,
            return_mode=return_mode,
        )
        if payload.exit_code is not None:
            with self._lock:
                self._sessions.pop(session_id, None)
        return payload

    def write_stdin(self, session_id: str, text: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return {"status": "error", "error": f"Unknown command_id '{session_id}'", "reason_code": "unknown_command", "sandbox": {}}
        proc = session["process"]
        if proc.poll() is not None or proc.stdin is None:
            return {"status": "error", "error": "Process is not accepting stdin.", "sandbox": session.get("sandbox", {})}
        proc.stdin.write(text)
        proc.stdin.flush()
        return {
            "status": "ok",
            "command_id": session_id,
            "bytes_written": len(text.encode("utf-8")),
            "sandbox": session.get("sandbox", {}),
        }

    def stop(self, session_id: str, signal_name: str = "terminate") -> SandboxSessionStatus:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return SandboxSessionStatus(
                status="error",
                command_id=session_id,
                reason_code="unknown_command",
                error=f"Unknown command_id '{session_id}'",
            )
        proc = session["process"]
        if proc.poll() is None:
            self._stop_process(session, signal_name)
        payload = self._session_payload(session)
        with self._lock:
            self._sessions.pop(session_id, None)
        return payload

    def _stop_process(self, session: dict[str, Any], signal_name: str) -> None:
        proc = session["process"]
        force = str(signal_name).lower() == "kill"
        if session.get("backend") == "local_restricted":
            if os.name == "nt":
                command = ["taskkill", "/T", "/PID", str(proc.pid)]
                if force:
                    command.insert(1, "/F")
                subprocess.run(command, capture_output=True, text=True, check=False)
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
                except ProcessLookupError:
                    return
                except Exception:
                    proc.kill() if force else proc.terminate()
        elif force:
            proc.kill()
        else:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    def cleanup_stale(self, *, max_age_seconds: int) -> list[str]:
        cutoff = time.monotonic() - max_age_seconds
        removed: list[str] = []
        with self._lock:
            session_ids = [
                session_id
                for session_id, session in self._sessions.items()
                if session["started_at"] <= cutoff
            ]
        for session_id in session_ids:
            self.stop(session_id, "kill")
            removed.append(session_id)
        return removed

    def _read_stream(self, stream, command_id: str, stream_name: str) -> None:
        if stream is None:
            return
        while True:
            chunk = stream.readline(4096)
            if not chunk:
                break
            with self._lock:
                session = self._sessions.get(command_id)
                if session is None:
                    return
                limit_key = f"{stream_name}_limit"
                size_key = f"{stream_name}_size"
                truncated_key = f"{stream_name}_truncated"
                limit = int(session.get(limit_key, 8192) or 8192)
                current_size = int(session.get(size_key, 0) or 0)
                remaining = max(0, limit - current_size)
                if remaining <= 0:
                    if not session.get(truncated_key):
                        session[stream_name].append("\n...[truncated]")
                        session[truncated_key] = True
                    continue
                kept = chunk[:remaining]
                session[stream_name].append(kept)
                session[size_key] = current_size + len(kept)
                if len(chunk) > len(kept) and not session.get(truncated_key):
                    session[stream_name].append("\n...[truncated]")
                    session[truncated_key] = True

    def _session_payload(
        self,
        session: dict[str, Any],
        *,
        max_output: int = 8192,
        tail_lines: int = 200,
        return_mode: str = "tail",
    ) -> SandboxSessionStatus:
        proc = session["process"]
        stdout_raw = "".join(session["stdout"])
        stderr_raw = "".join(session["stderr"])
        command_id = session["id"]
        stdout_path = _write_artifact(command_id, "stdout", stdout_raw)
        stderr_path = _write_artifact(command_id, "stderr", stderr_raw)
        exit_code = proc.poll()
        if exit_code is not None:
            script_path = session.get("script_path", "")
            if script_path:
                Path(script_path).unlink(missing_ok=True)
        return SandboxSessionStatus(
            status="running" if exit_code is None else "ok" if exit_code == 0 else "error",
            command_id=command_id,
            pid=proc.pid,
            exit_code=exit_code,
            duration_ms=int((time.monotonic() - session["started_at"]) * 1000),
            stdout=_format_output(stdout_raw, max_chars=max(1, int(max_output or 8192)), tail_lines=int(tail_lines or 0), return_mode=return_mode),
            stderr=_format_output(stderr_raw, max_chars=max(1, int(max_output or 8192)), tail_lines=int(tail_lines or 0), return_mode=return_mode),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            shell=session["shell"],
            workdir=session["workdir"],
            env_keys=session["env_keys"],
            sandbox=session.get("sandbox", {}),
        )
