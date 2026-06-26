from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid

from app.agent.sandbox.backends.local_direct import _format_output, _run_command_capped, _write_artifact
from app.agent.sandbox.models import SandboxExecutionRequest, SandboxExecutionResult, SandboxRunMetadata
from app.agent.sandbox.path_policy import write_artifact_manifest
from app.agent.settings_store import SandboxSettings


_PINNED_IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-fA-F]{64}$")


def is_pinned_docker_image(image: str) -> bool:
    return bool(_PINNED_IMAGE_RE.fullmatch(str(image or "").strip()))


class DockerRunner:
    backend_name = "docker"
    security_label = "strong"
    supported_shells = {"bash"}

    def __init__(self, settings: SandboxSettings) -> None:
        self.settings = settings

    def is_available(self) -> bool:
        return shutil.which("docker") is not None

    def docker_command(self, request: SandboxExecutionRequest, *, container_name: str = "") -> list[str]:
        resources = {**self.settings.resources.model_dump(), **(request.resources or {})}
        image = self.settings.docker.image
        network = "none" if request.network == "deny" else "bridge"
        command = [
            "docker",
            "run",
            "--rm",
            "--pull",
            self.settings.docker.pull_policy,
            "--network",
            network,
            "--cap-drop",
            "ALL",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--memory",
            f"{int(resources.get('memory_mb') or 1024)}m",
            "--cpus",
            str(resources.get("cpus") or 1.0),
            "--pids-limit",
            str(int(resources.get("pids") or 128)),
            "--workdir",
            "/workspace",
        ]
        if self.settings.docker.no_new_privileges:
            command.extend(["--security-opt", "no-new-privileges"])
        if self.settings.docker.read_only_root:
            command.append("--read-only")
        if container_name:
            command.extend(["--name", container_name])
        for key in sorted(request.env.keys(), key=str.upper):
            command.extend(["--env", f"{key}={request.env[key]}"])
        command.extend([image, "bash", "-lc", request.command])
        return command

    def _remove_container(self, container_name: str) -> None:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            check=False,
        )

    def run(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        started_at = time.monotonic()
        if not is_pinned_docker_image(self.settings.docker.image):
            return SandboxExecutionResult(
                status="blocked",
                reason="Docker sandbox images must be pinned with an @sha256 digest.",
                reason_code="docker_image_not_pinned",
                shell=request.shell,
                workdir=request.workdir,
                sandbox={
                    **request.env_metadata,
                    "backend": self.backend_name,
                    "selected_backend": self.backend_name,
                    "security_label": "none",
                    "reason_code": "docker_image_not_pinned",
                },
            )
        metadata = SandboxRunMetadata(
            backend=self.backend_name,
            selected_backend=self.backend_name,
            security_label=self.security_label,
            filesystem_isolation="container",
            process_isolation="container",
            process_cleanup="container_rm",
            network_isolation="enforced" if request.network == "deny" else "host_routed",
            env_inheritance=str(request.env_metadata.get("env_inheritance", "scrubbed")),
            profile=request.profile,
            mode=str(request.env_metadata.get("mode", self.settings.mode)),
            network=request.network,
            workdir="/workspace",
            inherited_env_keys=list(request.env_metadata.get("inherited_env_keys", [])),
            explicit_env_keys=list(request.env_metadata.get("explicit_env_keys", [])),
            blocked_env_keys=list(request.env_metadata.get("blocked_env_keys", [])),
            env_keys=list(request.env_metadata.get("env_keys", [])),
            warnings=list(request.env_metadata.get("warnings", [])),
        ).model_dump()
        if request.shell.lower() not in self.supported_shells:
            return SandboxExecutionResult(
                status="blocked",
                reason="Docker backend currently supports bash commands only.",
                reason_code="unsupported_shell_for_backend",
                shell=request.shell,
                shell_command="docker",
                workdir=request.workdir,
                env_keys=list(request.env_metadata.get("explicit_env_keys", [])),
                sandbox={
                    **metadata,
                    "reason": "Docker backend currently supports bash commands only.",
                    "reason_code": "unsupported_shell_for_backend",
                },
            )
        if not self.is_available():
            return SandboxExecutionResult(
                status="blocked",
                reason="Docker is not installed or not available on PATH.",
                reason_code="sandbox_backend_unavailable",
                shell=request.shell,
                workdir=request.workdir,
                env_keys=list(request.env_metadata.get("explicit_env_keys", [])),
                sandbox=metadata,
            )

        command_id = uuid.uuid4().hex[:12]
        container_name = f"monaw-sandbox-{command_id}"
        command = self.docker_command(request, container_name=container_name)
        try:
            completed = _run_command_capped(
                command,
                request,
                on_timeout=lambda _proc: self._remove_container(container_name),
                use_request_cwd=False,
                use_request_env=False,
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
            stdout_path = ""
            stderr_path = ""
            if request.save_output_to or len(raw_stdout) > len(stdout) or len(raw_stderr) > len(stderr):
                stdout_path = _write_artifact(command_id, "stdout", raw_stdout)
                stderr_path = _write_artifact(command_id, "stderr", raw_stderr)
            manifest_path = write_artifact_manifest(
                command_id,
                {"stdout_path": stdout_path, "stderr_path": stderr_path, "copy_out": []},
            )
            metadata["artifacts"] = {
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "manifest_path": manifest_path,
                "copy_out": [],
            }
            return SandboxExecutionResult(
                status="error" if completed.timed_out or completed.returncode != 0 else "ok",
                exit_code=completed.returncode,
                duration_ms=duration_ms,
                timed_out=completed.timed_out,
                command_id=command_id,
                stdout=stdout,
                stderr=stderr,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                shell=request.shell,
                shell_command="docker",
                workdir=request.workdir,
                env_keys=list(request.env_metadata.get("explicit_env_keys", [])),
                sandbox=metadata,
                error="Command timed out." if completed.timed_out else "",
            )
        except Exception as exc:
            return SandboxExecutionResult(
                status="error",
                error=str(exc),
                shell=request.shell,
                shell_command="docker",
                workdir=request.workdir,
                env_keys=list(request.env_metadata.get("explicit_env_keys", [])),
                sandbox=metadata,
            )
