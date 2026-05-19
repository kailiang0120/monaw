"""Memory manager for orchestration engine conversation state.

Manages per-conversation persistent state including recent messages, rolling summaries,
active tasks, and tool outcomes. Provides context strings for LLM prompts.

Persistence: SQLite database in .runtime/agent.db (replaces JSON files).
Token management: counts context size against the selected model window and
auto-compacts before the provider hard limit.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.agent.long_term_memory import LongTermMemory
from app.agent.context_usage import (
    DEFAULT_CONTEXT_TOKEN_LIMIT,
    build_context_usage_report,
    count_text_tokens,
    model_compaction_threshold,
    model_context_token_limit,
)
from app.agent.state import ConversationState, TaskState
from app.agent.llm_client import LLMClient
from app.agent.context_compression import compress_context
from app.agent.database import get_db
from app.agent.runtime_paths import RUNTIME_DIR

_RUNTIME_DIR = RUNTIME_DIR
_CONVERSATIONS_DIR = _RUNTIME_DIR / "conversations"
logger = logging.getLogger(__name__)

# ── Context-window budget ─────────────────────────────────────────────────────
CONTEXT_TOKEN_LIMIT = DEFAULT_CONTEXT_TOKEN_LIMIT
COMPACTION_THRESHOLD = int(DEFAULT_CONTEXT_TOKEN_LIMIT * 0.9)
CHARS_PER_TOKEN = 4


# ── Module-level helpers (no manager instance required) ───────────────────────

def list_conversation_metadata() -> list[dict]:
    """Return metadata for all persisted conversations, newest-updated first."""
    return get_db().list_conversations()


def create_conversation_file(conversation_id: str, title: str) -> dict:
    """Create a new conversation and return its metadata dict."""
    result = get_db().create_conversation(conversation_id, title)
    manager = peek_memory_manager()
    if manager is not None:
        manager._ensure_state(conversation_id, title)
    return result


def delete_conversation_file(conversation_id: str) -> bool:
    """Delete a conversation. Returns True if it existed."""
    return get_db().delete_conversation(conversation_id)


# ── MemoryManager ─────────────────────────────────────────────────────────────


class MemoryManager:
    """Manages conversation memory with SQLite persistence, summarization, and auto-compaction."""

    MAX_RECENT_MESSAGES = 10
    LLM_HISTORY_MESSAGES = 24
    MAX_TOOL_OUTCOMES = 5
    RECENT_MESSAGES_IN_CONTEXT = 5

    @staticmethod
    def _normalize_role(role: str) -> str:
        if role in {"assistant", "model"}:
            return "assistant"
        return "user"

    def __init__(self, llm_client: LLMClient) -> None:
        self._active_states: dict[str, ConversationState] = {}
        self.llm_client = llm_client
        self._db = get_db()
        self.long_term_memory: LongTermMemory | None = None

    def _ensure_state(self, conversation_id: str, title: str = "") -> ConversationState:
        """Load or create a ConversationState, populating from SQLite."""
        if conversation_id in self._active_states:
            state = self._active_states[conversation_id]
            if title and not state.title:
                state.title = title
                self._db.update_conversation(conversation_id, title=title)
            return state

        conv = self._db.get_conversation(conversation_id)
        if conv is None:
            self._db.create_conversation(conversation_id, title or "Untitled")
            conv = self._db.get_conversation(conversation_id)

        recent = self._db.get_recent_messages(conversation_id, self.MAX_RECENT_MESSAGES)
        all_msgs = self._db.get_messages(conversation_id, limit=200)
        tool_outcomes = self._db.get_recent_tool_outcomes(
            conversation_id, self.MAX_TOOL_OUTCOMES
        )
        pending_steps = self._db.get_pending_steps(conversation_id)
        completed_steps = self._db.get_completed_steps(conversation_id)

        state = ConversationState(
            conversation_id=conversation_id,
            title=conv.get("title", title or "Untitled") if conv else (title or "Untitled"),
            recent_messages=[
                {"role": m["role"], "content": m["content"], "timestamp": m["created_at"]}
                for m in recent
            ],
            all_messages=[
                {"role": m["role"], "content": m["content"], "timestamp": m["created_at"]}
                for m in all_msgs
            ],
            summary=conv.get("summary", "") if conv else "",
            tool_outcomes=tool_outcomes,
            task_goal=conv.get("task_goal", "") if conv else "",
            pending_steps=[
                {"step_id": s["step_id"], "description": s["description"], "status": s["status"]}
                for s in pending_steps
            ],
            completed_steps=completed_steps,
            context_tokens_estimate=conv.get("context_tokens_estimate", 0) if conv else 0,
            created_at=datetime.fromisoformat(conv["created_at"]) if conv else datetime.now(timezone.utc),
            updated_at=datetime.fromisoformat(conv["updated_at"]) if conv else datetime.now(timezone.utc),
        )

        if title and not state.title:
            state.title = title
            self._db.update_conversation(conversation_id, title=title)

        self._active_states[conversation_id] = state
        return state

    def _build_context_sections(self, state: ConversationState) -> list[str]:
        pending_steps = state.pending_steps or []
        recent_messages = (state.all_messages or state.recent_messages)[
            -self.RECENT_MESSAGES_IN_CONTEXT:
        ]

        parts = [
            "## Active Goal",
            state.task_goal.strip() or "None.",
            "",
            "## Remaining Steps",
        ]

        if pending_steps:
            for step in pending_steps:
                step_id = step.get("step_id", "step")
                description = step.get("description", "Unnamed step")
                status = step.get("status", "pending")
                parts.append(f"- {step_id}: {description} ({status})")
        else:
            parts.append("None.")

        parts.extend(["", "## What Happened So Far (Summary)"])
        if state.summary.strip():
            parts.append(state.summary.strip())
        elif state.completed_steps:
            parts.append("\n".join(f"- {step}" for step in state.completed_steps[-5:]))
        else:
            parts.append("No summary yet.")

        parts.extend(["", f"## Recent Messages (last {self.RECENT_MESSAGES_IN_CONTEXT})"])
        if recent_messages:
            for msg in recent_messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                parts.append(f"- {role}: {content}")
        else:
            parts.append("None.")

        parts.extend(["", "## Recent Tool Results"])
        if state.tool_outcomes:
            for outcome in state.tool_outcomes[-self.MAX_TOOL_OUTCOMES:]:
                tool_name = outcome.get("tool", "unknown")
                result = outcome.get("result", "")
                if len(result) > 200:
                    result = result[:200] + "..."
                parts.append(f"- {tool_name}: {result}")
        else:
            parts.append("None.")

        return parts

    # ── Public API ────────────────────────────────────────────────────────

    def get_or_create(self, conversation_id: str, title: str = "") -> ConversationState:
        """Get existing conversation state or create a new one."""
        return self._ensure_state(conversation_id, title)

    def get_context(self, conversation_id: str) -> str:
        """Build memory context string to inject into prompts."""
        state = self.get_or_create(conversation_id)
        return "\n".join(self._build_context_sections(state)).strip()

    def set_long_term_memory(self, memory: LongTermMemory | None) -> None:
        self.long_term_memory = memory

    def build_long_term_memory_context(self, query: str) -> str:
        if self.long_term_memory is None:
            return ""
        try:
            return self.long_term_memory.build_prompt(query)
        except Exception as exc:
            logger.warning("Long-term memory prompt unavailable: %s", exc)
            return ""

    def schedule_long_term_learning(
        self,
        *,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
    ) -> None:
        # Durable memory is curated at session close. Per-turn learning is intentionally disabled.
        return

    def close_session(self, conversation_id: str) -> dict:
        from app.agent.memory_consolidation import close_session

        return close_session(
            conversation_id,
            store=self.long_term_memory,
            db=self._db,
        )

    @staticmethod
    def _log_learning_error(task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Long-term memory learning failed: %s", exc)

    def build_llm_messages(self, conversation_id: str) -> list[dict]:
        """Build conversation history messages for cross-turn LLM continuity."""
        state = self.get_or_create(conversation_id)
        persisted_history = self._db.get_messages(conversation_id, limit=200)
        if persisted_history:
            state.all_messages = [
                {"role": m["role"], "content": m["content"], "timestamp": m["created_at"]}
                for m in persisted_history
            ]
            state.recent_messages = state.all_messages[-self.MAX_RECENT_MESSAGES:]
        history = state.all_messages[-self.LLM_HISTORY_MESSAGES:] or state.recent_messages[
            -self.LLM_HISTORY_MESSAGES:
        ]
        messages: list[dict] = []

        if state.summary.strip():
            messages.append({
                "role": "assistant",
                "content": f"Earlier conversation summary:\n{state.summary.strip()}",
            })

        if state.task_goal or state.pending_steps:
            messages.append({
                "role": "assistant",
                "content": self.get_context(conversation_id),
            })

        for msg in history:
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            messages.append({
                "role": self._normalize_role(msg.get("role", "user")),
                "content": content,
            })

        return messages

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        status: str = "complete",
    ) -> None:
        """Add a message to conversation state and persist to SQLite."""
        state = self.get_or_create(conversation_id)
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        state.recent_messages.append(msg)
        state.all_messages.append(msg)

        self._db.add_message(conversation_id, role, content, status=status)

        state.context_tokens_estimate = self.estimate_context_tokens(conversation_id)
        state.updated_at = datetime.now(timezone.utc)
        self._db.update_conversation(
            conversation_id,
            context_tokens_estimate=state.context_tokens_estimate,
        )

    def _add_message_record(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        thinking: str = "",
        status: str = "complete",
        response_duration_ms: int | None = None,
        response_attachments: list[dict] | None = None,
    ) -> int:
        state = self.get_or_create(conversation_id)
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        state.recent_messages.append(msg)
        state.all_messages.append(msg)
        message_id = self._db.add_message(
            conversation_id,
            role,
            content,
            thinking=thinking,
            status=status,
            response_duration_ms=response_duration_ms,
            attachments_json=(
                json.dumps(response_attachments, ensure_ascii=False)
                if response_attachments
                else ""
            ),
        )
        state.context_tokens_estimate = self.estimate_context_tokens(conversation_id)
        state.updated_at = datetime.now(timezone.utc)
        self._db.update_conversation(
            conversation_id,
            context_tokens_estimate=state.context_tokens_estimate,
        )
        return message_id

    async def add_message_and_maybe_summarize(
        self, conversation_id: str, role: str, content: str
    ) -> None:
        """Add message; if recent_messages exceeds MAX_RECENT_MESSAGES, trigger summarization."""
        self.add_message(conversation_id, role, content)
        state = self.get_or_create(conversation_id)
        if len(state.recent_messages) > self.MAX_RECENT_MESSAGES:
            await self._summarize(conversation_id)

    async def persist_turn(
        self,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        *,
        tool_calls: list[dict],
        thinking: str = "",
        status: str = "complete",
        response_duration_ms: int | None = None,
        response_attachments: list[dict] | None = None,
    ) -> None:
        self._add_message_record(conversation_id, "user", user_message)
        assistant_message_id = self._add_message_record(
            conversation_id,
            "assistant",
            assistant_message,
            thinking=thinking,
            status=status,
            response_duration_ms=response_duration_ms,
            response_attachments=response_attachments,
        )
        for tool_call in tool_calls:
            self._db.add_tool_call(
                message_id=assistant_message_id,
                conv_id=conversation_id,
                tool_name=str(tool_call.get("tool_name", "")),
                tool_input=str(tool_call.get("input", "")),
                tool_output=str(tool_call.get("output", "")),
                status=str(tool_call.get("status", "complete")),
            )

        state = self.get_or_create(conversation_id)
        if len(state.recent_messages) > self.MAX_RECENT_MESSAGES:
            await self._summarize(conversation_id)

    def add_tool_outcome(self, conversation_id: str, tool_name: str, result: str) -> None:
        """Record a tool call result for context and persist."""
        state = self.get_or_create(conversation_id)
        outcome = {
            "tool": tool_name,
            "result": result[:200] if result else "",
        }
        state.tool_outcomes.append(outcome)
        if len(state.tool_outcomes) > self.MAX_TOOL_OUTCOMES:
            state.tool_outcomes = state.tool_outcomes[-self.MAX_TOOL_OUTCOMES:]

        self._db.add_tool_outcome(conversation_id, tool_name, result)

        state.context_tokens_estimate = self.estimate_context_tokens(conversation_id)
        state.updated_at = datetime.now(timezone.utc)

    def set_task_goal(self, conversation_id: str, goal: str) -> None:
        """Persist the active task goal for resumable multi-step tasks."""
        state = self.get_or_create(conversation_id)
        state.task_goal = goal.strip()
        state.updated_at = datetime.now(timezone.utc)
        self._db.update_conversation(conversation_id, task_goal=state.task_goal)

    def sync_plan_progress(self, conversation_id: str, plan) -> None:
        """Persist current pending/completed plan progress."""
        state = self.get_or_create(conversation_id)
        pending = [
            step.model_dump()
            for step in plan.steps
            if step.status in {"pending", "active", "retrying"}
        ]
        completed = [
            step.description
            for step in plan.steps
            if step.status == "done"
        ]
        state.pending_steps = pending
        state.completed_steps = completed
        state.updated_at = datetime.now(timezone.utc)
        self._db.sync_plan_steps(conversation_id, pending, completed)

    def clear_task_progress(self, conversation_id: str) -> None:
        """Clear the persisted active goal and remaining steps after completion."""
        state = self.get_or_create(conversation_id)
        state.task_goal = ""
        state.pending_steps = []
        state.completed_steps = []
        state.updated_at = datetime.now(timezone.utc)
        self._db.update_conversation(conversation_id, task_goal="")
        self._db.clear_plan_steps(conversation_id)

    def set_active_task(self, conversation_id: str, task: TaskState | None) -> None:
        """Set or clear the active task (transient — not persisted)."""
        state = self.get_or_create(conversation_id)
        state.active_task = task
        state.updated_at = datetime.now(timezone.utc)

    def delete(self, conversation_id: str) -> bool:
        """Delete conversation from memory and database."""
        found_memory = conversation_id in self._active_states
        if found_memory:
            del self._active_states[conversation_id]
        found_db = self._db.delete_conversation(conversation_id)
        return found_memory or found_db

    def list_conversations(self) -> list[str]:
        """Return all conversation IDs."""
        rows = self._db.list_conversations()
        return [r["id"] for r in rows]

    def list_conversations_metadata(self) -> list[dict]:
        """Return conversation metadata for all conversations, newest first."""
        return self._db.list_conversations()

    def get_messages_for_conversation(
        self, conv_id: str, limit: int = 50, before_id: int | None = None
    ) -> tuple[list[dict], bool]:
        """Fetch messages for frontend display with cursor pagination.

        Returns (messages, has_more).
        """
        messages = self._db.get_messages(conv_id, limit=limit + 1, before_id=before_id)
        has_more = len(messages) > limit
        if has_more:
            messages = messages[1:]
        return messages, has_more

    # ── Token estimation & auto-compaction ────────────────────────────────

    def estimate_tokens(self, text: str) -> int:
        return count_text_tokens(text, llm_client=self.llm_client)

    def estimate_context_tokens(
        self,
        conversation_id: str,
        system_prompt: str = "",
        *,
        tools: list[dict] | None = None,
        current_user_message: str = "",
    ) -> int:
        limit = model_context_token_limit(self.llm_client)
        report = build_context_usage_report(
            memory=self,
            llm_client=self.llm_client,
            conversation_id=conversation_id,
            runtime_prompt_text=system_prompt,
            visible_tools=tools or [],
            hidden_tools=[],
            current_user_message=current_user_message,
            limit=limit,
            compaction_at=model_compaction_threshold(self.llm_client),
        )
        return int(report["used"])

    def estimate_and_report(
        self,
        conversation_id: str,
        system_prompt: str = "",
        *,
        tools: list[dict] | None = None,
        current_user_message: str = "",
    ) -> dict[str, int | float]:
        used = self.estimate_context_tokens(
            conversation_id,
            system_prompt,
            tools=tools,
            current_user_message=current_user_message,
        )
        limit = model_context_token_limit(self.llm_client)
        compaction_at = model_compaction_threshold(self.llm_client)
        percentage = round((used / limit) * 100, 1) if limit else 0.0
        return {
            "used": used,
            "limit": limit,
            "compaction_at": compaction_at,
            "percentage": percentage,
        }

    async def ensure_context_fits(
        self,
        conversation_id: str,
        system_prompt: str = "",
        *,
        tools: list[dict] | None = None,
        current_user_message: str = "",
    ) -> None:
        while (
            self.estimate_context_tokens(
                conversation_id,
                system_prompt,
                tools=tools,
                current_user_message=current_user_message,
            )
            > model_compaction_threshold(self.llm_client)
        ):
            compacted = await self._compact(conversation_id)
            if not compacted:
                break

    async def _compact(self, conversation_id: str) -> bool:
        state = self._active_states.get(conversation_id)
        if state is None or not state.recent_messages:
            return False

        focus_topics: list[str] = []
        if state.task_goal.strip():
            focus_topics.append(state.task_goal.strip())

        async def _llm_summarize(msgs, sys_prompt):
            return await self.llm_client.chat(messages=msgs, system_prompt=sys_prompt)

        result = await compress_context(
            messages=state.recent_messages,
            existing_summary=state.summary,
            focus_topics=focus_topics or None,
            llm_chat_fn=_llm_summarize,
        )

        state.summary = result.summary
        state.recent_messages = result.messages
        state.context_tokens_estimate = self.estimate_context_tokens(conversation_id, "")
        state.updated_at = datetime.now(timezone.utc)
        self._db.update_conversation(
            conversation_id,
            summary=state.summary,
            context_tokens_estimate=state.context_tokens_estimate,
        )
        return len(result.phases_applied) > 0

    async def _summarize(self, conversation_id: str) -> None:
        """Summarize older messages into the summary field."""
        state = self.get_or_create(conversation_id)
        if len(state.recent_messages) <= 5:
            return

        messages_to_summarize = state.recent_messages[:-5]
        messages_to_keep = state.recent_messages[-5:]

        messages_text = "\n".join(
            f"{m.get('role', 'unknown')}: {m.get('content', '')}"
            for m in messages_to_summarize
        )

        prompt = f"Summarize this conversation history concisely (2-3 sentences):\n{messages_text}"
        try:
            new_summary = await self.llm_client.chat(
                messages=[{"role": "user", "content": prompt}],
                system_prompt="You are a helpful assistant that summarizes conversations.",
            )
            if state.summary.strip():
                state.summary = f"{new_summary}\n\n{state.summary}"
            else:
                state.summary = new_summary
        except Exception:
            pass

        state.recent_messages = messages_to_keep
        state.context_tokens_estimate = self.estimate_context_tokens(conversation_id, "")
        state.updated_at = datetime.now(timezone.utc)
        self._db.update_conversation(
            conversation_id,
            summary=state.summary,
            context_tokens_estimate=state.context_tokens_estimate,
        )


# ── Singleton management ──────────────────────────────────────────────────────

_default_memory_manager: MemoryManager | None = None


def get_memory_manager(llm_client: LLMClient | None = None) -> MemoryManager:
    """Get or create the shared memory manager."""
    global _default_memory_manager
    if _default_memory_manager is None:
        if llm_client is None:
            raise ValueError("llm_client required to initialize memory manager")
        _default_memory_manager = MemoryManager(llm_client)
    elif llm_client is not None:
        _default_memory_manager.llm_client = llm_client
    return _default_memory_manager


def peek_memory_manager() -> MemoryManager | None:
    """Return the shared memory manager if it has already been created."""
    return _default_memory_manager


def get_memory(llm_client: LLMClient | None = None) -> MemoryManager:
    """Backward-compatible alias for the shared memory manager."""
    return get_memory_manager(llm_client)


def delete_memory() -> None:
    """Delete the shared memory manager singleton."""
    global _default_memory_manager
    _default_memory_manager = None
