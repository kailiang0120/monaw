from __future__ import annotations

from typing import Protocol

from app.agent.sandbox.models import SandboxExecutionRequest, SandboxExecutionResult


class SandboxRunner(Protocol):
    backend_name: str
    security_label: str

    def is_available(self) -> bool:
        ...

    def run(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        ...
