"""Best-effort tracing shared by the turn's model and tool boundaries."""

from __future__ import annotations

import logging
import time

from app.agent.observability.ports import ObservabilityPort
from app.agent.observability.recorder import infer_source
from app.agent.action_security.sensitive_data import public_tool_arguments, redact_sensitive

logger = logging.getLogger(__name__)


class TurnObserver:
    def __init__(self, port: ObservabilityPort, *, conversation_id: str, client) -> None:
        self.port = port
        self.conversation_id = conversation_id
        self.client = client
        self.run_id = ""

    def event(self, event_type: str, **kwargs) -> None:
        if not self.run_id:
            return
        try:
            self.port.log_event(
                run_id=self.run_id, conversation_id=self.conversation_id, event_type=event_type,
                source=infer_source(self.conversation_id),
                model=str(getattr(self.client, "model_name", "") or ""),
                provider=str(getattr(self.client, "provider", "") or ""), **kwargs,
            )
        except Exception:
            logger.debug("observability event failed event_type=%s", event_type, exc_info=True)

    def error(self, message: str, *, error_type: str = "", metadata: dict | None = None) -> None:
        try:
            self.port.log_error(
                run_id=self.run_id, conversation_id=self.conversation_id, message=message,
                error_type=error_type, metadata=metadata or {},
            )
        except Exception:
            logger.debug("observability error capture failed", exc_info=True)

    def tool_started(self, name: str, arguments: dict, call_id: str, risk: str = "") -> float:
        started = time.perf_counter()
        self.event("tool_call_started", tool_name=name, input={"arguments": public_tool_arguments(name, arguments), "call_id": call_id, "risk": risk})
        return started

    def tool_finished(
        self, name: str, output: str, status: str, call_id: str, started: float,
        *, risk: str = "", error_code: str = "", metadata: dict | None = None, duration_ms: int | None = None,
    ) -> None:
        self.event(
            "tool_call_finished", tool_name=name, status=status, error_code=error_code,
            error_message=redact_sensitive(output) if status not in {"ok", "complete", "success"} else "",
            duration_ms=duration_ms if duration_ms is not None else round((time.perf_counter() - started) * 1000),
            output={"output": redact_sensitive(output), "call_id": call_id, "risk": risk}, metadata=metadata or {},
        )
