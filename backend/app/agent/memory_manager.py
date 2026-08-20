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
from app.agent.database import get_db
from app.agent.runtime_paths import RUNTIME_DIR

_RUNTIME_DIR = RUNTIME_DIR
_CONVERSATIONS_DIR = _RUNTIME_DIR / "conversations"
logger = logging.getLogger(__name__)

# ── Context-window budget ─────────────────────────────────────────────────────
CONTEXT_TOKEN_LIMIT = DEFAULT_CONTEXT_TOKEN_LIMIT
COMPACTION_THRESHOLD = int(DEFAULT_CONTEXT_TOKEN_LIMIT * 0.9)
CHARS_PER_TOKEN = 4
MANUAL_COMPACT_MESSAGE_LIMIT = 1000
MAX_CONVERSATION_SUMMARY_CHARS = 12000

MANUAL_COMPACT_SYSTEM_PROMPT = (
    "You are a separate summarizer for a /compact command. "
    "Do not execute tasks, call tools, or continue the conversation. "
    "Only write a durable continuation summary."
)

MANUAL_COMPACT_PROMPT = (
    "Compact this conversation so the next assistant turn can use the summary "
    "instead of the original chat history.\n\n"
    "Write a concise but complete handoff with these sections:\n"
    "- Current goal\n"
    "- User preferences and constraints\n"
    "- Decisions and important facts\n"
    "- Repo, files, commands, URLs, ids, and tool results that matter\n"
    "- Errors, blockers, and unresolved questions\n"
    "- Current state and next steps\n\n"
    "Preserve exact file paths, commands, URLs, identifiers, settings, and error text. "
    "Drop chit-chat and repeated acknowledgements. Do not invent facts."
)


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
            summary=self._cap_summary(conv.get("summary", "")) if conv else "",
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

    def _build_context_sections(self, state: ConversationState, *, current_turn: str = "") -> list[str]:
        pending_steps = state.pending_steps or []
        parts: list[str] = []
        if state.task_goal.strip() and state.task_goal.strip() != current_turn.strip():
            parts.extend(["## Active Goal", state.task_goal.strip(), ""])
        if pending_steps:
            parts.append("## Remaining Steps")
            for step in pending_steps:
                step_id = step.get("step_id", "step")
                description = step.get("description", "Unnamed step")
                status = step.get("status", "pending")
                parts.append(f"- {step_id}: {description} ({status})")
        if state.tool_outcomes:
            if parts:
                parts.append("")
            parts.append("## Recent Tool Results")
            for outcome in state.tool_outcomes[-self.MAX_TOOL_OUTCOMES:]:
                tool_name = outcome.get("tool", "unknown")
                result = outcome.get("result", "")
                if len(result) > 200:
                    result = result[:200] + "..."
                parts.append(f"- {tool_name}: {result}")

        return parts

    # ── Public API ────────────────────────────────────────────────────────

    def get_or_create(self, conversation_id: str, title: str = "") -> ConversationState:
        """Get existing conversation state or create a new one."""
        return self._ensure_state(conversation_id, title)

    def get_context(self, conversation_id: str, *, current_turn: str = "") -> str:
        """Build memory context string to inject into prompts."""
        state = self.get_or_create(conversation_id)
        return "\n".join(self._build_context_sections(state, current_turn=current_turn)).strip()

    @staticmethod
    def _cap_summary(value: str) -> str:
        text = str(value or "").strip()
        if len(text) <= MAX_CONVERSATION_SUMMARY_CHARS:
            return text
        return text[:MAX_CONVERSATION_SUMMARY_CHARS].rstrip() + "\n[summary truncated]"

    @classmethod
    def _merge_summaries(cls, newest: str, older: str) -> str:
        parts = [str(value or "").strip() for value in (newest, older) if str(value or "").strip()]
        return cls._cap_summary("\n\n".join(parts))

    def set_long_term_memory(self, memory: LongTermMemory | None) -> None:
        self.long_term_memory = memory

    def record_compaction(self, conversation_id: str, result: dict) -> None:
        """Persist the compaction checkpoint without exposing the database layer."""
        if result.get("status") not in {"compacted", "unchanged"}:
            return
        summary = str(result.get("summary") or "").strip()
        if not summary:
            return
        source_message_id = self._db.get_last_message_id(conversation_id)
        if source_message_id <= int(result.get("source_message_id") or 0):
            return
        self._db.upsert_conversation_compaction(
            conv_id=conversation_id,
            summary=summary,
            source_message_id=source_message_id,
            message_count=int(result.get("message_count") or 0),
            tokens_before=int(result.get("tokens_before") or 0),
            tokens_after=int(result.get("tokens_after") or 0),
        )

    def build_long_term_memory_context(self, query: str, *, mark_used: bool = True) -> str:
        if self.long_term_memory is None:
            return ""
        try:
            return self.long_term_memory.build_prompt(query, mark_used=mark_used)
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
        if self.long_term_memory is None:
            return
        try:
            self.long_term_memory.capture_turn_candidates(
                conversation_id=conversation_id,
                user_message=user_message,
                assistant_message=assistant_message,
            )
        except Exception as exc:
            logger.warning("Long-term memory candidate capture failed: %s", exc)

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
        compaction = self._db.get_conversation_compaction(conversation_id)
        compaction_source_id = int((compaction or {}).get("source_message_id") or 0)
        if compaction is not None:
            persisted_history = self._db.get_messages_after(
                conversation_id,
                after_id=compaction_source_id,
                limit=self.LLM_HISTORY_MESSAGES,
            )
        else:
            persisted_history = self._db.get_messages(conversation_id, limit=200)
        if persisted_history or compaction is not None:
            state.all_messages = [
                {"role": m["role"], "content": m["content"], "timestamp": m["created_at"]}
                for m in persisted_history
            ]
            state.recent_messages = state.all_messages[-self.MAX_RECENT_MESSAGES:]
        if compaction is not None:
            history = state.all_messages[-self.LLM_HISTORY_MESSAGES:]
        else:
            history = state.all_messages[-self.LLM_HISTORY_MESSAGES:] or state.recent_messages[
                -self.LLM_HISTORY_MESSAGES:
            ]
        messages: list[dict] = []

        if compaction is not None and str(compaction.get("summary") or "").strip():
            messages.append({
                "role": "assistant",
                "content": (
                    "[Compacted Conversation Context]\n"
                    "This replaces all earlier chat history before message "
                    f"{compaction_source_id}. Treat it as authoritative prior context.\n\n"
                    f"{str(compaction.get('summary') or '').strip()}"
                ),
            })
        elif state.summary.strip():
            messages.append({
                "role": "assistant",
                "content": f"Earlier conversation summary:\n{state.summary.strip()}",
            })

        context = self.get_context(conversation_id, current_turn=state.task_goal)
        if context:
            messages.append({"role": "assistant", "content": context})

        for msg in history:
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            messages.append({
                "role": self._normalize_role(msg.get("role", "user")),
                "content": content,
            })

        return messages

    def _messages_for_manual_compaction(
        self,
        conversation_id: str,
        *,
        after_id: int = 0,
    ) -> list[dict]:
        messages = self._db.get_messages_after(
            conversation_id,
            after_id=after_id,
            limit=MANUAL_COMPACT_MESSAGE_LIMIT,
        )
        for message in messages:
            if message.get("role") == "assistant":
                message["tool_calls"] = self._db.get_tool_calls_for_message(int(message["id"]))
        return messages

    def _format_messages_for_compaction(self, messages: list[dict]) -> str:
        parts: list[str] = []
        for message in messages:
            role = str(message.get("role") or "user")
            message_id = int(message.get("id") or 0)
            content = str(message.get("content") or "").strip()
            parts.append(f"[message {message_id}] {role}: {content}")
            for tool_call in message.get("tool_calls") or []:
                tool_name = str(tool_call.get("tool_name") or "")
                status = str(tool_call.get("status") or "")
                tool_input = str(tool_call.get("input") or "").strip()
                output = str(tool_call.get("output") or "").strip()
                if len(output) > 4000:
                    output = output[:4000].rstrip() + "\n[truncated]"
                parts.append(
                    "[tool call "
                    f"{tool_call.get('id')}] {tool_name} ({status})\n"
                    f"input: {tool_input}\noutput: {output}"
                )
        return "\n\n".join(parts).strip()

    async def compact_conversation(self, conversation_id: str) -> dict:
        """Create a durable /compact checkpoint for future turns."""
        self.get_or_create(conversation_id)
        existing = self._db.get_conversation_compaction(conversation_id)
        previous_source_id = int((existing or {}).get("source_message_id") or 0)
        state = self.get_or_create(conversation_id)
        existing_summary = self._merge_summaries(
            str((existing or {}).get("summary") or "").strip(),
            state.summary.strip(),
        )
        messages = self._messages_for_manual_compaction(
            conversation_id,
            after_id=previous_source_id,
        )
        if not messages and not existing_summary:
            return {
                "status": "empty",
                "summary": "",
                "message_count": 0,
                "source_message_id": 0,
                "tokens_before": 0,
                "tokens_after": 0,
            }
        if not messages and existing_summary:
            existing_summary_tokens = count_text_tokens(existing_summary, llm_client=self.llm_client)
            return {
                "status": "unchanged",
                "summary": existing_summary,
                "message_count": 0,
                "source_message_id": previous_source_id,
                "tokens_before": existing_summary_tokens,
                "tokens_after": existing_summary_tokens,
            }

        transcript = self._format_messages_for_compaction(messages)
        prompt_parts = [MANUAL_COMPACT_PROMPT]
        if existing_summary:
            prompt_parts.extend([
                "",
                "Existing compacted context to merge and update:",
                existing_summary,
            ])
        prompt_parts.extend(["", "Conversation segment to compact:", transcript])
        prompt = "\n".join(prompt_parts).strip()
        tokens_before = count_text_tokens(
            "\n\n".join(
                part
                for part in [existing_summary, transcript]
                if part.strip()
            ),
            llm_client=self.llm_client,
        )

        try:
            summary = await self.llm_client.chat(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=MANUAL_COMPACT_SYSTEM_PROMPT,
            )
            summary = self._cap_summary(str(summary or ""))
        except Exception as exc:
            logger.warning("/compact summarization failed; leaving history unchanged: %s", exc)
            summary = ""
        if not summary:
            tokens_after = count_text_tokens(existing_summary, llm_client=self.llm_client) if existing_summary else 0
            return {
                "status": "failed",
                "summary": existing_summary,
                "message_count": len(messages),
                "source_message_id": previous_source_id,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
            }

        source_message_id = int(messages[-1].get("id") or previous_source_id)
        tokens_after = count_text_tokens(summary, llm_client=self.llm_client)
        compaction = self._db.upsert_conversation_compaction(
            conv_id=conversation_id,
            summary=summary,
            source_message_id=source_message_id,
            message_count=len(messages),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )

        state.all_messages = []
        state.recent_messages = []
        state.summary = ""
        state.context_tokens_estimate = self.estimate_context_tokens(conversation_id, "")
        self._db.update_conversation(
            conversation_id,
            summary="",
            context_tokens_estimate=state.context_tokens_estimate,
        )
        return {
            "status": "compacted",
            "summary": summary,
            "message_count": len(messages),
            "source_message_id": int(compaction["source_message_id"]),
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
        }

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
        if self.long_term_memory is not None and state.task_goal:
            try:
                self.long_term_memory.upsert_checkpoint(
                    f"conversation-{conversation_id}",
                    scope="conversation",
                    status="active",
                    conversation_id=conversation_id,
                    goal=state.task_goal,
                    last_known_state="Task started.",
                    next_action="Continue the active task.",
                )
            except Exception as exc:
                logger.debug("Unable to persist memory checkpoint: %s", exc)

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
        if self.long_term_memory is not None:
            try:
                next_step = pending[0]["description"] if pending else ""
                self.long_term_memory.upsert_checkpoint(
                    f"conversation-{conversation_id}",
                    scope="conversation",
                    status="active",
                    conversation_id=conversation_id,
                    goal=state.task_goal,
                    last_known_state=(
                        f"Completed: {completed[-1]}" if completed else "Plan is in progress."
                    ),
                    next_action=next_step or "Finish the current task.",
                )
            except Exception as exc:
                logger.debug("Unable to update memory checkpoint from plan: %s", exc)

    def clear_task_progress(self, conversation_id: str) -> None:
        """Clear the persisted active goal and remaining steps after completion."""
        state = self.get_or_create(conversation_id)
        state.task_goal = ""
        state.pending_steps = []
        state.completed_steps = []
        state.updated_at = datetime.now(timezone.utc)
        self._db.update_conversation(conversation_id, task_goal="")
        self._db.clear_plan_steps(conversation_id)
        if self.long_term_memory is not None:
            try:
                self.long_term_memory.upsert_checkpoint(
                    f"conversation-{conversation_id}",
                    scope="conversation",
                    status="completed",
                    conversation_id=conversation_id,
                    goal="",
                    last_known_state="Task completed.",
                    next_action="",
                )
            except Exception as exc:
                logger.debug("Unable to clear memory checkpoint: %s", exc)

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
        while True:
            before = self.estimate_context_tokens(
                conversation_id,
                system_prompt,
                tools=tools,
                current_user_message=current_user_message,
            )
            if before <= model_compaction_threshold(self.llm_client):
                break
            compacted = await self._compact(conversation_id)
            if not compacted:
                break
            after = self.estimate_context_tokens(
                conversation_id,
                system_prompt,
                tools=tools,
                current_user_message=current_user_message,
            )
            if after >= before:
                break

    async def _compact(self, conversation_id: str) -> bool:
        state = self.get_or_create(conversation_id)
        before = self.estimate_context_tokens(conversation_id, "")
        result = await self.compact_conversation(conversation_id)
        if result.get("status") != "compacted":
            return False
        state.context_tokens_estimate = self.estimate_context_tokens(conversation_id, "")
        return state.context_tokens_estimate < before

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
            state.summary = self._merge_summaries(str(new_summary), state.summary)
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
