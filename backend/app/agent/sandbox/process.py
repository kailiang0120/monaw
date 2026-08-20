"""Shared process execution helpers for local and container runners."""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable

from app.agent.sandbox.models import SandboxExecutionRequest

_DEFAULT_MAX_OUTPUT_BYTES = 1048576


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _tail_lines(text: str, limit: int) -> str:
    if limit <= 0:
        return text
    lines = text.splitlines(keepends=True)
    return "".join(lines[-limit:])


def _format_output(text: str, *, max_chars: int, tail_lines: int = 0, return_mode: str = "full") -> str:
    normalized_mode = (return_mode or "full").strip().lower()
    if tail_lines > 0 or normalized_mode == "tail":
        return _truncate(_tail_lines(text, tail_lines or 200), max_chars)
    if normalized_mode == "summary":
        return _truncate(_tail_lines(text, tail_lines or 80), max_chars)
    if normalized_mode == "head_tail" and len(text) > max_chars:
        half = max(1, max_chars // 2)
        return text[:half] + "\n...[truncated middle]...\n" + text[-half:]
    return _truncate(text, max_chars)


@dataclass(slots=True)
class _CappedProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False


class _OutputBudget:
    def __init__(self, limit: int) -> None:
        self.remaining = max(1, int(limit))
        self._lock = threading.Lock()

    def take(self, chunk: str) -> tuple[str, bool]:
        if not chunk:
            return "", False
        with self._lock:
            if self.remaining <= 0:
                return "", True
            if len(chunk) > self.remaining:
                kept = chunk[: self.remaining]
                self.remaining = 0
                return kept, True
            self.remaining -= len(chunk)
            return chunk, False


def _resource_int(request: SandboxExecutionRequest, key: str, default: int) -> int:
    try:
        return max(1, int((request.resources or {}).get(key) or default))
    except (TypeError, ValueError):
        return default


def _bounded_timeout(request: SandboxExecutionRequest) -> int:
    requested = max(1, int(request.timeout or 1))
    configured = _resource_int(request, "timeout_seconds", requested)
    return min(requested, configured)


def _stream_capture_limits(request: SandboxExecutionRequest) -> tuple[int, int, int]:
    total = _resource_int(request, "max_output_bytes", _DEFAULT_MAX_OUTPUT_BYTES)
    stdout = min(max(1, int(request.max_stdout or 8192)) + 1, total)
    stderr = min(max(1, int(request.max_stderr or 4096)) + 1, total)
    return stdout, stderr, total


def _read_stream_capped(stream, sink: list[str], *, limit: int, budget: _OutputBudget) -> None:
    if stream is None:
        return
    remaining = max(1, int(limit))
    truncated = False
    while True:
        chunk = stream.read(4096)
        if not chunk:
            break
        if remaining <= 0:
            truncated = True
            continue
        kept = chunk[:remaining]
        remaining -= len(kept)
        kept, budget_truncated = budget.take(kept)
        if kept:
            sink.append(kept)
        if len(chunk) > len(kept) or budget_truncated:
            truncated = True
    if truncated:
        sink.append("\n...[truncated]")


def _run_command_capped(
    command: list[str],
    request: SandboxExecutionRequest,
    *,
    popen_kwargs: dict | None = None,
    on_timeout: Callable[[subprocess.Popen[str]], None] | None = None,
    on_cancel: Callable[[subprocess.Popen[str]], None] | None = None,
    use_request_cwd: bool = True,
    use_request_env: bool = True,
) -> _CappedProcessResult:
    stdout_limit, stderr_limit, total_limit = _stream_capture_limits(request)
    budget = _OutputBudget(total_limit)
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    cwd = (request.workdir or None) if use_request_cwd else None
    env = request.env if use_request_env else None
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **(popen_kwargs or {}),
    )
    stdout_thread = threading.Thread(
        target=_read_stream_capped,
        args=(proc.stdout, stdout_chunks),
        kwargs={"limit": stdout_limit, "budget": budget},
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_stream_capped,
        args=(proc.stderr, stderr_chunks),
        kwargs={"limit": stderr_limit, "budget": budget},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    cancelled = False
    try:
        deadline = time.monotonic() + _bounded_timeout(request)
        while proc.poll() is None:
            cancel_event = request.cancel_event
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                if on_cancel is not None:
                    on_cancel(proc)
                elif on_timeout is not None:
                    on_timeout(proc)
                elif proc.poll() is None:
                    proc.kill()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                if on_timeout is not None:
                    on_timeout(proc)
                elif proc.poll() is None:
                    proc.kill()
                break
            try:
                proc.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    finally:
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
    return _CappedProcessResult(
        returncode=proc.returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
        timed_out=timed_out,
        cancelled=cancelled,
    )


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        proc.kill()


def _script_suffix(shell: str) -> str:
    return {"powershell": ".ps1", "pwsh": ".ps1", "cmd": ".cmd", "bash": ".sh"}[shell.lower()]


def _write_script(command: str, shell: str) -> str:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=_script_suffix(shell),
        delete=False,
    ) as handle:
        handle.write(command)
        return handle.name


def shell_command(shell: str, script_path: str) -> list[str]:
    requested = shell.lower()
    if requested in {"powershell", "pwsh"}:
        shell_exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [
            shell_exe,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script_path,
        ]
    if requested == "cmd":
        return ["cmd.exe", "/C", script_path]
    if requested == "bash":
        return [shutil.which("bash") or "bash", script_path]
    raise ValueError(f"Unsupported shell '{shell}'")
