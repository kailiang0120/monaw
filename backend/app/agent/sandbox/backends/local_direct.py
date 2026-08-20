from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

from app.agent.sandbox.artifacts import write_artifact as _write_artifact
from app.agent.sandbox.models import (
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxRunMetadata,
)
from app.agent.sandbox.path_policy import write_artifact_manifest
from app.agent.sandbox.process import (
    _format_output,
    _kill_process_tree,
    _run_command_capped,
    _write_script,
    shell_command,
)
class LocalDirectRunner:
    backend_name = "local_direct"
    security_label = "none"

    def is_available(self) -> bool:
        return True

    def run(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        script_path = ""
        started_at = time.monotonic()
        metadata = SandboxRunMetadata(
            backend=self.backend_name,
            selected_backend=self.backend_name,
            security_label=self.security_label,
            filesystem_isolation="none",
            process_isolation="none",
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
            warnings=list(request.env_metadata.get("warnings", [])),
            enabled=bool(request.env_metadata.get("enabled", True)),
            trust_class=str(request.env_metadata.get("trust_class", "trusted")),
            required_isolation=str(request.env_metadata.get("required_isolation", "none")),
            network_enforcement=str(request.env_metadata.get("network_enforcement", "none")),
            filesystem_policy=str(request.env_metadata.get("filesystem_policy", "host")),
            write_strategy=str(request.env_metadata.get("write_strategy", "discard")),
            reason=str(request.env_metadata.get("reason", "")),
            reason_code=str(request.env_metadata.get("reason_code", "allowed")),
        ).model_dump()

        try:
            script_path = _write_script(request.command, request.shell)
            command = shell_command(request.shell, script_path)
            popen_kwargs: dict[str, object] = {}
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            completed = _run_command_capped(
                command,
                request,
                popen_kwargs=popen_kwargs,
                on_timeout=_kill_process_tree,
                on_cancel=_kill_process_tree,
            )
            duration_ms = int((time.monotonic() - started_at) * 1000)
            raw_stdout = completed.stdout or ""
            raw_stderr = completed.stderr or ""
            stdout = _format_output(
                raw_stdout,
                max_chars=max(1, int(request.max_stdout or 8192)),
                tail_lines=int(request.tail_lines or 0),
                return_mode=request.return_mode,
            )
            stderr = _format_output(
                raw_stderr,
                max_chars=max(1, int(request.max_stderr or 4096)),
                tail_lines=int(request.tail_lines or 0),
                return_mode=request.return_mode,
            )
            command_id = uuid.uuid4().hex[:12]
            stdout_path = ""
            stderr_path = ""
            if request.save_output_to or len(raw_stdout) > len(stdout) or len(raw_stderr) > len(stderr):
                stdout_path = _write_artifact(command_id, "stdout", raw_stdout)
                stderr_path = _write_artifact(command_id, "stderr", raw_stderr)
            manifest_path = write_artifact_manifest(
                command_id,
                {"stdout_path": stdout_path, "stderr_path": stderr_path},
            )
            metadata["artifacts"] = {
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "manifest_path": manifest_path,
            }
            return SandboxExecutionResult(
                status="error" if completed.timed_out or completed.returncode != 0 else "ok",
                exit_code=completed.returncode,
                duration_ms=duration_ms,
                timed_out=completed.timed_out,
                cancelled=bool(getattr(completed, "cancelled", False)),
                command_id=command_id,
                stdout=stdout,
                stderr=stderr,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                shell=request.shell,
                shell_command=command[0],
                workdir=request.workdir,
                env_keys=list(request.env_metadata.get("explicit_env_keys", [])),
                sandbox=metadata,
                error=(
                    "Command cancelled."
                    if bool(getattr(completed, "cancelled", False))
                    else "Command timed out." if completed.timed_out else ""
                ),
            )
        except Exception as exc:
            return SandboxExecutionResult(
                status="error",
                error=str(exc),
                shell=request.shell,
                workdir=request.workdir,
                env_keys=list(request.env_metadata.get("explicit_env_keys", [])),
                sandbox=metadata,
            )
        finally:
            if script_path:
                try:
                    Path(script_path).unlink(missing_ok=True)
                except Exception:
                    pass
