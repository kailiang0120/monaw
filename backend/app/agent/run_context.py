"""Per-turn runtime context shared with tool executions."""

from __future__ import annotations

from contextvars import ContextVar, Token

_CURRENT_CONVERSATION_ID: ContextVar[str] = ContextVar(
    "agent_current_conversation_id",
    default="",
)


def current_conversation_id() -> str:
    return _CURRENT_CONVERSATION_ID.get()


def set_current_conversation_id(conversation_id: str) -> Token[str]:
    return _CURRENT_CONVERSATION_ID.set(conversation_id or "")


def reset_current_conversation_id(token: Token[str]) -> None:
    _CURRENT_CONVERSATION_ID.reset(token)
