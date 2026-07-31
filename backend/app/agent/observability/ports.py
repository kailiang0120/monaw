"""Typed observability interfaces used by agent orchestration."""

from __future__ import annotations

from typing import Any, Protocol

from app.agent.observability.recorder import UsageStats


class ObservabilityPort(Protocol):
    def start_run(
        self,
        *,
        conversation_id: str,
        message_id: str = "",
        user_message: str = "",
        source: str = "",
        model: str = "",
        provider: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        ...

    def log_event(
        self,
        *,
        event_type: str,
        run_id: str = "",
        conversation_id: str = "",
        message_id: str = "",
        level: str = "info",
        status: str = "",
        source: str = "",
        model: str = "",
        provider: str = "",
        tool_name: str = "",
        error_code: str = "",
        error_message: str = "",
        duration_ms: int | float = 0,
        input: Any = None,
        output: Any = None,
        tokens: UsageStats | dict | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        ...

    def log_error(
        self,
        *,
        message: str,
        run_id: str = "",
        conversation_id: str = "",
        message_id: str = "",
        logger_name: str = "",
        module: str = "",
        error_type: str = "",
        traceback_text: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        ...

    def finish_run(
        self,
        *,
        run_id: str,
        status: str,
        final_output: str = "",
        failure_reason: str = "",
        duration_ms: int | None = None,
        usage: UsageStats | dict | None = None,
        tool_count: int = 0,
        tool_error_count: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        ...

    def finish_open_run_for_conversation(
        self,
        *,
        conversation_id: str,
        status: str,
        failure_reason: str = "",
        final_output: str = "",
    ) -> None:
        ...
