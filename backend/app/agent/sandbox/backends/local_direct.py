from __future__ import annotations

from pathlib import Path

from app.agent.sandbox.models import (
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxRunMetadata,
)
from app.agent.sandbox.process import (
    _run_local_process,
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
            return _run_local_process(
                request,
                metadata,
                shell_command(request.shell, script_path),
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
