"""Typed state machine for a single agent turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import perf_counter


class AgentRunState(str, Enum):
    INITIALIZING = "initializing"
    MODEL_CALL = "model_call"
    TOOL_EXECUTION = "tool_execution"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_ACCESS = "waiting_access"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = {
    AgentRunState.COMPLETED,
    AgentRunState.FAILED,
    AgentRunState.CANCELLED,
}


@dataclass(frozen=True)
class AgentRunTransition:
    from_state: AgentRunState
    to_state: AgentRunState
    reason: str = ""
    at_seconds: float = 0.0


@dataclass
class AgentRunStateMachine:
    state: AgentRunState = AgentRunState.INITIALIZING
    started_at: float = field(default_factory=perf_counter)
    transitions: list[AgentRunTransition] = field(default_factory=list)

    def transition(self, next_state: AgentRunState | str, *, reason: str = "") -> AgentRunTransition:
        state = next_state if isinstance(next_state, AgentRunState) else AgentRunState(str(next_state))
        if self.state in TERMINAL_STATES:
            return self.transitions[-1] if self.transitions else AgentRunTransition(self.state, self.state, reason, 0.0)
        transition = AgentRunTransition(
            from_state=self.state,
            to_state=state,
            reason=reason,
            at_seconds=max(0.0, perf_counter() - self.started_at),
        )
        self.transitions.append(transition)
        self.state = state
        return transition

    def model_call(self) -> AgentRunTransition:
        return self.transition(AgentRunState.MODEL_CALL)

    def tool_execution(self) -> AgentRunTransition:
        return self.transition(AgentRunState.TOOL_EXECUTION)

    def waiting_for(self, event_kind: str) -> AgentRunTransition:
        if event_kind == "access_grant_required":
            return self.transition(AgentRunState.WAITING_ACCESS, reason=event_kind)
        return self.transition(AgentRunState.WAITING_APPROVAL, reason=event_kind)

    def finalizing(self) -> AgentRunTransition:
        return self.transition(AgentRunState.FINALIZING)

    def complete(self) -> AgentRunTransition:
        return self.transition(AgentRunState.COMPLETED)

    def fail(self, reason: str = "") -> AgentRunTransition:
        return self.transition(AgentRunState.FAILED, reason=reason)

    def cancel(self, reason: str = "") -> AgentRunTransition:
        return self.transition(AgentRunState.CANCELLED, reason=reason)
