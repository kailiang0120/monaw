"""Per-turn runtime context shared with tool executions."""

from __future__ import annotations

from dataclasses import dataclass
from contextvars import ContextVar, Token


@dataclass(frozen=True, slots=True)
class ExecutionPrincipal:
    source: str = "desktop"
    principal_id: str = ""
    conversation_id: str = ""
    permission_profile_id: str = ""
    interactive: bool = True


_CURRENT_CONVERSATION_ID: ContextVar[str] = ContextVar(
    "agent_current_conversation_id",
    default="",
)
_CURRENT_CONTROL_SESSION_ID: ContextVar[str] = ContextVar(
    "agent_current_control_session_id",
    default="",
)
_CURRENT_EXECUTION_SOURCE: ContextVar[str] = ContextVar(
    "agent_current_execution_source",
    default="desktop",
)
_CURRENT_PRINCIPAL_ID: ContextVar[str] = ContextVar(
    "agent_current_principal_id",
    default="",
)
_CURRENT_PERMISSION_PROFILE_ID: ContextVar[str] = ContextVar(
    "agent_current_permission_profile_id",
    default="",
)
_CURRENT_INTERACTIVE: ContextVar[bool] = ContextVar(
    "agent_current_interactive",
    default=True,
)


def current_conversation_id() -> str:
    return _CURRENT_CONVERSATION_ID.get()


def set_current_conversation_id(conversation_id: str) -> Token[str]:
    return _CURRENT_CONVERSATION_ID.set(conversation_id or "")


def reset_current_conversation_id(token: Token[str]) -> None:
    _CURRENT_CONVERSATION_ID.reset(token)


def current_control_session_id() -> str:
    return _CURRENT_CONTROL_SESSION_ID.get()


def set_current_control_session_id(session_id: str) -> Token[str]:
    return _CURRENT_CONTROL_SESSION_ID.set(session_id or "")


def reset_current_control_session_id(token: Token[str]) -> None:
    _CURRENT_CONTROL_SESSION_ID.reset(token)


def current_execution_source() -> str:
    return _CURRENT_EXECUTION_SOURCE.get()


def set_current_execution_source(source: str) -> Token[str]:
    return _CURRENT_EXECUTION_SOURCE.set(source or "desktop")


def reset_current_execution_source(token: Token[str]) -> None:
    _CURRENT_EXECUTION_SOURCE.reset(token)


def current_principal_id() -> str:
    return _CURRENT_PRINCIPAL_ID.get()


def set_current_principal_id(principal_id: str) -> Token[str]:
    return _CURRENT_PRINCIPAL_ID.set(principal_id or "")


def reset_current_principal_id(token: Token[str]) -> None:
    _CURRENT_PRINCIPAL_ID.reset(token)


def current_permission_profile_id() -> str:
    return _CURRENT_PERMISSION_PROFILE_ID.get()


def set_current_permission_profile_id(profile_id: str) -> Token[str]:
    return _CURRENT_PERMISSION_PROFILE_ID.set(profile_id or "")


def reset_current_permission_profile_id(token: Token[str]) -> None:
    _CURRENT_PERMISSION_PROFILE_ID.reset(token)


def current_interactive() -> bool:
    return _CURRENT_INTERACTIVE.get()


def set_current_interactive(interactive: bool) -> Token[bool]:
    return _CURRENT_INTERACTIVE.set(bool(interactive))


def reset_current_interactive(token: Token[bool]) -> None:
    _CURRENT_INTERACTIVE.reset(token)


def current_execution_principal() -> ExecutionPrincipal:
    return ExecutionPrincipal(
        source=current_execution_source(),
        principal_id=current_principal_id(),
        conversation_id=current_conversation_id(),
        permission_profile_id=current_permission_profile_id(),
        interactive=current_interactive(),
    )
