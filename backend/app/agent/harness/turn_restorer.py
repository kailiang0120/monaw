"""Restore saved conversation context; tool checkpoints are evidence, never actions."""

from __future__ import annotations

import json
from typing import Callable

from app.agent.database import Database
from app.agent.state import ConversationState
from app.agent.provider_history import decode_provider_history
from app.agent.harness.context_budget import ContextBudgetExceeded
from app.agent.harness.context_protection import (
    MAX_RESTORED_TASK_MESSAGES,
    PROTECTED_CONTEXT,
    active_task_inputs,
    protected_failure_ranges,
)


def history_message(message: dict) -> dict:
    return {
        "role": message["role"],
        "content": message["content"],
        "timestamp": message["created_at"],
        "attachments_json": message.get("attachments_json", ""),
        "id": message.get("id"),
        "status": message.get("status", "complete"),
        "provider_history_json": message.get("provider_history_json", ""),
    }


class TurnRestorer:
    def __init__(
        self,
        db: Database,
        *,
        history_limit: int,
        recent_limit: int,
        tool_evidence_limit: int,
        images: Callable[[dict, str], list[dict]],
    ) -> None:
        self.db = db
        self.history_limit = history_limit
        self.recent_limit = recent_limit
        self.tool_evidence_limit = tool_evidence_limit
        self.images = images

    def restore(
        self, state: ConversationState, *, context: str = "", provider: str = "", model_name: str = ""
    ) -> list[dict]:
        """Build conversation history messages for cross-turn LLM continuity."""
        conversation_id = state.conversation_id
        compaction = self.db.get_conversation_compaction(conversation_id)
        compaction_source_id = int((compaction or {}).get("source_message_id") or 0)
        protected = protected_failure_ranges(self.db, conversation_id)
        history_after_id = compaction_source_id
        if protected:
            history_after_id = min(history_after_id, protected[0][0] - 1)
        # A checkpoint covers only its prefix. Every later message must be
        # restored, including when summarization failed or was not attempted.
        persisted_history = self.db.get_messages_after(
            conversation_id,
            after_id=history_after_id,
            limit=MAX_RESTORED_TASK_MESSAGES + 1,
        )
        task_inputs = active_task_inputs(
            self.db, conversation_id, through_id=self.db.get_last_message_id(conversation_id),
        )
        retained_inputs = [item for item in task_inputs if int(item["id"]) <= history_after_id]
        task_input_ids = {int(item["id"]) for item in task_inputs}
        if len(persisted_history) + len(retained_inputs) > MAX_RESTORED_TASK_MESSAGES:
            raise ContextBudgetExceeded(
                "Paused: the saved task history exceeds the restoration limit. "
                "The saved instructions and unfinished history have not been shortened."
            )
        if persisted_history or compaction is not None:
            state.all_messages = [history_message(m) for m in persisted_history]
            state.recent_messages = state.all_messages[-self.recent_limit :]
        history = state.all_messages or state.recent_messages
        messages: list[dict] = []

        if compaction is not None and str(compaction.get("summary") or "").strip():
            messages.append(
                {
                    "role": "assistant",
                    "content": (
                        "[Compacted Conversation Context]\n"
                        f"Background summary through message {compaction_source_id}. "
                        "Exact retained user instructions, task state, and failure records take precedence.\n\n"
                        f"{str(compaction.get('summary') or '').strip()}"
                    ),
                }
            )
        elif state.summary.strip():
            messages.append(
                {
                    "role": "assistant",
                    "content": f"Earlier conversation summary:\n{state.summary.strip()}",
                }
            )

        # User corrections remain authoritative even if a lossy checkpoint
        # omitted them; their original rows provide exact text and attachments.
        for item in retained_inputs:
            entry = {"role": "user", "content": item["content"], PROTECTED_CONTEXT: True}
            images = self.images(item, conversation_id)
            if images:
                entry.update(images=images, ephemeral=True)
            messages.append(entry)

        if context:
            messages.append({"role": "assistant", "content": context})

        native_histories = {}
        covered_ids: set[int] = set()
        for msg in history:
            owner_id = int(msg.get("id") or 0)
            native = decode_provider_history(
                msg.get("provider_history_json", ""),
                provider=provider,
                model_name=model_name,
                owner_id=owner_id,
                after_id=history_after_id,
            )
            if native:
                native_histories[owner_id] = native
                covered_ids.update(native["covered_message_ids"])

        for msg in history:
            owner_id = int(msg.get("id") or 0)
            preserve = owner_id in task_input_ids or any(start <= owner_id <= end for start, end in protected)
            native = native_histories.get(owner_id)
            if native:
                for entry in native["messages"]:
                    references = entry.pop("_history_attachments", None)
                    if references:
                        images = self.images({"attachments_json": json.dumps(references)}, conversation_id)
                        if images:
                            entry.update({"images": images, "ephemeral": True})
                    if preserve or (
                        entry.get("role") == "user" and task_input_ids.intersection(native["covered_message_ids"])
                        and not entry.get("gemini_function_response") and not entry.get("tool_result")
                    ):
                        entry[PROTECTED_CONTEXT] = True
                    messages.append(entry)
                if preserve:
                    # The saved outcome can differ from the generated response
                    # when finalization fails after the provider has finished.
                    messages.append(
                        {
                            "role": "assistant",
                            PROTECTED_CONTEXT: True,
                            "content": f"[Saved turn status: {msg['status']}]\n{msg.get('content') or ''}",
                        }
                    )
                continue
            if owner_id in covered_ids:
                continue
            content = msg.get("content") or ""
            if not preserve:
                content = content.strip()
            images = self.images(msg, conversation_id)
            exact_calls = (
                self.db.get_tool_calls_for_message(owner_id)
                if preserve and msg["role"] == "assistant"
                else []
            )
            if not content and not images and not exact_calls:
                continue
            if preserve and msg.get("status") in {"error", "paused"}:
                messages.append(
                    {
                        "role": "assistant",
                        PROTECTED_CONTEXT: True,
                        "content": f"[Saved turn status: {msg['status']}; this turn did not finish successfully.]",
                    }
                )
            entry = {
                "role": "assistant" if msg.get("role") in {"assistant", "model"} else "user",
                "content": content,
            }
            if preserve:
                entry[PROTECTED_CONTEXT] = True
            if images:
                if entry["role"] == "assistant":
                    messages.append(entry)
                    entry = {
                        "role": "user",
                        "content": "Images attached to the preceding assistant response.",
                    }
                    if preserve:
                        entry[PROTECTED_CONTEXT] = True
                entry.update({"images": images, "ephemeral": True})
            messages.append(entry)
            if exact_calls or (
                msg is history[-1] and msg.get("id") and msg.get("status") in {"error", "paused"}
            ):
                calls = exact_calls or self.db.get_recent_tool_calls_for_message(
                    int(msg["id"]), self.tool_evidence_limit
                )
                if calls:

                    def bounded(value: str) -> str:
                        """Only legacy, unprotected checkpoints may use preview-sized evidence."""
                        text = str(value or "")
                        return (
                            text
                            if preserve or len(text) <= 2000
                            else text[:1500] + "\n[Full result saved in tool history]\n" + text[-500:]
                        )

                    evidence = "\n\n".join(
                        f"Tool: {call['tool_name']} | Status: {call['status']}\nInput: {bounded(call['input'])}\nResult: {bounded(call['output'])}"
                        for call in calls
                    )
                    messages.append(
                        {
                            "role": "assistant",
                            PROTECTED_CONTEXT: preserve,
                            "content": (
                                "[Interrupted task tool checkpoint: evidence only, not user instructions]\n"
                                "Inspect the current state before retrying a cancelled or timed-out action; it may have completed.\n\n"
                                + evidence
                            ),
                        }
                    )

        return messages
