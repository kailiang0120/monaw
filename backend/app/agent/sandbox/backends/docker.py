from __future__ import annotations

import re
import shutil
import subprocess
import time
import uuid
import os
from pathlib import Path

from app.agent.sandbox.process import _format_output, _run_command_capped
from app.agent.sandbox.artifacts import write_artifact as _write_artifact
from app.agent.sandbox.models import SandboxExecutionRequest, SandboxExecutionResult, SandboxRunMetadata
from app.agent.sandbox.path_policy import (
    SandboxPathPolicy,
    DEFAULT_MAX_COPY_OUT_BYTES,
    canonical_path,
    collect_copy_out,
    create_run_workspace,
    ensure_allowed_path,
    write_artifact_manifest,
)
from app.agent.runtime_paths import WORKSPACE_DIR
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

    def _workspace_mount(
        self,
        request: SandboxExecutionRequest,
        *,
        command_id: str,
    ) -> tuple[Path | None, SandboxPathPolicy | None, bool]:
        strategy = str(
            request.copy_policy.get("write_strategy")
            or request.env_metadata.get("write_strategy")
            or self.settings.default_write_strategy
        )
        workdir = canonical_path(request.workdir or WORKSPACE_DIR)
        allowed_roots = self.settings.allowed_bind_roots or [str(WORKSPACE_DIR)]
        blocked_roots = self.settings.blocked_bind_roots
        allowed = [canonical_path(root) for root in allowed_roots]
        if any(workdir == root or workdir.is_relative_to(root) for root in [canonical_path(root) for root in blocked_roots]):
            raise PermissionError(f"Sandbox bind path is blocked: {workdir}")
        ensure_allowed_path(workdir, allowed, purpose="bind")
        if strategy == "discard":
            return None, None, False
        if strategy == "direct_rw":
            return workdir, None, False

        run_workspace = create_run_workspace(command_id)
        initial_files: dict[str, tuple[int, int]] = {}
        total_bytes = 0
        max_copy_in_bytes = int(
            (request.resources or {}).get("max_copy_in_bytes")
            or self.settings.resources.max_copy_in_bytes
            or 104857600
        )
        try:
            for root, directories, filenames in os.walk(workdir, followlinks=False):
                root_path = Path(root)
                directories[:] = [
                    name for name in directories if not (root_path / name).is_symlink()
                ]
                for filename in filenames:
                    source = root_path / filename
                    if source.is_symlink() or not source.is_file():
                        continue
                    stat = source.stat()
                    total_bytes += stat.st_size
                    if total_bytes > max_copy_in_bytes:
                        raise PermissionError(
                            f"Sandbox copy-in size limit exceeded ({max_copy_in_bytes} bytes)"
                        )
                    relative = source.relative_to(workdir)
                    target = run_workspace / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    initial_files[str(relative).replace(os.sep, "/")] = (
                        stat.st_size,
                        stat.st_mtime_ns,
                    )
            policy = SandboxPathPolicy.from_strings(
                allowed_input_roots=[str(workdir)],
                allowed_output_roots=[str(workdir)],
                max_copy_out_bytes=int(
                    (request.resources or {}).get("max_copy_out_bytes")
                    or getattr(self.settings.resources, "max_copy_out_bytes", 0)
                    or DEFAULT_MAX_COPY_OUT_BYTES
                ),
                max_copy_in_bytes=max_copy_in_bytes,
                initial_files=initial_files,
            )
            # The staging workspace is the command's writable copy. The
            # input/output marker directories are implementation details and
            # must not be copied back to the user's workdir.
            shutil.rmtree(run_workspace / "input", ignore_errors=True)
            shutil.rmtree(run_workspace / "output", ignore_errors=True)
            return run_workspace, policy, True
        except Exception:
            shutil.rmtree(run_workspace, ignore_errors=True)
            raise

    def docker_command(
        self,
        request: SandboxExecutionRequest,
        *,
        container_name: str = "",
        mount_source: Path | None = None,
        mount_read_only: bool = False,
    ) -> list[str]:
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
        if mount_source is not None:
            mount = f"type=bind,source={mount_source},target=/workspace"
            if mount_read_only:
                mount += ",readonly"
            command.extend(["--mount", mount])
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
            enabled=bool(request.env_metadata.get("enabled", True)),
            trust_class=str(request.env_metadata.get("trust_class", "trusted")),
            required_isolation=str(request.env_metadata.get("required_isolation", "strong")),
            network_enforcement=str(request.env_metadata.get("network_enforcement", "enforced")),
            filesystem_policy=str(request.env_metadata.get("filesystem_policy", "container")),
            write_strategy=str(request.env_metadata.get("write_strategy", "discard")),
            reason=str(request.env_metadata.get("reason", "")),
            reason_code=str(request.env_metadata.get("reason_code", "allowed")),
        ).model_dump()
        metadata["copy_policy"] = {
            "strategy": metadata["write_strategy"],
            "container_changes_ephemeral": metadata["write_strategy"] != "direct_rw",
            "changed_or_new_files_only": metadata["write_strategy"] == "copy_out",
            "deletions_propagated": False,
            "symlinks_copied": False,
        }
        if metadata["write_strategy"] == "discard":
            metadata["warnings"].append(
                "Container filesystem changes are ephemeral; changes outside /workspace are discarded."
            )
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
        mount_source: Path | None = None
        copy_policy: SandboxPathPolicy | None = None
        copied_workspace = False
        try:
            mount_source, copy_policy, copied_workspace = self._workspace_mount(request, command_id=command_id)
            command = self.docker_command(
                request,
                container_name=container_name,
                mount_source=mount_source,
                mount_read_only=bool(request.copy_policy.get("mount_read_only", False)),
            )
            completed = _run_command_capped(
                command,
                request,
                on_timeout=lambda _proc: self._remove_container(container_name),
                on_cancel=lambda _proc: self._remove_container(container_name),
                use_request_cwd=False,
                use_request_env=False,
            )
            cancelled = bool(completed.cancelled)
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
            copied_files: list[dict] = []
            metadata["artifacts"] = {
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "manifest_path": "",
                "copy_out": [],
            }
            if copied_workspace and mount_source is not None and copy_policy is not None:
                copied_files = collect_copy_out(
                    mount_source,
                    request.workdir or str(WORKSPACE_DIR),
                    copy_policy,
                )
                metadata["artifacts"]["copy_out"] = copied_files
                if not copied_files:
                    metadata["warnings"].append(
                        "No changed or new files under /workspace were copied back; container-only changes are ephemeral."
                    )
            manifest_path = write_artifact_manifest(
                command_id,
                {
                    "stdout_path": stdout_path,
                    "stderr_path": stderr_path,
                    "copy_out": copied_files,
                },
            )
            metadata["artifacts"]["manifest_path"] = manifest_path
            return SandboxExecutionResult(
                status="error" if completed.timed_out or completed.returncode != 0 else "ok",
                exit_code=completed.returncode,
                duration_ms=duration_ms,
                timed_out=completed.timed_out,
                cancelled=cancelled,
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
                error=(
                    "Command cancelled."
                    if cancelled
                    else "Command timed out." if completed.timed_out else ""
                ),
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
        finally:
            if copied_workspace and mount_source is not None:
                shutil.rmtree(mount_source, ignore_errors=True)
