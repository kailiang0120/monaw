"""Multi-phase context compression to keep conversations within context limits.

4-phase algorithm:
1. Tool output pruning (no LLM call) — shrink large tool outputs to summaries
2. Boundary protection — keep first N + recent ~20K tokens intact
3. Middle summarization (LLM call) — structured template
4. Iterative updates — update previous summary instead of regenerating

Guardrails:
- Anti-thrashing: skip if <10% savings expected
- Summarizer isolation: system prompt prevents task execution
- Focus topics: preserve specific subjects
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4
TOOL_OUTPUT_SUMMARY_THRESHOLD = 2000
BOUNDARY_KEEP_FIRST = 3
BOUNDARY_KEEP_RECENT_CHARS = 80_000  # ~20K tokens
ANTI_THRASH_MIN_SAVINGS = 0.10

_SUMMARIZER_SYSTEM = (
    "You are a DIFFERENT assistant whose ONLY job is to summarize conversation history. "
    "You must NEVER execute tasks, call tools, write code, or continue the conversation. "
    "Only produce a structured summary."
)

_SUMMARY_TEMPLATE = (
    "Summarize the following conversation segment into this exact template:\n\n"
    "**Goal**: [What the user is trying to accomplish]\n"
    "**Actions Taken**: [What tools were called and their key results]\n"
    "**Current State**: [Files created, URLs visited, values computed]\n"
    "**Blockers**: [Any unresolved errors or pending items]\n\n"
    "Preserve ALL file paths, URLs, variable values, and error messages exactly.\n"
    "Be concise — 4-8 sentences total.\n\n"
    "---\n\n"
)


@dataclass
class CompressionResult:
    """Outcome of a compression pass."""
    messages: list[dict]
    summary: str
    tokens_before: int
    tokens_after: int
    phases_applied: list[str] = field(default_factory=list)
    # M2: True when LLM summarization was attempted but failed; caller can decide
    # whether to retry, use stale summary, or surface a warning.
    summarization_failed: bool = False

    @property
    def savings_ratio(self) -> float:
        if self.tokens_before == 0:
            return 0.0
        return 1.0 - (self.tokens_after / self.tokens_before)


def _estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def _total_tokens(messages: list[dict]) -> int:
    return sum(_estimate_tokens(m.get("content", "")) for m in messages)


# ── Phase 1: Tool output pruning ─────────────────────────────────────────────


def _prune_tool_outputs(messages: list[dict]) -> list[dict]:
    """Replace large tool outputs with concise summaries (no LLM call)."""
    result = []
    for msg in messages:
        content = msg.get("content", "")
        if not content or len(content) < TOOL_OUTPUT_SUMMARY_THRESHOLD:
            result.append(msg)
            continue

        # Detect tool result messages
        if content.startswith("Tool result for "):
            tool_match = re.match(r"Tool result for (\w+): (.+)", content, re.DOTALL)
            if tool_match:
                tool_name = tool_match.group(1)
                output = tool_match.group(2)
                line_count = output.count("\n") + 1
                # Extract exit code if present
                exit_code = ""
                exit_match = re.search(r"exit[_ ]code[:\s]+(\d+)", output, re.IGNORECASE)
                if exit_match:
                    exit_code = f", exit {exit_match.group(1)}"
                # Extract first meaningful line
                first_line = output.strip().split("\n")[0][:100]
                pruned = f"Tool result for {tool_name}: [{tool_name}] {first_line}{exit_code}, {line_count} lines"
                result.append({**msg, "content": pruned})
                continue

        result.append(msg)
    return result


# ── Phase 2: Boundary protection ─────────────────────────────────────────────


def _split_boundaries(
    messages: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split messages into head (protected), middle (compressible), tail (protected)."""
    if len(messages) <= BOUNDARY_KEEP_FIRST + 2:
        return messages, [], []

    head = messages[:BOUNDARY_KEEP_FIRST]

    # Find how many recent messages fit in the tail budget
    tail: list[dict] = []
    tail_chars = 0
    for msg in reversed(messages[BOUNDARY_KEEP_FIRST:]):
        msg_chars = len(msg.get("content", ""))
        if tail_chars + msg_chars > BOUNDARY_KEEP_RECENT_CHARS:
            break
        tail.insert(0, msg)
        tail_chars += msg_chars

    tail_start = len(messages) - len(tail)
    middle = messages[BOUNDARY_KEEP_FIRST:tail_start]

    return head, middle, tail


# ── Main compression pipeline ────────────────────────────────────────────────


async def compress_context(
    messages: list[dict],
    existing_summary: str = "",
    focus_topics: list[str] | None = None,
    llm_chat_fn=None,
) -> CompressionResult:
    """Run the full 5-phase compression pipeline.

    Args:
        messages: Conversation messages to compress.
        existing_summary: Previous summary to update (Phase 4).
        focus_topics: Subjects to preserve with extra care.
        llm_chat_fn: Async callable(messages, system_prompt) -> str for summarization.
    """
    tokens_before = _total_tokens(messages)
    phases: list[str] = []

    # Phase 1: Prune tool outputs
    messages = _prune_tool_outputs(messages)
    if _total_tokens(messages) < tokens_before:
        phases.append("tool_output_pruning")

    # Anti-thrash guard: if Phase 1 alone already saved enough, skip further phases
    tokens_after_p1 = _total_tokens(messages)
    p1_savings = (1.0 - tokens_after_p1 / tokens_before) if tokens_before > 0 else 0.0
    if p1_savings >= ANTI_THRASH_MIN_SAVINGS:
        return CompressionResult(
            messages=messages,
            summary=existing_summary,
            tokens_before=tokens_before,
            tokens_after=tokens_after_p1,
            phases_applied=phases,
        )

    # Phase 2: Split boundaries
    head, middle, tail = _split_boundaries(messages)
    phases.append("boundary_protection")

    if not middle:
        return CompressionResult(
            messages=messages,
            summary=existing_summary,
            tokens_before=tokens_before,
            tokens_after=_total_tokens(messages),
            phases_applied=phases,
        )

    # Phase 3/4: Summarize or update middle section
    summary = existing_summary
    if llm_chat_fn is not None and middle:
        middle_text = "\n".join(
            f"{m.get('role', 'unknown')}: {m.get('content', '')}" for m in middle
        )

        prompt = _SUMMARY_TEMPLATE + middle_text
        if focus_topics:
            prompt += f"\n\nPay special attention to: {', '.join(focus_topics)}"

        # Phase 4: iterative update if previous summary exists
        if existing_summary.strip():
            prompt += f"\n\nPrevious summary to merge with:\n{existing_summary}"
            phases.append("iterative_summary_update")
        else:
            phases.append("middle_summarization")

        try:
            summary = await llm_chat_fn(
                [{"role": "user", "content": prompt}],
                _SUMMARIZER_SYSTEM,
            )
        except Exception as exc:
            logger.warning("Summarization failed (keeping stale summary): %s", exc)
            summary = existing_summary or "[Earlier messages compacted]"
            # M2: Surface failure so callers know compression was degraded.
            return CompressionResult(
                messages=messages,
                summary=summary,
                tokens_before=tokens_before,
                tokens_after=_total_tokens(messages),
                phases_applied=phases,
                summarization_failed=True,
            )

    # Rebuild: head + summary injection + tail
    compressed = list(head)
    if summary.strip():
        compressed.append({
            "role": "assistant",
            "content": f"[Conversation Summary]\n{summary}",
        })
    compressed.extend(tail)

    return CompressionResult(
        messages=compressed,
        summary=summary,
        tokens_before=tokens_before,
        tokens_after=_total_tokens(compressed),
        phases_applied=phases,
    )
