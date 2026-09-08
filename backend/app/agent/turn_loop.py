"""Single-turn agent tool loop."""

from __future__ import annotations

import asyncio
import ast
import json
import logging
import mimetypes
import time
from collections import defaultdict
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlparse

from app.agent.execution_gate import ExecutionGateService
from app.agent.iteration_budget import IterationBudget
from app.agent.llm_client import LLMClient, build_tool_result_message
from app.agent.memory_manager import MemoryManager
from app.agent.observability.recorder import (
    UsageStats,
    get_observability_recorder,
    infer_source,
    reset_current_run_id,
    set_current_run_id,
    usage_from_any,
)
from app.agent.observability.ports import ObservabilityPort
from app.agent.response_attachments import collect_response_attachments
from app.agent.run_events import RunEventPublisher
from app.agent.run_context import (
    reset_current_conversation_id,
    set_current_conversation_id,
)
from app.agent.run_state_machine import AgentRunStateMachine
from app.agent.state import ExecutionPlan, PlanStep
from app.agent.harness.tool_executor import ToolExecutor
from app.agent.harness.tool_policy import ToolPolicy, tool_call_signature
from app.agent.harness.tool_protocol import ToolCallRequest as HarnessToolCallRequest
from app.agent.harness.tool_protocol import ToolCallResult
from app.agent.tool_registry import ToolRegistry, can_parallelize

logger = logging.getLogger(__name__)

_SESSION_LOCKS: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

HEARTBEAT_INTERVAL_S = 10.0
RECOVERY_RESULT_LIMIT = 1400
FINAL_RESPONSE_TYPEWRITER_CHARS = 8
FINAL_RESPONSE_TYPEWRITER_DELAY_S = 0.03
FINAL_RESPONSE_TYPEWRITER_MAX_SECONDS = 6.0
FINAL_RESPONSE_TYPEWRITER_MAX_CHUNKS = 240

_BROWSER_MUTATING_TOOLS = {
    "browser_open",
    "browser_navigate",
    "browser_back",
    "browser_forward",
    "browser_reload",
    "browser_tabs",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_select_option",
    "browser_scroll",
}

_BROWSER_OBSERVATION_TOOLS = {
    "browser_snapshot",
    "browser_screenshot",
    "browser_full_page_screenshot",
    "browser_evaluate",
    "browser_wait",
}

FINAL_ANSWER_TOOL_NAME = "final_answer"
FINAL_ANSWER_TOOL = {
    "name": FINAL_ANSWER_TOOL_NAME,
    "description": (
        "Complete the current user request. Use this only when no more tool calls "
        "are needed and the response is ready for the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "description": "The final response to show to the user.",
            },
            "attachment_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional exact local file paths from prior tool outputs that should "
                    "be sent as user-visible attachments. Use only for final deliverables, "
                    "not intermediate progress artifacts."
                ),
            },
        },
        "required": ["answer"],
    },
}


def _is_browser_tool(tool_name: str) -> bool:
    return tool_name.startswith("browser_")


def _requires_structured_final_tool(llm_client: LLMClient) -> bool:
    return True


def _tools_with_final_answer(tools: list[dict], *, enabled: bool) -> list[dict]:
    if not enabled:
        return tools
    if any(str(tool.get("name") or "") == FINAL_ANSWER_TOOL_NAME for tool in tools):
        return tools
    return [*tools, dict(FINAL_ANSWER_TOOL)]


def _completion_protocol_prompt(system_prompt: str, *, enabled: bool) -> str:
    protocol_lines = [
        "## Completion Protocol",
        "- Use the same decision rule for every provider and model.",
        "- Progress updates are allowed, but progress text does not finish the turn.",
        "- If more work is needed, call the next real tool.",
        "- If no tools are available and the request can be answered directly, answer directly and finish the turn.",
        "- Do not rely on plain text alone to continue a tool-capable turn; the runtime will keep asking for the next action.",
        "- Do not try to inspect, expand, collapse, click, or control the chat UI, reasoning trace, progress messages, message bubbles, or tool cards. Those are frontend display surfaces, not part of the task.",
        "- Do not claim that you already answered a fresh user question unless the persisted conversation history shows that exact answer.",
    ]
    if enabled:
        protocol_lines.insert(
            1,
            f"- To finish the turn, call `{FINAL_ANSWER_TOOL_NAME}` with the final answer.",
        )
        protocol_lines.insert(
            2,
            "- If you need to keep working, call the next real tool. You may include one short progress update before the tool call.",
        )
        protocol_lines.insert(
            3,
            "- When files from tool outputs should be delivered to the user, include their exact local paths in `attachment_paths` on the final answer tool call.",
        )
    protocol = "\n".join(protocol_lines)
    return f"{system_prompt}\n\n{protocol}" if system_prompt else protocol


_TOOL_REPROMPT_MESSAGE = (
    f"Choose the next action using the tool protocol. If the task is complete, call "
    f"`{FINAL_ANSWER_TOOL_NAME}` with the final answer. If more work is needed, call the "
    "next real task tool. Do not respond with plain text only, and do not try to control "
    "the chat UI or reasoning trace."
)

_TOOL_FORCE_MESSAGE = (
    "You have responded with text multiple times without calling any tool. "
    "You MUST call a tool now. Either call the next task tool to make progress, "
    f"or call `{FINAL_ANSWER_TOOL_NAME}` to finish. "
    "Do not respond with plain text."
)

_TEXT_ONLY_FORCE_AFTER = 2
_TEXT_ONLY_CIRCUIT_BREAK_AFTER = 4


def _compact_for_recovery(value: object, *, limit: int = RECOVERY_RESULT_LIMIT) -> str:
    if value is None:
        return "No previous result is available."
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if not text:
        return "No previous result is available."
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


def _build_repeat_recovery_prompt(
    *,
    reason_code: str,
    tool_name: str,
    arguments: dict,
    last_result: object = None,
) -> str:
    arguments_text = _compact_for_recovery(arguments, limit=900)
    result_text = _compact_for_recovery(last_result)
    if reason_code == "stalled_repeat_detected":
        next_steps = (
            "For the next step, use a browser observation or finish the turn: "
            "`browser_snapshot`, `browser_tabs(action=\"list\")`, "
            "`browser_session(action=\"status\")`, `browser_session(action=\"doctor\")`, "
            "or `final_answer`. After inspection, only act again if the target or arguments change."
        )
    elif reason_code == "repeated_blocked_tool_result":
        next_steps = (
            "Do not call the same tool with the same arguments again in this turn. "
            "Ask for permission or access changes if needed, choose a different permitted action, "
            "or call `final_answer` with the blocker."
        )
    else:
        next_steps = (
            "Do not call this exact tool with these exact arguments again in this turn. "
            "Use the prior result, inspect current state, choose a different action, "
            "or call `final_answer`."
        )
    return (
        "Loop recovery checkpoint: the last tool request repeated a blocked or stalled action.\n\n"
        f"Reason: `{reason_code}`\n"
        f"Repeated tool: `{tool_name}`\n"
        f"Repeated arguments:\n{arguments_text}\n\n"
        f"Last available result:\n{result_text}\n\n"
        f"{next_steps}"
    )

_FINAL_ANSWER_REPROMPT_MESSAGE = (
    f"Your previous message appears to be the final answer. Complete the turn now by calling "
    f"`{FINAL_ANSWER_TOOL_NAME}` with the exact previous assistant message as the `answer` value. "
    "Do not summarize, shorten, rewrite, translate, add to, or remove anything from that message. "
    "Do not repeat the answer as plain text."
)

_WEB_SEARCH_RESULT_REPROMPT_MESSAGE = (
    "Your final answer did not include the actual web_search results. Use the web_search "
    "tool output above and provide a concrete answer with the result titles, source URLs, "
    "and key snippets. Do not just say the result is ready or visible above; include the "
    f"results in the `{FINAL_ANSWER_TOOL_NAME}` answer."
)


def _parse_tool_output(value: object) -> object:
    if not isinstance(value, str):
        return value
    content = value.strip()
    if not content:
        return ""
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        return ast.literal_eval(content)
    except (SyntaxError, ValueError, TypeError):
        return content


def _search_results_from_output(value: object) -> list[dict]:
    payload = _parse_tool_output(value)
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _important_words(text: str) -> set[str]:
    stop_words = {
        "about",
        "after",
        "from",
        "headlines",
        "latest",
        "news",
        "result",
        "results",
        "source",
        "that",
        "this",
        "with",
    }
    words = {
        "".join(ch for ch in word.lower() if ch.isalnum())
        for word in str(text or "").split()
    }
    return {word for word in words if len(word) >= 4 and word not in stop_words}


def _final_answer_includes_web_search_results(answer: str, tool_calls: list[dict]) -> bool:
    web_outputs = [
        tool_call.get("output")
        for tool_call in tool_calls
        if str(tool_call.get("tool_name") or "") == "web_search"
    ]
    if not web_outputs:
        return True

    results: list[dict] = []
    for output in web_outputs:
        results.extend(_search_results_from_output(output))
    if not results:
        return True

    answer_text = str(answer or "")
    answer_lower = answer_text.lower()

    for result in results[:5]:
        url = str(result.get("url") or "").strip()
        title = str(result.get("title") or "").strip()
        domain = urlparse(url).netloc.lower().removeprefix("www.")
        if domain and domain in answer_lower:
            return True
        title_words = _important_words(title)
        if title_words and len(title_words.intersection(_important_words(answer_text))) >= 2:
            return True
    return False


def _emit_progress_message(emit: Callable[[dict], None], text: str) -> None:
    content = str(text or "").strip()
    if content:
        emit({"event": "progress", "data": {"content": content}})


def _typewriter_chunks(text: str) -> list[str]:
    content = str(text or "")
    if not content:
        return []
    chunk_size = max(
        FINAL_RESPONSE_TYPEWRITER_CHARS,
        (len(content) + FINAL_RESPONSE_TYPEWRITER_MAX_CHUNKS - 1)
        // FINAL_RESPONSE_TYPEWRITER_MAX_CHUNKS,
    )
    return [
        content[index:index + chunk_size]
        for index in range(0, len(content), chunk_size)
    ]


async def _emit_typewriter_tokens(emit: Callable[[dict], None], text: str) -> None:
    chunks = _typewriter_chunks(text)
    if not chunks:
        return
    delay = 0.0
    if len(chunks) > 1:
        delay = min(
            FINAL_RESPONSE_TYPEWRITER_DELAY_S,
            FINAL_RESPONSE_TYPEWRITER_MAX_SECONDS / (len(chunks) - 1),
        )
    for index, chunk in enumerate(chunks):
        emit({"event": "token", "data": {"content": chunk}})
        if delay > 0 and index < len(chunks) - 1:
            await asyncio.sleep(delay)


def _final_answer_text(arguments: dict, fallback_content: str) -> str:
    answer = str(arguments.get("answer") or arguments.get("summary") or "").strip()
    return answer or fallback_content.strip() or "Done."


def _final_answer_attachment_paths(arguments: dict) -> list[str]:
    raw_paths = arguments.get("attachment_paths")
    if not isinstance(raw_paths, list):
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        if not isinstance(raw_path, str):
            continue
        path = raw_path.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _strip_json_fence(text: str) -> str:
    content = str(text or "").strip()
    if not content.startswith("```"):
        return content
    lines = content.splitlines()
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return content
    return "\n".join(lines[1:-1]).strip()


def _coerce_final_answer_args(arguments: Any) -> dict | None:
    """Return the arguments dict for a final_answer payload, decoding a JSON string
    if needed; None when the payload is not a usable dict."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    return arguments if isinstance(arguments, dict) else None


def _json_text_as_final_answer(text: str) -> str | None:
    """Accept plain-text JSON only when it matches the final_answer schema."""
    content = _strip_json_fence(text)
    if not content:
        return None
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    if isinstance(payload.get("answer"), str):
        answer = payload["answer"].strip()
        return answer or None
    if isinstance(payload.get("summary"), str):
        summary = payload["summary"].strip()
        return summary or None

    tool_name = str(payload.get("tool_name") or payload.get("name") or "").strip()
    if tool_name == FINAL_ANSWER_TOOL_NAME:
        arguments = _coerce_final_answer_args(payload.get("arguments"))
        if arguments is not None:
            return _final_answer_text(arguments, content)

    function_payload = payload.get("function")
    if isinstance(function_payload, dict) and str(function_payload.get("name") or "") == FINAL_ANSWER_TOOL_NAME:
        arguments = _coerce_final_answer_args(function_payload.get("arguments"))
        if arguments is not None:
            return _final_answer_text(arguments, content)

    return None


_FINAL_TEXT_PREFIXES = (
    "done",
    "complete",
    "completed",
    "task complete",
    "task is complete",
    "the task is complete",
    "i completed",
    "i have completed",
    "i've completed",
)

_PENDING_ACTION_PHRASES = (
    "let me ",
    "i'll ",
    "i will ",
    "i need to ",
    "i should ",
    "i'm going to ",
    "i am going to ",
    "next i ",
    "now i ",
    "proceed to ",
    "continue to ",
)

_PENDING_FINAL_MIN_CHARS = 160
_PENDING_FINAL_MIN_LINES = 3


def _plain_text_as_completion(text: str) -> str | None:
    """Accept explicit completion text without letting it become a progress loop."""
    content = str(text or "").strip()
    if not content:
        return None
    normalized = content.lower().lstrip(" \t\r\n-:—–")
    if not any(normalized.startswith(prefix) for prefix in _FINAL_TEXT_PREFIXES):
        return None

    # "Done typing. Let me send it." is not complete; it is a pending action.
    # Conditional offers after a real completion are fine: "If you want, I can..."
    actionable_text = normalized
    for marker in ("if you want", "if you'd like", "if you would like"):
        index = actionable_text.find(marker)
        if index >= 0:
            actionable_text = actionable_text[:index]
            break
    if any(phrase in actionable_text for phrase in _PENDING_ACTION_PHRASES):
        return None
    return content


def _is_substantial_plain_text(text: str) -> bool:
    content = str(text or "").strip()
    if len(content) >= _PENDING_FINAL_MIN_CHARS:
        return True
    meaningful_lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(meaningful_lines) >= _PENDING_FINAL_MIN_LINES:
        return True
    return content.startswith(("#", "##", "- ", "* ", "1. "))


def _plain_text_as_pending_final(text: str) -> str | None:
    """Remember substantial answer-like text while enforcing final_answer."""
    content = str(text or "").strip()
    if not content or not _is_substantial_plain_text(content):
        return None
    return content


def _should_use_pending_final_text(final_text: str, pending_final_text: str) -> bool:
    final_content = str(final_text or "").strip()
    pending_content = str(pending_final_text or "").strip()
    if not final_content or not pending_content:
        return False
    if not _is_substantial_plain_text(pending_content):
        return False
    if _is_substantial_plain_text(final_content):
        return False
    if len(final_content) > max(240, int(len(pending_content) * 0.35)):
        return False
    return True


def _is_browser_repeat_guarded(tool_name: str, arguments: dict) -> bool:
    if not _is_browser_tool(tool_name):
        return False
    if tool_name in _BROWSER_OBSERVATION_TOOLS:
        return False
    if tool_name == "browser_tabs":
        action = str(arguments.get("action") or "list").strip().lower()
        return action != "list"
    if tool_name == "browser_session":
        action = str(arguments.get("action") or "status").strip().lower()
        return action not in {"status", "list_profiles"}
    return tool_name in _BROWSER_MUTATING_TOOLS or tool_name == "browser_session"


def _is_browser_observation_action(tool_name: str, arguments: dict) -> bool:
    if not _is_browser_tool(tool_name):
        return False
    if tool_name in _BROWSER_OBSERVATION_TOOLS:
        return True
    if tool_name == "browser_tabs":
        action = str(arguments.get("action") or "list").strip().lower()
        return action == "list"
    if tool_name == "browser_session":
        action = str(arguments.get("action") or "status").strip().lower()
        return action in {"status", "doctor", "list_profiles"}
    return False


def _parse_tool_json(tool_output: str) -> dict:
    try:
        data = json.loads(tool_output) if isinstance(tool_output, str) else tool_output
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _tool_error_code(tool_output: str) -> str:
    payload = _parse_tool_json(tool_output)
    for key in ("code", "reason_code", "error_code"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return "tool_error"


def _extract_image_attachments(tool_name: str, tool_output: str) -> list[dict]:
    if tool_name not in {
        "browser_snapshot",
        "browser_screenshot",
        "browser_full_page_screenshot",
        "computer_functions_get_window_state",
    }:
        return []
    payload = _parse_tool_json(tool_output)
    screenshot = payload.get("screenshot") if isinstance(payload.get("screenshot"), dict) else {}
    path = str(
        payload.get("screenshot_path")
        or payload.get("path")
        or screenshot.get("screenshot_path")
        or screenshot.get("path")
        or ""
    ).strip()
    if not path:
        return []
    mime_type = mimetypes.guess_type(path)[0] or "image/png"
    return [{"path": path, "mime_type": mime_type}]


def _attachment_image_payloads(attachments: list[dict] | None) -> list[dict]:
    payloads: list[dict] = []
    for attachment in attachments or []:
        mime_type = str(attachment.get("mime_type") or "").strip()
        path = str(attachment.get("path") or "").strip()
        if not path:
            continue
        if not mime_type:
            mime_type = mimetypes.guess_type(path)[0] or ""
        if mime_type.startswith("image/"):
            payloads.append({"path": path, "mime_type": mime_type})
    return payloads


def _browser_state_fingerprint(tool_output: str) -> str:
    payload = _parse_tool_json(tool_output)
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
    page_state = snapshot.get("page_state") if isinstance(snapshot.get("page_state"), dict) else {}
    clicked = payload.get("clicked") if isinstance(payload.get("clicked"), dict) else {}
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    stable = {
        "url": page.get("url") or snapshot.get("url") or "",
        "title": page.get("title") or snapshot.get("title") or "",
        "loading_state": page_state.get("loading_state") or "",
        "is_blank_page": page_state.get("is_blank_page"),
        "tab_count": len(payload.get("tabs") or []) if isinstance(payload.get("tabs"), list) else None,
        "selector": payload.get("selector") or clicked.get("selector") or "",
        "clicked_ref": clicked.get("ref") or "",
        "target": _stable_target_fingerprint(payload.get("target") or clicked.get("element")),
        "target_after": _stable_target_fingerprint(payload.get("target_after") or clicked.get("element_after")),
        "verification": {
            "status": verification.get("status") or "",
            "verified": verification.get("verified"),
            "reason_code": verification.get("reason_code") or "",
            "actual_value": verification.get("actual_value") or "",
        },
    }
    return json.dumps(stable, sort_keys=True, ensure_ascii=False)


def _stable_target_fingerprint(metadata: object) -> dict | None:
    if not isinstance(metadata, dict):
        return None
    keys = (
        "ref",
        "tag",
        "role",
        "type",
        "id",
        "name",
        "aria_label",
        "placeholder",
        "text",
        "value",
        "contenteditable",
        "checked",
        "selected",
        "disabled",
        "expanded",
        "aria_expanded",
    )
    return {key: metadata.get(key) for key in keys if key in metadata}


def _repeat_key(tool_name: str, arguments: dict, tool_output: str) -> str:
    return json.dumps(
        {
            "tool": tool_name,
            "arguments": arguments,
            "state": _browser_state_fingerprint(tool_output),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _blocked_tool_key(tool_name: str, arguments: dict, tool_output: str) -> str | None:
    payload = _parse_tool_json(tool_output)
    if str(payload.get("status") or "").lower() != "blocked":
        return None
    return json.dumps(
        {
            "tool": tool_name,
            "arguments": arguments,
            "reason_code": payload.get("reason_code") or payload.get("code") or "",
            "reason": payload.get("reason") or payload.get("error") or "",
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _compact_tool_observation(tool_name: str, tool_output: str) -> str:
    payload = _parse_tool_json(tool_output)
    if not payload:
        return "Tool completed."
    if str(payload.get("status") or "").lower() == "error":
        return tool_output
    parts: list[str] = []
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    if page:
        page_label = page.get("title") or page.get("url")
        if page_label:
            parts.append(f"page={page_label}")
    if tool_name == "browser_type" and isinstance(payload.get("target_after"), dict):
        target = payload["target_after"]
        label = target.get("aria_label") or target.get("placeholder") or target.get("name") or target.get("role")
        value = str(target.get("value") or "")[:80]
        parts.append(f"target={label or 'field'} value={value!r}")
    if tool_name == "browser_snapshot" and isinstance(payload.get("snapshot"), dict):
        snapshot = payload["snapshot"]
        candidates = snapshot.get("field_candidates")
        if isinstance(candidates, dict):
            parts.append(f"field_candidates={','.join(sorted(candidates.keys()))}")
    return "; ".join(parts) or "Tool completed."


async def _heartbeat_pump(
    publisher: RunEventPublisher,
    stop_event: asyncio.Event,
) -> None:
    """Push a heartbeat event whenever no real event has been flushed for HEARTBEAT_INTERVAL_S."""
    # H4: Use asyncio.wait_for instead of sleep(1) polling so teardown is immediate.
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        deadline = publisher.last_flush[0] + HEARTBEAT_INTERVAL_S
        timeout = max(0.01, deadline - loop.time())
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=timeout)
            break  # stop_event was set
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        if loop.time() - publisher.last_flush[0] >= HEARTBEAT_INTERVAL_S:
            await publisher.heartbeat(phase="idle")


class TurnLoop:
    MAX_ITERATIONS = 40

    def __init__(
        self,
        llm_client: LLMClient,
        registry: ToolRegistry,
        memory: MemoryManager,
        budget: IterationBudget | None = None,
        max_iterations: int | None = None,
        max_turn_seconds: float = 1800.0,
        max_llm_call_seconds: float = 300.0,
        observability: ObservabilityPort | None = None,
        max_parallel_tool_calls: int = 4,
    ) -> None:
        self.llm_client = llm_client
        self.registry = registry
        self.memory = memory
        self._max_iterations = max_iterations if max_iterations is not None else self.MAX_ITERATIONS
        seed_budget = budget or IterationBudget(max_iterations=self._max_iterations)
        self._turn_budget_max_iterations = seed_budget.max_iterations
        self.max_turn_seconds = max_turn_seconds
        self.max_llm_call_seconds = max_llm_call_seconds
        self.max_parallel_tool_calls = max(1, int(max_parallel_tool_calls))
        self.executor = ToolExecutor()
        self.policy = ToolPolicy()
        self.execution_gate = ExecutionGateService()
        self.observability: ObservabilityPort = observability or get_observability_recorder()

    def shutdown(self) -> None:
        self.executor.shutdown()

    def run_on_affinity(self, affinity_group: str, fn) -> bool:
        return self.executor.run_on_affinity(affinity_group, fn)

    def _new_turn_budget(self) -> IterationBudget:
        return IterationBudget(max_iterations=self._turn_budget_max_iterations)

    async def _execute_tool_result(
        self,
        budget: IterationBudget,
        tool_dict: dict,
        arguments: dict,
        *,
        call_id: str = "",
    ) -> ToolCallResult:
        return await self.executor.execute(
            tool_dict=tool_dict,
            arguments=arguments,
            budget=budget,
            call_id=call_id,
        )

    async def _execute_tool(self, budget: IterationBudget, tool_dict: dict, arguments: dict) -> str:
        result = await self._execute_tool_result(budget, tool_dict, arguments)
        return result.output

    def _build_tool_result_message(
        self,
        *,
        call_id: str,
        tool_name: str,
        tool_output: str,
        status: str = "ok",
    ) -> dict:
        result = ToolCallResult.from_output(
            call_id=call_id,
            name=tool_name,
            output=tool_output,
            fallback_status=status,
        )
        return build_tool_result_message(
            result,
            provider=str(getattr(self.llm_client, "provider", "") or ""),
        )

    @staticmethod
    def _describe_tool_step(tool_name: str, arguments: dict) -> str:
        if tool_name == "browser_snapshot":
            return "Inspect the current browser page"
        if tool_name == "browser_tabs":
            return "Inspect open browser tabs"
        if tool_name == "browser_open":
            if arguments.get("reuse_existing", True):
                return "Open or reuse the matching browser tab"
            return "Open the requested browser page"
        if tool_name == "browser_click":
            return "Click the identified browser element"
        if tool_name == "browser_type":
            return "Fill the identified browser field"
        if tool_name == "browser_select_option":
            return "Select the requested browser option"
        if tool_name.startswith("browser_"):
            return f"Run browser action `{tool_name}`"
        return f"Run `{tool_name}`"

    @classmethod
    def _build_execution_plan(cls, message: str, tool_calls: list[dict]) -> ExecutionPlan:
        steps: list[PlanStep] = []
        for index, call in enumerate(tool_calls, start=1):
            tool_name = str(call.get("name", "tool"))
            arguments = call.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            steps.append(
                PlanStep(
                    step_id=f"tool-{index}",
                    description=cls._describe_tool_step(tool_name, arguments),
                    tool_hints=[tool_name],
                    success_criteria=f"`{tool_name}` returns a successful result or a clear recoverable error.",
                )
            )
        return ExecutionPlan(original_message=message, steps=steps)

    @staticmethod
    def _tool_output_status(tool_output: str, fallback: str = "ok") -> str:
        try:
            payload = json.loads(tool_output) if isinstance(tool_output, str) else tool_output
        except (json.JSONDecodeError, TypeError):
            return fallback
        if not isinstance(payload, dict):
            return fallback
        status = str(payload.get("status") or "").lower()
        if status in {"error", "blocked", "denied", "failed"}:
            return "error"
        return fallback

    async def execute_tool_call(
        self,
        tool_name: str,
        arguments: dict,
        *,
        budget: IterationBudget | None = None,
    ) -> str:
        dispatched = self.registry.dispatch(tool_name, arguments)
        if dispatched is None:
            return json.dumps(
                {"status": "error", "error": f"Unknown tool '{tool_name}'"}
            )
        return await self._execute_tool(
            budget or self._new_turn_budget(),
            dispatched["tool"],
            dispatched["arguments"],
        )

    async def resolve_pending_tool_output(
        self,
        tool_name: str,
        arguments: dict,
        tool_output: str,
        *,
        budget: IterationBudget | None = None,
    ) -> tuple[str, list[dict]]:
        return await self.execution_gate.handle_pending_tool_output(
            tool_output=tool_output,
            tool_name=tool_name,
            arguments=arguments,
            registry=self.registry,
            budget=budget or self._new_turn_budget(),
            execute_tool=self._execute_tool,
        )

    @staticmethod
    def _check_pending_status(tool_output: str) -> dict | None:
        return ExecutionGateService.check_pending_status(tool_output)

    async def _await_ticket_resolution(
        self,
        budget: IterationBudget,
        ticket_id: str,
        event_kind: str,
        tool_dict: dict,
        arguments: dict,
        timeout: float = 600.0,
    ):
        async for event in self.execution_gate.await_ticket_resolution(
            budget=budget,
            ticket_id=ticket_id,
            event_kind=event_kind,
            tool_dict=tool_dict,
            arguments=arguments,
            execute_tool=self._execute_tool,
            timeout=timeout,
        ):
            yield event

    async def _react_worker(
        self,
        message: str,
        conversation_id: str,
        system_prompt: str | Callable[[], str],
        publisher: RunEventPublisher,
        attachments: list[dict] | None = None,
    ) -> None:
        """Run the react loop, pushing all events into the queue. Caller is responsible for the None sentinel."""
        context_token = set_current_conversation_id(conversation_id)
        obs_run_id = ""
        obs_context_token = None
        turn_started_at = time.perf_counter()
        run_state = AgentRunStateMachine()

        def _put(event: dict) -> None:
            publisher.publish_nowait(event)

        def _emit_run_transition(transition) -> None:
            _put(
                {
                    "event": "run_state",
                    "data": {
                        "from_state": transition.from_state.value,
                        "state": transition.to_state.value,
                        "reason": transition.reason,
                    },
                }
            )

        def _transition(method, *args):
            transition = method(*args)
            _emit_run_transition(transition)
            return transition

        final_text = ""
        final_attachment_paths: list[str] = []
        streamed_answer_text = ""
        persisted_tool_calls: list[dict] = []
        last_tool_name = ""
        latest_progress_text = ""
        # Calls are prepared in model order, but a safe contiguous batch may
        # have several invocations in flight at once.  Keep every started call
        # until its ordered result has been persisted so cancellation can
        # account for the whole batch rather than only the last call.
        active_tool_calls: dict[str, dict] = {}
        active_tool_tasks: dict[str, asyncio.Task] = {}
        turn_persisted = False
        obs_usage_total = UsageStats()

        def _obs_event(event_type: str, **kwargs) -> None:
            if not obs_run_id:
                return
            try:
                self.observability.log_event(
                    run_id=obs_run_id,
                    conversation_id=conversation_id,
                    event_type=event_type,
                    source=infer_source(conversation_id),
                    model=str(getattr(self.llm_client, "model_name", "") or ""),
                    provider=str(getattr(self.llm_client, "provider", "") or ""),
                    **kwargs,
                )
            except Exception:
                logger.debug("observability event failed event_type=%s", event_type, exc_info=True)

        def _obs_error(message_text: str, *, error_type: str = "", metadata: dict | None = None) -> None:
            try:
                self.observability.log_error(
                    run_id=obs_run_id,
                    conversation_id=conversation_id,
                    message=message_text,
                    error_type=error_type,
                    metadata=metadata or {},
                )
            except Exception:
                logger.debug("observability error capture failed", exc_info=True)

        def _obs_tool_start(tool_name: str, arguments: dict, call_id: str, risk: str = "") -> float:
            started = time.perf_counter()
            _obs_event(
                "tool_call_started",
                tool_name=tool_name,
                input={"arguments": arguments, "call_id": call_id, "risk": risk},
            )
            return started

        def _obs_tool_end(
            tool_name: str,
            output: str,
            status: str,
            call_id: str,
            started: float,
            *,
            risk: str = "",
            error_code: str = "",
            metadata: dict | None = None,
            duration_ms: int | None = None,
        ) -> None:
            _obs_event(
                "tool_call_finished",
                tool_name=tool_name,
                status=status,
                error_code=error_code,
                error_message=output if status not in {"ok", "complete", "success"} else "",
                duration_ms=(
                    duration_ms
                    if duration_ms is not None
                    else round((time.perf_counter() - started) * 1000)
                ),
                output={"output": output, "call_id": call_id, "risk": risk},
                metadata=metadata or {},
            )

        def _emit_agent_progress(text: str) -> None:
            nonlocal latest_progress_text
            content = str(text or "").strip()
            if content:
                latest_progress_text = content
                _emit_progress_message(_put, content)

        def _assistant_message_on_cancel() -> str:
            if final_text.strip():
                return final_text.strip()
            if streamed_answer_text.strip():
                return streamed_answer_text.strip()
            if latest_progress_text.strip():
                return (
                    "Stopped before final answer.\n\n"
                    f"Last progress: {latest_progress_text.strip()}"
                )
            if last_tool_name:
                return f"Stopped before final answer. Last tool: {last_tool_name}."
            return "Stopped before final answer."

        def _tool_calls_for_persist() -> list[dict]:
            tool_calls = [dict(tool_call) for tool_call in persisted_tool_calls]
            tool_calls.extend(
                {
                    key: tool_call[key]
                    for key in ("tool_name", "input", "output", "status")
                    if key in tool_call
                }
                for tool_call in active_tool_calls.values()
            )
            return tool_calls

        def _emit_response_token(content: str) -> None:
            _put({"event": "token", "data": {"content": content}})

        def _emit_response_event(event: dict) -> None:
            if event.get("event") == "token":
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                _emit_response_token(str(data.get("content") or ""))
                return
            _put(event)

        def _response_duration_ms() -> int:
            return max(0, round((time.perf_counter() - turn_started_at) * 1000))

        async def _persist_turn_once(
            assistant_message: str,
            *,
            status: str,
            response_attachments: list[dict] | None = None,
        ) -> None:
            nonlocal turn_persisted
            if turn_persisted:
                return
            _transition(run_state.finalizing)
            response_duration_ms = _response_duration_ms()
            persist_turn = getattr(self.memory, "persist_turn", None)
            if callable(persist_turn):
                await persist_turn(
                    conversation_id,
                    message,
                    assistant_message,
                    tool_calls=_tool_calls_for_persist(),
                    thinking="",
                    status=status,
                    response_duration_ms=response_duration_ms,
                    response_attachments=response_attachments or [],
                )
            else:
                await self.memory.add_message_and_maybe_summarize(
                    conversation_id, "user", message
                )
                await self.memory.add_message_and_maybe_summarize(
                    conversation_id, "assistant", assistant_message
                )
            turn_persisted = True

        try:
            obs_run_id = self.observability.start_run(
                conversation_id=conversation_id,
                user_message=message,
                source=infer_source(conversation_id),
                model=str(getattr(self.llm_client, "model_name", "") or ""),
                provider=str(getattr(self.llm_client, "provider", "") or ""),
                metadata={"attachment_count": len(attachments or [])},
            )
            obs_context_token = set_current_run_id(obs_run_id)
            budget = self._new_turn_budget()
            self.policy.begin_turn()
            self.memory.get_or_create(conversation_id, title=message[:60] + ("..." if len(message) > 60 else ""))
            self.memory.set_task_goal(conversation_id, message)
            self.memory.set_active_task(conversation_id, None)
            structured_final_required = _requires_structured_final_tool(self.llm_client)
            build_long_term_context = getattr(self.memory, "build_long_term_memory_context", None)
            long_term_context = (
                build_long_term_context(message)
                if callable(build_long_term_context)
                else ""
            )

            def _current_system_prompt() -> str:
                base_prompt = system_prompt() if callable(system_prompt) else system_prompt
                if long_term_context:
                    base_prompt = f"{base_prompt}\n\n{long_term_context}" if base_prompt else long_term_context
                return _completion_protocol_prompt(base_prompt, enabled=structured_final_required)

            initial_visible_tools = self.registry.get_all_tools(visible_only=True)
            await self.memory.ensure_context_fits(
                conversation_id,
                _current_system_prompt(),
                tools=initial_visible_tools,
                current_user_message=message,
            )
            messages = self.memory.build_llm_messages(conversation_id)
            initial_message = {"role": "user", "content": message}
            initial_images = _attachment_image_payloads(attachments)
            if initial_images:
                initial_message["images"] = initial_images
                initial_message["ephemeral"] = True
            messages.append(initial_message)

            budget_exhausted = False
            completed_normally = False
            iteration_limit_hit = False
            terminal_error = False
            incomplete_reason_code = ""
            last_tool_outputs_by_signature: dict[str, str] = {}
            browser_repeat_counts: dict[str, int] = {}
            blocked_tool_counts: dict[str, int] = {}
            policy_repeat_recoveries: set[str] = set()
            browser_repeat_recoveries: set[str] = set()
            blocked_tool_recoveries: set[str] = set()
            stop_requested = False
            plain_completion_reprompts = 0
            pending_final_reprompts = 0
            pending_final_text = ""
            consecutive_text_only = 0
            force_tool_choice_next = False
            parallel_semaphore = asyncio.Semaphore(self.max_parallel_tool_calls)

            async def _cancel_active_tool_tasks() -> None:
                """Cancel and drain invocation tasks created for this turn."""
                tasks = list(active_tool_tasks.values())
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                active_tool_tasks.clear()

            def _mark_active_tool_calls_cancelled() -> None:
                cancelled_output = json.dumps(
                    {
                        "status": "cancelled",
                        "reason_code": "user_stopped",
                        "error": "Stopped by user before the tool finished.",
                    },
                    ensure_ascii=False,
                )
                for active_call in active_tool_calls.values():
                    completed = bool(active_call.get("completed"))
                    if not completed:
                        active_call.update({"output": cancelled_output, "status": "cancelled"})
                    if active_call.get("observability_finished"):
                        continue
                    if completed:
                        observed_status = "ok" if active_call.get("status") == "complete" else "error"
                    else:
                        observed_status = "cancelled"
                    observed_output = str(active_call.get("output") or cancelled_output)
                    _obs_tool_end(
                        str(active_call.get("tool_name") or "tool"),
                        observed_output,
                        observed_status,
                        str(active_call.get("call_id") or ""),
                        float(active_call.get("obs_tool_started_at") or time.perf_counter()),
                        error_code="" if observed_status in {"ok", "complete"} else "cancelled",
                        duration_ms=(
                            int(active_call["duration_ms"])
                            if completed and active_call.get("duration_ms") is not None
                            else None
                        ),
                    )
                    active_call["observability_finished"] = True

            async def _invoke_prepared_tool(prepared: dict) -> ToolCallResult:
                """Invoke one preflighted tool, converting ordinary failures to results."""
                tool_name = str(prepared["tool_name"])
                call_id = str(prepared["call_id"])
                try:
                    async with parallel_semaphore:
                        _transition(run_state.tool_execution)
                        result = await self._execute_tool_result(
                            budget,
                            prepared["tool_dict"],
                            prepared["arguments"],
                            call_id=call_id,
                        )
                        completed_at = time.perf_counter()
                        active_call = active_tool_calls.get(prepared["active_key"])
                        if active_call is not None:
                            active_call.update(
                                {
                                    "output": result.output,
                                    "status": "complete" if result.status == "ok" else result.status,
                                    "completed": result.status not in {"pending_approval", "pending_access_grant"},
                                    "duration_ms": round(
                                        (completed_at - float(active_call.get("obs_tool_started_at") or completed_at))
                                        * 1000
                                    ),
                                }
                            )
                        return result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    tool_output = json.dumps(
                        {"status": "error", "error": str(exc)}
                    )
                    _obs_error(
                        str(exc),
                        error_type=type(exc).__name__,
                        metadata={"phase": "tool_call", "tool": tool_name, "call_id": call_id},
                    )
                    result = ToolCallResult.from_output(
                        call_id=call_id,
                        name=tool_name,
                        output=tool_output,
                        fallback_status="error",
                    )
                    active_call = active_tool_calls.get(prepared["active_key"])
                    if active_call is not None:
                        completed_at = time.perf_counter()
                        active_call.update(
                            {
                                "output": result.output,
                                "status": "error",
                                "completed": result.status not in {"pending_approval", "pending_access_grant"},
                                "duration_ms": round(
                                    (completed_at - float(active_call.get("obs_tool_started_at") or completed_at))
                                    * 1000
                                ),
                            }
                        )
                    return result

            async def _run_prepared_batch(prepared_calls: list[dict], *, parallel: bool) -> list[ToolCallResult]:
                """Run a prepared batch while retaining task ownership for cancellation."""
                tasks: list[asyncio.Task] = []
                for prepared in prepared_calls:
                    task = asyncio.create_task(
                        _invoke_prepared_tool(prepared),
                        name=f"turn-tool-{prepared['call_id']}",
                    )
                    active_tool_tasks[prepared["active_key"]] = task
                    tasks.append(task)

                if not parallel:
                    # A serial barrier still uses a tracked task so a turn
                    # cancellation can drain it through the same path.
                    result = await tasks[0]
                    active_tool_tasks.pop(prepared_calls[0]["active_key"], None)
                    return [result]

                values = await asyncio.gather(*tasks, return_exceptions=True)
                results: list[ToolCallResult] = []
                for prepared, value in zip(prepared_calls, values):
                    active_tool_tasks.pop(prepared["active_key"], None)
                    if isinstance(value, asyncio.CancelledError):
                        raise value
                    if isinstance(value, BaseException):
                        # The invocation wrapper normally converts exceptions,
                        # but isolate a failure from its sibling if a future
                        # error escapes that boundary.
                        tool_name = str(prepared["tool_name"])
                        output = json.dumps({"status": "error", "error": str(value)})
                        _obs_error(
                            str(value),
                            error_type=type(value).__name__,
                            metadata={
                                "phase": "tool_call",
                                "tool": tool_name,
                                "call_id": str(prepared["call_id"]),
                            },
                        )
                        value = ToolCallResult.from_output(
                            call_id=str(prepared["call_id"]),
                            name=tool_name,
                            output=output,
                            fallback_status="error",
                        )
                        active_call = active_tool_calls.get(prepared["active_key"])
                        if active_call is not None:
                            active_call.update(
                                {
                                    "output": output,
                                    "status": "error",
                                    "completed": True,
                                    "duration_ms": round(
                                        (time.perf_counter() - float(active_call.get("obs_tool_started_at") or time.perf_counter()))
                                        * 1000
                                    ),
                                }
                            )
                    results.append(value)
                return results

            async def _process_tool_result(prepared: dict, tool_result: ToolCallResult) -> None:
                """Apply gate handling and all existing ordered result guards."""
                nonlocal final_text, stop_requested, terminal_error, incomplete_reason_code

                call_id = str(prepared["call_id"])
                step_id = str(prepared["step_id"])
                plan_step = prepared["plan_step"]
                tool_name = str(prepared["tool_name"])
                arguments = prepared["arguments"]
                policy_decision = prepared["policy_decision"]
                tool_dict = prepared["tool_dict"]
                status = "ok" if tool_result.status == "ok" else "error"
                tool_output = tool_result.output

                for _gate_hop in range(self.execution_gate.MAX_GATE_HOPS):
                    pending_event = self._check_pending_status(tool_output)
                    if pending_event is None:
                        break
                    _transition(run_state.waiting_for, str(pending_event.get("event") or ""))
                    _put(pending_event)
                    ticket_id = pending_event["data"]["ticket_id"]
                    resolved_output: str | None = None
                    async for resolution_event in self._await_ticket_resolution(
                        budget,
                        ticket_id,
                        pending_event["event"],
                        tool_dict,
                        arguments,
                    ):
                        if "_result" in resolution_event:
                            resolved_output = resolution_event["_result"]
                        else:
                            _put(resolution_event)
                    tool_output = resolved_output or json.dumps(
                        {"status": "error", "error": "Resolution lost."}
                    )

                status = self._tool_output_status(tool_output, status)
                recovery_prompt = ""
                blocked_key = _blocked_tool_key(tool_name, arguments, tool_output)
                if blocked_key is not None:
                    blocked_tool_counts[blocked_key] = blocked_tool_counts.get(blocked_key, 0) + 1
                    if blocked_tool_counts[blocked_key] >= 2:
                        status = "error"
                        blocked_payload = _parse_tool_json(tool_output)
                        if blocked_key not in blocked_tool_recoveries:
                            blocked_tool_recoveries.add(blocked_key)
                            policy_repeat_recoveries.add(tool_call_signature(tool_name, arguments))
                            tool_output = json.dumps(
                                {
                                    "status": "error",
                                    "code": "repeated_blocked_tool_result",
                                    "tool": tool_name,
                                    "arguments": arguments,
                                    "recovery_required": True,
                                    "error": (
                                        "The same tool action was blocked repeatedly. "
                                        "Use a different permitted action, ask for permission changes, "
                                        "or report the blocker."
                                    ),
                                    "last_result": blocked_payload,
                                },
                                ensure_ascii=False,
                            )
                            recovery_prompt = _build_repeat_recovery_prompt(
                                reason_code="repeated_blocked_tool_result",
                                tool_name=tool_name,
                                arguments=arguments,
                                last_result=blocked_payload,
                            )
                            logger.info(
                                "turn_loop prompted recovery after repeated blocked tool result "
                                "conversation_id=%s tool=%s arguments=%s",
                                conversation_id,
                                tool_name,
                                arguments,
                            )
                            _obs_event(
                                "guardrail_triggered",
                                level="warning",
                                status="error",
                                tool_name=tool_name,
                                error_code="repeated_blocked_tool_result",
                                error_message="Repeated blocked tool result; recovery prompt inserted.",
                                input={"arguments": arguments, "call_id": call_id},
                                output={"last_result": blocked_payload},
                                metadata={"recovery_required": True},
                            )
                        else:
                            stop_requested = True
                            terminal_error = True
                            incomplete_reason_code = "repeated_blocked_tool_result"
                            tool_output = json.dumps(
                                {
                                    "status": "error",
                                    "code": "repeated_blocked_tool_result",
                                    "tool": tool_name,
                                    "arguments": arguments,
                                    "error": (
                                        "The same tool action was blocked repeatedly after a recovery prompt. "
                                        "Ask for permission changes or choose a different action before continuing."
                                    ),
                                    "last_result": blocked_payload,
                                },
                                ensure_ascii=False,
                            )
                            final_text = (
                                "Stopped: the same tool action was blocked repeatedly after a recovery prompt. "
                                "Adjust the permission settings or choose another action before continuing."
                            )
                            logger.warning(
                                "turn_loop stopped: repeated blocked tool result after recovery "
                                "conversation_id=%s tool=%s arguments=%s",
                                conversation_id,
                                tool_name,
                                arguments,
                            )
                            _obs_event(
                                "guardrail_triggered",
                                level="warning",
                                status="error",
                                tool_name=tool_name,
                                error_code="repeated_blocked_tool_result",
                                error_message=final_text,
                                input={"arguments": arguments, "call_id": call_id},
                                output={"last_result": blocked_payload},
                                metadata={"after_recovery": True},
                            )
                if _is_browser_observation_action(tool_name, arguments) and status == "ok":
                    browser_repeat_counts.clear()
                if _is_browser_repeat_guarded(tool_name, arguments) and status == "ok":
                    repeat_key = _repeat_key(tool_name, arguments, tool_output)
                    browser_repeat_counts[repeat_key] = browser_repeat_counts.get(repeat_key, 0) + 1
                    if browser_repeat_counts[repeat_key] >= 2:
                        status = "error"
                        repeated_browser_result = tool_output
                        if repeat_key not in browser_repeat_recoveries:
                            browser_repeat_recoveries.add(repeat_key)
                            policy_repeat_recoveries.add(tool_call_signature(tool_name, arguments))
                            tool_output = json.dumps(
                                {
                                    "status": "error",
                                    "code": "stalled_repeat_detected",
                                    "tool": tool_name,
                                    "arguments": arguments,
                                    "recovery_required": True,
                                    "error": (
                                        "Repeated the same browser action with the same arguments "
                                        "and unchanged page state. Inspect status/tabs/snapshot or report the blocker."
                                    ),
                                },
                                ensure_ascii=False,
                            )
                            recovery_prompt = _build_repeat_recovery_prompt(
                                reason_code="stalled_repeat_detected",
                                tool_name=tool_name,
                                arguments=arguments,
                                last_result=repeated_browser_result,
                            )
                            logger.info(
                                "turn_loop prompted recovery after repeated browser action "
                                "conversation_id=%s tool=%s arguments=%s",
                                conversation_id,
                                tool_name,
                                arguments,
                            )
                            _obs_event(
                                "guardrail_triggered",
                                level="warning",
                                status="error",
                                tool_name=tool_name,
                                error_code="stalled_repeat_detected",
                                error_message="Repeated browser action with unchanged page state; recovery prompt inserted.",
                                input={"arguments": arguments, "call_id": call_id},
                                output={"last_result": repeated_browser_result},
                                metadata={"recovery_required": True},
                            )
                        else:
                            stop_requested = True
                            terminal_error = True
                            incomplete_reason_code = "stalled_repeat_detected"
                            tool_output = json.dumps(
                                {
                                    "status": "error",
                                    "code": "stalled_repeat_detected",
                                    "tool": tool_name,
                                    "arguments": arguments,
                                    "error": (
                                        "Repeated the same browser action with the same arguments "
                                        "and unchanged page state after a recovery prompt. "
                                        "Inspect the browser status/snapshot or adjust the plan before continuing."
                                    ),
                                },
                                ensure_ascii=False,
                            )
                            final_text = (
                                "Stopped: repeated browser action with unchanged page state after a recovery prompt. "
                                "Inspect the browser status/snapshot or adjust the plan before continuing."
                            )
                            logger.warning(
                                "turn_loop stopped: repeated browser action after recovery "
                                "conversation_id=%s tool=%s arguments=%s",
                                conversation_id,
                                tool_name,
                                arguments,
                            )
                            _obs_event(
                                "guardrail_triggered",
                                level="warning",
                                status="error",
                                tool_name=tool_name,
                                error_code="stalled_repeat_detected",
                                error_message=final_text,
                                input={"arguments": arguments, "call_id": call_id},
                                output={"last_result": repeated_browser_result},
                                metadata={"after_recovery": True},
                            )

                raw_tool_output = tool_output
                active_call = active_tool_calls.get(prepared["active_key"])
                if active_call is not None:
                    active_call.update(
                        {
                            "output": raw_tool_output,
                            "status": "complete" if status == "ok" else status,
                        }
                    )
                image_attachments = _extract_image_attachments(tool_name, raw_tool_output)
                tool_result = ToolCallResult.from_output(
                    call_id=call_id,
                    name=tool_name,
                    output=raw_tool_output,
                    fallback_status="ok" if status == "ok" else "error",
                    metadata={"policy": policy_decision.metadata},
                )
                result_signature = str(
                    policy_decision.metadata.get("signature")
                    or tool_call_signature(tool_name, arguments)
                )
                last_tool_outputs_by_signature[result_signature] = raw_tool_output

                _put({
                    "event": "tool_end",
                    "data": {
                        "tool": tool_name,
                        "output": raw_tool_output,
                        "status": status,
                        "call_id": call_id,
                        "risk": policy_decision.risk,
                    },
                })
                _obs_tool_end(
                    tool_name,
                    raw_tool_output,
                    status,
                    call_id,
                    prepared["obs_tool_started_at"],
                    risk=policy_decision.risk,
                    error_code=_tool_error_code(raw_tool_output) if status != "ok" else "",
                    duration_ms=(
                        int(active_call["duration_ms"])
                        if active_call is not None and active_call.get("duration_ms") is not None
                        else None
                    ),
                )
                if active_call is not None:
                    active_call["observability_finished"] = True
                if plan_step is not None:
                    verified = status == "ok"
                    plan_step.status = "done" if verified else "failed"
                    if callable(sync_plan_progress):
                        sync_plan_progress(conversation_id, execution_plan)
                    _put({
                        "event": "observation",
                        "data": {
                            "step_id": step_id,
                            "verified": verified,
                            "detail": _compact_tool_observation(tool_name, raw_tool_output) if verified else raw_tool_output,
                        },
                    })
                    _put({
                        "event": "step_complete",
                        "data": {
                            "step_id": step_id,
                            "status": "done" if verified else "failed",
                        },
                    })

                self.memory.add_tool_outcome(conversation_id, tool_name, raw_tool_output)
                persisted_tool_calls.append(
                    {
                        "tool_name": tool_name,
                        "input": json.dumps(arguments, ensure_ascii=False),
                        "output": raw_tool_output,
                        "status": "complete" if status == "ok" else status,
                    }
                )
                active_tool_calls.pop(prepared["active_key"], None)
                message_entry = build_tool_result_message(
                    tool_result,
                    provider=str(getattr(self.llm_client, "provider", "") or ""),
                )
                if image_attachments:
                    message_entry["images"] = image_attachments
                    message_entry["ephemeral"] = True
                messages.append(message_entry)
                if recovery_prompt:
                    messages.append({"role": "user", "content": recovery_prompt})

            async def _flush_prepared_tool_calls(prepared_calls: list[dict]) -> None:
                """Invoke and process the calls collected before a serial barrier."""
                if not prepared_calls:
                    return
                calls = list(prepared_calls)
                prepared_calls.clear()
                results = await _run_prepared_batch(
                    calls,
                    parallel=len(calls) > 1,
                )
                for prepared, result in zip(calls, results):
                    await _process_tool_result(prepared, result)

            for iteration_index in range(self._max_iterations):
                if stop_requested:
                    break
                if not budget.consume():
                    final_text = "Iteration budget exhausted."
                    budget_exhausted = True
                    iteration_limit_hit = True
                    incomplete_reason_code = "iteration_budget_exhausted"
                    logger.warning(
                        "turn_loop paused: iteration budget exhausted before model call "
                        "conversation_id=%s limit=%s consumed=%s",
                        conversation_id,
                        budget.max_iterations,
                        budget.consumed,
                    )
                    _put({
                        "event": "error",
                        "data": {
                            "code": "iteration_budget_exhausted",
                            "message": final_text,
                            "limit": budget.max_iterations,
                            "consumed": budget.consumed,
                        },
                    })
                    _obs_event(
                        "guardrail_triggered",
                        level="warning",
                        status="error",
                        error_code="iteration_budget_exhausted",
                        error_message=final_text,
                        metadata={"limit": budget.max_iterations, "consumed": budget.consumed},
                    )
                    break

                streamed_answer_text = ""

                async def _answer_token_cb(delta: str, _put=_put) -> None:
                    nonlocal streamed_answer_text
                    streamed_answer_text += str(delta or "")
                    _emit_response_token(delta)

                llm_call_started_at = time.perf_counter()
                try:
                    _transition(run_state.model_call)
                    visible_tools = self.registry.get_all_tools(visible_only=True)
                    model_tools = _tools_with_final_answer(
                        visible_tools,
                        enabled=structured_final_required,
                    )
                    stream_callback = (
                        None
                        if structured_final_required
                        else _answer_token_cb
                    )
                    _obs_event(
                        "llm_call_started",
                        input={
                            "message_count": len(messages),
                            "tool_count": len(model_tools),
                            "tool_choice": "required" if force_tool_choice_next else "auto",
                        },
                        metadata={"iteration": iteration_index + 1, "consumed": budget.consumed},
                    )
                    if force_tool_choice_next:
                        logger.info(
                            "turn_loop forcing tool_choice after repeated text-only responses "
                            "conversation_id=%s consecutive_text_only=%s consumed=%s",
                            conversation_id,
                            consecutive_text_only,
                            budget.consumed,
                        )
                        llm_call = self.llm_client.chat_with_tools(
                            messages,
                            model_tools,
                            system_prompt=_current_system_prompt(),
                            stream_callback=stream_callback,
                            tool_choice="required",
                        )
                    else:
                        llm_call = self.llm_client.chat_with_tools(
                            messages,
                            model_tools,
                            system_prompt=_current_system_prompt(),
                            stream_callback=stream_callback,
                        )
                    force_tool_choice_next = False
                    llm_response = await asyncio.wait_for(
                        llm_call,
                        timeout=self.max_llm_call_seconds,
                    )
                    llm_usage = usage_from_any(getattr(llm_response, "usage", None))
                    obs_usage_total = obs_usage_total.merge(llm_usage)
                    _obs_event(
                        "llm_call_finished",
                        status="ok",
                        duration_ms=round((time.perf_counter() - llm_call_started_at) * 1000),
                        output={
                            "finish_reason": llm_response.finish_reason,
                            "content": llm_response.content,
                            "tool_calls": [
                                {
                                    "call_id": call.call_id,
                                    "tool_name": call.tool_name,
                                    "arguments": call.arguments,
                                }
                                for call in llm_response.tool_calls
                            ],
                        },
                        tokens=llm_usage,
                        metadata={"iteration": iteration_index + 1},
                    )
                except asyncio.TimeoutError:
                    final_text = f"Timed out: LLM call exceeded {self.max_llm_call_seconds}s limit."
                    terminal_error = True
                    incomplete_reason_code = "llm_timeout"
                    logger.warning(
                        "turn_loop paused: llm call timed out conversation_id=%s timeout_s=%s consumed=%s",
                        conversation_id,
                        self.max_llm_call_seconds,
                        budget.consumed,
                    )
                    _put({
                        "event": "error",
                        "data": {"code": "llm_timeout", "message": final_text},
                    })
                    _obs_event(
                        "llm_call_finished",
                        level="warning",
                        status="error",
                        duration_ms=round((time.perf_counter() - llm_call_started_at) * 1000),
                        error_code="llm_timeout",
                        error_message=final_text,
                        metadata={"timeout_s": self.max_llm_call_seconds, "iteration": iteration_index + 1},
                    )
                    break
                except Exception as exc:
                    final_text = f"Provider error: {exc}"
                    terminal_error = True
                    incomplete_reason_code = "provider_error"
                    logger.exception("turn_loop provider call failed conversation_id=%s", conversation_id)
                    _obs_error(
                        str(exc),
                        error_type=type(exc).__name__,
                        metadata={"phase": "llm_call", "iteration": iteration_index + 1},
                    )
                    _obs_event(
                        "llm_call_finished",
                        level="error",
                        status="error",
                        duration_ms=round((time.perf_counter() - llm_call_started_at) * 1000),
                        error_code="provider_error",
                        error_message=str(exc),
                        metadata={"iteration": iteration_index + 1},
                    )
                    _put({
                        "event": "error",
                        "data": {"code": "provider_error", "message": final_text},
                    })
                    break

                assistant_content = llm_response.content or ""
                reasoning_content = str(getattr(llm_response, "reasoning_content", "") or "")
                if reasoning_content:
                    _put({"event": "thinking", "data": {"content": reasoning_content}})

                if not llm_response.tool_calls:
                    final_text = assistant_content.strip() or "Done."
                    if llm_response.finish_reason == "length":
                        terminal_error = True
                        incomplete_reason_code = "model_output_truncated"
                        if final_text == "Done.":
                            final_text = (
                                "Paused: the model output was truncated before a final answer. "
                                "Continue to resume from the current state."
                            )
                        logger.warning(
                            "turn_loop paused: model output truncated conversation_id=%s consumed=%s",
                            conversation_id,
                            budget.consumed,
                        )
                        _put({
                            "event": "error",
                            "data": {
                                "code": incomplete_reason_code,
                                "message": final_text,
                                "limit": budget.max_iterations,
                                "consumed": budget.consumed,
                            },
                        })
                        _obs_event(
                            "guardrail_triggered",
                            level="warning",
                            status="error",
                            error_code=incomplete_reason_code,
                            error_message=final_text,
                            metadata={"limit": budget.max_iterations, "consumed": budget.consumed},
                        )
                    else:
                        final_tool_missing = structured_final_required and visible_tools
                        if final_tool_missing:
                            json_final_text = _json_text_as_final_answer(assistant_content)
                            if json_final_text is not None:
                                final_text = json_final_text
                                messages.append({"role": "assistant", "content": assistant_content})
                                completed_normally = True
                                logger.info(
                                    "turn_loop completed from plain JSON final_answer content "
                                    "conversation_id=%s consumed=%s tool_calls=%s finish_reason=%s",
                                    conversation_id,
                                    budget.consumed,
                                    len(persisted_tool_calls),
                                    llm_response.finish_reason,
                                )
                                break
                            plain_completion_text = _plain_text_as_completion(assistant_content)
                            if plain_completion_text is not None:
                                messages.append({"role": "assistant", "content": assistant_content})
                                if plain_completion_reprompts == 0 and budget.remaining > 0:
                                    pending_final_text = plain_completion_text
                                    plain_completion_reprompts += 1
                                    logger.info(
                                        "turn_loop reprompting after explicit plain-text completion "
                                        "conversation_id=%s consumed=%s remaining=%s finish_reason=%s content=%r",
                                        conversation_id,
                                        budget.consumed,
                                        budget.remaining,
                                        llm_response.finish_reason,
                                        assistant_content[:160],
                                    )
                                    messages.append({"role": "user", "content": _FINAL_ANSWER_REPROMPT_MESSAGE})
                                    continue
                                final_text = plain_completion_text
                                completed_normally = True
                                logger.info(
                                    "turn_loop completed from repeated explicit plain-text completion "
                                    "conversation_id=%s consumed=%s tool_calls=%s finish_reason=%s",
                                    conversation_id,
                                    budget.consumed,
                                    len(persisted_tool_calls),
                                    llm_response.finish_reason,
                                )
                                break
                            pending_plain_final_text = _plain_text_as_pending_final(assistant_content)
                            if pending_plain_final_text is not None:
                                pending_final_text = pending_plain_final_text
                                pending_final_reprompts += 1
                                _emit_agent_progress(assistant_content)
                                messages.append({"role": "assistant", "content": assistant_content})
                                if pending_final_reprompts == 1 and budget.remaining > 0:
                                    logger.info(
                                        "turn_loop reprompting after pending plain-text final answer "
                                        "conversation_id=%s consumed=%s remaining=%s finish_reason=%s content=%r",
                                        conversation_id,
                                        budget.consumed,
                                        budget.remaining,
                                        llm_response.finish_reason,
                                        assistant_content[:160],
                                    )
                                    messages.append({"role": "user", "content": _FINAL_ANSWER_REPROMPT_MESSAGE})
                                    continue
                                final_text = pending_final_text
                                completed_normally = True
                                logger.warning(
                                    "turn_loop completing from pending plain-text final after repeated text-only "
                                    "conversation_id=%s consumed=%s tool_calls=%s finish_reason=%s",
                                    conversation_id,
                                    budget.consumed,
                                    len(persisted_tool_calls),
                                    llm_response.finish_reason,
                                )
                                break
                            consecutive_text_only += 1
                            _emit_agent_progress(assistant_content)
                            if assistant_content:
                                messages.append({"role": "assistant", "content": assistant_content})
                            if consecutive_text_only >= _TEXT_ONLY_CIRCUIT_BREAK_AFTER:
                                completed_normally = True
                                logger.warning(
                                    "turn_loop force-completing after repeated text-only responses "
                                    "conversation_id=%s consecutive_text_only=%s consumed=%s "
                                    "tool_calls=%s finish_reason=%s",
                                    conversation_id,
                                    consecutive_text_only,
                                    budget.consumed,
                                    len(persisted_tool_calls),
                                    llm_response.finish_reason,
                                )
                                break
                            if consecutive_text_only >= _TEXT_ONLY_FORCE_AFTER:
                                force_tool_choice_next = True
                                logger.info(
                                    "turn_loop escalating text-only response to required tool choice "
                                    "conversation_id=%s consecutive_text_only=%s consumed=%s "
                                    "remaining=%s finish_reason=%s content=%r",
                                    conversation_id,
                                    consecutive_text_only,
                                    budget.consumed,
                                    budget.remaining,
                                    llm_response.finish_reason,
                                    assistant_content[:160],
                                )
                                messages.append({"role": "user", "content": _TOOL_FORCE_MESSAGE})
                                continue
                            logger.info(
                                "turn_loop continuing after progress text without tool call "
                                "conversation_id=%s consecutive_text_only=%s consumed=%s "
                                "remaining=%s finish_reason=%s content=%r",
                                conversation_id,
                                consecutive_text_only,
                                budget.consumed,
                                budget.remaining,
                                llm_response.finish_reason,
                                assistant_content[:160],
                            )
                            messages.append({"role": "user", "content": _TOOL_REPROMPT_MESSAGE})
                            continue
                        else:
                            if assistant_content:
                                messages.append({"role": "assistant", "content": assistant_content})
                            completed_normally = True
                            logger.info(
                                "turn_loop completed normally conversation_id=%s consumed=%s tool_calls=%s finish_reason=%s",
                                conversation_id,
                                budget.consumed,
                                len(persisted_tool_calls),
                                llm_response.finish_reason,
                            )
                    break

                consecutive_text_only = 0
                if assistant_content:
                    _emit_agent_progress(assistant_content)
                provider_messages = list(getattr(llm_response, "provider_messages", []) or [])
                if provider_messages:
                    messages.extend(provider_messages)
                elif assistant_content:
                    messages.append({"role": "assistant", "content": assistant_content})

                call_dicts = []
                final_answer_call = None
                for index, call in enumerate(llm_response.tool_calls, start=1):
                    arguments = call.arguments if isinstance(call.arguments, dict) else {}
                    if call.tool_name == FINAL_ANSWER_TOOL_NAME:
                        final_answer_call = call
                        continue
                    call_dicts.append(
                        {
                            "call_id": call.call_id or f"tool-{index}",
                            "name": call.tool_name,
                            "arguments": arguments,
                            "step_id": f"tool-{index}",
                        }
                    )
                if final_answer_call is not None and not call_dicts:
                    final_args = final_answer_call.arguments if isinstance(final_answer_call.arguments, dict) else {}
                    final_text = _final_answer_text(final_args, llm_response.content)
                    if _should_use_pending_final_text(final_text, pending_final_text):
                        logger.warning(
                            "turn_loop replacing generic final_answer with pending plain-text final "
                            "conversation_id=%s consumed=%s final_len=%s pending_len=%s",
                            conversation_id,
                            budget.consumed,
                            len(final_text),
                            len(pending_final_text),
                        )
                        final_text = pending_final_text
                    candidate_attachment_paths = _final_answer_attachment_paths(final_args)
                    if (
                        budget.remaining > 0
                        and not _final_answer_includes_web_search_results(final_text, persisted_tool_calls)
                    ):
                        logger.info(
                            "turn_loop reprompting generic web_search final answer "
                            "conversation_id=%s consumed=%s remaining=%s",
                            conversation_id,
                            budget.consumed,
                            budget.remaining,
                        )
                        messages.append({"role": "user", "content": _WEB_SEARCH_RESULT_REPROMPT_MESSAGE})
                        final_text = ""
                        pending_final_text = ""
                        final_attachment_paths = []
                        continue
                    final_attachment_paths = candidate_attachment_paths
                    pending_final_text = ""
                    completed_normally = True
                    logger.info(
                        "turn_loop completed via final_answer conversation_id=%s consumed=%s tool_calls=%s",
                        conversation_id,
                        budget.consumed,
                        len(persisted_tool_calls),
                    )
                    break
                if final_answer_call is not None:
                    logger.info(
                        "turn_loop ignored final_answer because real tools were also requested conversation_id=%s tools=%s",
                        conversation_id,
                        [str(call.get("name") or "") for call in call_dicts],
                    )
                if call_dicts:
                    pending_final_text = ""
                if budget.remaining <= 0:
                    iteration_limit_hit = True
                    budget_exhausted = True
                    incomplete_reason_code = "iteration_limit_reached_before_more_tools"
                    final_text = (
                        "Paused before executing more tools because the iteration limit is reached. "
                        "Continue to proceed from the current state."
                    )
                    logger.warning(
                        "turn_loop paused: iteration limit reached before more tools "
                        "conversation_id=%s limit=%s consumed=%s requested_tools=%s",
                        conversation_id,
                        budget.max_iterations,
                        budget.consumed,
                        [str(call.get("name") or "") for call in call_dicts],
                    )
                    _put({
                        "event": "error",
                        "data": {
                            "code": incomplete_reason_code,
                            "message": final_text,
                            "limit": budget.max_iterations,
                            "consumed": budget.consumed,
                        },
                    })
                    _obs_event(
                        "guardrail_triggered",
                        level="warning",
                        status="error",
                        error_code=incomplete_reason_code,
                        error_message=final_text,
                        metadata={
                            "limit": budget.max_iterations,
                            "consumed": budget.consumed,
                            "requested_tools": [str(call.get("name") or "") for call in call_dicts],
                        },
                    )
                    break
                execution_plan = self._build_execution_plan(message, call_dicts)
                plan_by_step_id = {step.step_id: step for step in execution_plan.steps}
                _put({"event": "plan", "data": execution_plan.to_dict()})
                sync_plan_progress = getattr(self.memory, "sync_plan_progress", None)
                if callable(sync_plan_progress):
                    sync_plan_progress(conversation_id, execution_plan)

                for batch in can_parallelize(call_dicts, self.registry.get_tool):
                    prepared_tool_calls: list[dict] = []
                    for tool_call in batch:
                        if stop_requested:
                            break
                        step_id = str(tool_call.get("step_id") or "")
                        call_id = str(tool_call.get("call_id") or step_id or tool_call.get("name") or "tool-call")
                        plan_step = plan_by_step_id.get(step_id)
                        if plan_step is not None:
                            plan_step.status = "active"
                            if callable(sync_plan_progress):
                                sync_plan_progress(conversation_id, execution_plan)
                            _put({
                                "event": "step_start",
                                "data": {
                                    "step_id": step_id,
                                    "description": plan_step.description,
                                },
                            })

                        raw_request = HarnessToolCallRequest(
                            call_id=call_id,
                            name=str(tool_call["name"]),
                            arguments=tool_call["arguments"],
                            step_id=step_id or None,
                        )
                        last_tool_name = raw_request.name
                        dispatched = self.registry.dispatch(raw_request.name, raw_request.arguments)
                        if dispatched is None:
                            await _flush_prepared_tool_calls(prepared_tool_calls)
                            if stop_requested:
                                break
                            policy_decision = self.policy.decide(
                                request=raw_request,
                                registry=self.registry,
                                arguments=raw_request.arguments,
                            )
                            tool_output = json.dumps(
                                {
                                    "status": "error",
                                    "code": policy_decision.metadata.get("code", "unknown_tool"),
                                    "error": policy_decision.reason or f"Unknown tool '{raw_request.name}'",
                                }
                            )
                            obs_tool_started_at = _obs_tool_start(
                                raw_request.name,
                                raw_request.arguments,
                                call_id,
                                policy_decision.risk,
                            )
                            _put({
                                "event": "tool_start",
                                "data": {
                                    "tool": raw_request.name,
                                    "input": raw_request.arguments,
                                    "call_id": call_id,
                                    "risk": policy_decision.risk,
                                },
                            })
                            _put({
                                "event": "tool_end",
                                "data": {
                                    "tool": raw_request.name,
                                    "output": tool_output,
                                    "status": "error",
                                    "call_id": call_id,
                                },
                            })
                            _obs_tool_end(
                                raw_request.name,
                                tool_output,
                                "error",
                                call_id,
                                obs_tool_started_at,
                                risk=policy_decision.risk,
                                error_code=policy_decision.metadata.get("code", "unknown_tool"),
                            )
                            if plan_step is not None:
                                plan_step.status = "failed"
                                if callable(sync_plan_progress):
                                    sync_plan_progress(conversation_id, execution_plan)
                                _put({
                                    "event": "observation",
                                    "data": {
                                        "step_id": step_id,
                                        "verified": False,
                                        "detail": tool_output,
                                    },
                                })
                                _put({
                                    "event": "step_complete",
                                    "data": {"step_id": step_id, "status": "failed"},
                                })
                            messages.append(
                                self._build_tool_result_message(
                                    call_id=call_id,
                                    tool_name=raw_request.name,
                                    tool_output=tool_output,
                                    status="error",
                                )
                            )
                            continue

                        tool_dict = dispatched["tool"]
                        arguments = dispatched["arguments"]
                        validation_errors = dispatched.get("validation_errors") or []
                        tool_name = str(tool_dict["name"])
                        last_tool_name = tool_name
                        if validation_errors:
                            await _flush_prepared_tool_calls(prepared_tool_calls)
                            if stop_requested:
                                break
                            tool_output = json.dumps(
                                {
                                    "status": "error",
                                    "reason_code": "invalid_tool_arguments",
                                    "error": "; ".join(validation_errors),
                                    "expected_schema": tool_dict.get("parameters", {}),
                                }
                            )
                            obs_tool_started_at = _obs_tool_start(
                                tool_name,
                                arguments,
                                call_id,
                                "low",
                            )
                            _put({
                                "event": "tool_start",
                                "data": {
                                    "tool": tool_name,
                                    "input": arguments,
                                    "call_id": call_id,
                                    "risk": "low",
                                },
                            })
                            _put({
                                "event": "tool_end",
                                "data": {
                                    "tool": tool_name,
                                    "output": tool_output,
                                    "status": "error",
                                    "call_id": call_id,
                                },
                            })
                            _obs_tool_end(
                                tool_name,
                                tool_output,
                                "error",
                                call_id,
                                obs_tool_started_at,
                                risk="low",
                                error_code="invalid_tool_arguments",
                            )
                            messages.append(
                                self._build_tool_result_message(
                                    call_id=call_id,
                                    tool_name=tool_name,
                                    tool_output=tool_output,
                                    status="error",
                                )
                            )
                            if plan_step is not None:
                                plan_step.status = "failed"
                                if callable(sync_plan_progress):
                                    sync_plan_progress(conversation_id, execution_plan)
                                _put({"event": "step_complete", "data": {"step_id": step_id, "status": "failed"}})
                            continue
                        request = HarnessToolCallRequest(
                            call_id=call_id,
                            name=tool_name,
                            arguments=arguments,
                            step_id=step_id or None,
                        )
                        policy_decision = self.policy.decide(
                            request=request,
                            registry=self.registry,
                            arguments=arguments,
                        )
                        if not policy_decision.allowed:
                            await _flush_prepared_tool_calls(prepared_tool_calls)
                            if stop_requested:
                                break
                            blocked_code = policy_decision.metadata.get("code", "tool_policy_blocked")
                            recovery_prompt = ""
                            tool_output = json.dumps(
                                {
                                    "status": "error",
                                    "code": blocked_code,
                                    "tool": tool_name,
                                    "arguments": arguments,
                                    "error": policy_decision.reason,
                                },
                                ensure_ascii=False,
                            )
                            _obs_event(
                                "guardrail_triggered",
                                level="warning",
                                status="error",
                                tool_name=tool_name,
                                error_code=blocked_code,
                                error_message=policy_decision.reason,
                                input={"arguments": arguments, "call_id": call_id},
                                metadata={"risk": policy_decision.risk},
                            )
                            if blocked_code == "repeated_tool_call_blocked":
                                signature = str(
                                    policy_decision.metadata.get("signature")
                                    or tool_call_signature(tool_name, arguments)
                                )
                                if signature not in policy_repeat_recoveries:
                                    policy_repeat_recoveries.add(signature)
                                    recovery_prompt = _build_repeat_recovery_prompt(
                                        reason_code="repeated_tool_call_blocked",
                                        tool_name=tool_name,
                                        arguments=arguments,
                                        last_result=last_tool_outputs_by_signature.get(signature),
                                    )
                                    logger.info(
                                        "turn_loop prompted recovery after repeated blocked tool call "
                                        "conversation_id=%s tool=%s arguments=%s",
                                        conversation_id,
                                        tool_name,
                                        arguments,
                                    )
                                else:
                                    stop_requested = True
                                    terminal_error = True
                                    incomplete_reason_code = "repeated_tool_call_blocked"
                                    final_text = (
                                        "Stopped: the model repeated a blocked tool call with the same arguments "
                                        "after a recovery prompt. Use the prior result, inspect the latest state, "
                                        "or choose a different action before continuing."
                                    )
                                    logger.warning(
                                        "turn_loop stopped: repeated blocked tool call after recovery "
                                        "conversation_id=%s tool=%s arguments=%s",
                                        conversation_id,
                                        tool_name,
                                        arguments,
                                    )
                                    _obs_event(
                                        "guardrail_triggered",
                                        level="warning",
                                        status="error",
                                        tool_name=tool_name,
                                        error_code="repeated_tool_call_blocked",
                                        error_message=final_text,
                                        input={"arguments": arguments, "call_id": call_id},
                                        output={"last_result": last_tool_outputs_by_signature.get(signature)},
                                        metadata={"after_recovery": True},
                                    )
                            obs_tool_started_at = _obs_tool_start(
                                tool_name,
                                arguments,
                                call_id,
                                policy_decision.risk,
                            )
                            _put({
                                "event": "tool_start",
                                "data": {
                                    "tool": tool_name,
                                    "input": arguments,
                                    "call_id": call_id,
                                    "risk": policy_decision.risk,
                                },
                            })
                            _put({
                                "event": "tool_end",
                                "data": {
                                    "tool": tool_name,
                                    "output": tool_output,
                                    "status": "error",
                                    "call_id": call_id,
                                    "risk": policy_decision.risk,
                                },
                            })
                            _obs_tool_end(
                                tool_name,
                                tool_output,
                                "error",
                                call_id,
                                obs_tool_started_at,
                                risk=policy_decision.risk,
                                error_code=blocked_code,
                            )
                            if plan_step is not None:
                                plan_step.status = "failed"
                                if callable(sync_plan_progress):
                                    sync_plan_progress(conversation_id, execution_plan)
                                _put({
                                    "event": "observation",
                                    "data": {
                                        "step_id": step_id,
                                        "verified": False,
                                        "detail": tool_output,
                                    },
                                })
                                _put({
                                    "event": "step_complete",
                                    "data": {"step_id": step_id, "status": "failed"},
                                })
                            self.memory.add_tool_outcome(conversation_id, tool_name, tool_output)
                            persisted_tool_calls.append(
                                {
                                    "tool_name": tool_name,
                                    "input": json.dumps(arguments, ensure_ascii=False),
                                    "output": tool_output,
                                    "status": "error",
                                }
                            )
                            messages.append(
                                self._build_tool_result_message(
                                    call_id=call_id,
                                    tool_name=tool_name,
                                    tool_output=tool_output,
                                    status="error",
                                )
                            )
                            if recovery_prompt:
                                messages.append({"role": "user", "content": recovery_prompt})
                            if stop_requested:
                                break
                            continue

                        obs_tool_started_at = _obs_tool_start(
                            tool_name,
                            arguments,
                            call_id,
                            policy_decision.risk,
                        )
                        _put({
                            "event": "tool_start",
                            "data": {
                                "tool": tool_name,
                                "input": arguments,
                                "call_id": call_id,
                                "risk": policy_decision.risk,
                            },
                        })
                        active_key = f"{call_id}:{len(active_tool_calls)}"
                        active_tool_calls[active_key] = {
                            "call_id": call_id,
                            "tool_name": tool_name,
                            "input": json.dumps(arguments, ensure_ascii=False),
                            "output": json.dumps(
                                {
                                    "status": "cancelled",
                                    "reason_code": "user_stopped",
                                    "error": "Stopped by user before the tool finished.",
                                },
                                ensure_ascii=False,
                            ),
                            "status": "cancelled",
                            "obs_tool_started_at": obs_tool_started_at,
                            "observability_finished": False,
                        }
                        prepared_tool_calls.append(
                            {
                                "active_key": active_key,
                                "call_id": call_id,
                                "step_id": step_id,
                                "plan_step": plan_step,
                                "tool_name": tool_name,
                                "arguments": arguments,
                                "tool_dict": tool_dict,
                                "policy_decision": policy_decision,
                                "obs_tool_started_at": obs_tool_started_at,
                            }
                        )
                        # Invocation and result handling happen after all
                        # calls in this contiguous batch have passed the
                        # preflight checks, allowing safe calls to overlap.
                        continue

                    if prepared_tool_calls:
                        await _flush_prepared_tool_calls(prepared_tool_calls)
                    if stop_requested:
                        break

            if (
                not completed_normally
                and not stop_requested
                and not budget_exhausted
                and not terminal_error
                and not iteration_limit_hit
            ):
                iteration_limit_hit = True
                budget_exhausted = True
                if persisted_tool_calls:
                    last_tool_name = str(persisted_tool_calls[-1].get("tool_name") or last_tool_name)
                if last_tool_name:
                    incomplete_reason_code = "iteration_limit_reached_after_tool"
                    final_text = (
                        "Paused: iteration limit reached after the last tool result. "
                        "The task may be incomplete. Continue to resume from the current state."
                    )
                else:
                    incomplete_reason_code = "iteration_limit_reached_without_final_answer"
                    final_text = (
                        "Paused: iteration limit reached before the agent produced a final answer. "
                        "Continue to resume from the current state."
                    )
                logger.warning(
                    "turn_loop paused: loop ended without final answer conversation_id=%s limit=%s "
                    "consumed=%s last_tool=%s",
                    conversation_id,
                    budget.max_iterations,
                    budget.consumed,
                    last_tool_name,
                )
                _put({
                    "event": "error",
                    "data": {
                        "code": incomplete_reason_code,
                        "message": final_text,
                        "limit": budget.max_iterations,
                        "consumed": budget.consumed,
                        "last_tool": last_tool_name,
                    },
                })
                _obs_event(
                    "guardrail_triggered",
                    level="warning",
                    status="error",
                    error_code=incomplete_reason_code,
                    error_message=final_text,
                    metadata={
                        "limit": budget.max_iterations,
                        "consumed": budget.consumed,
                        "last_tool": last_tool_name,
                    },
                )

            run_status = (
                "paused"
                if iteration_limit_hit or budget_exhausted or terminal_error or stop_requested
                else "complete"
            )
            if (
                final_text
                and not streamed_answer_text
                and not budget_exhausted
                and not terminal_error
                and not iteration_limit_hit
            ):
                await _emit_typewriter_tokens(_emit_response_event, final_text)
                streamed_answer_text = final_text

            response_duration_ms = _response_duration_ms()
            response_attachments = collect_response_attachments(
                content=final_text,
                explicit_paths=final_attachment_paths,
                tool_calls=persisted_tool_calls,
            )
            await _persist_turn_once(
                final_text,
                status=run_status,
                response_attachments=response_attachments,
            )
            if completed_normally and not iteration_limit_hit and not budget_exhausted:
                self.memory.clear_task_progress(conversation_id)
            self.observability.finish_run(
                run_id=obs_run_id,
                status=run_status,
                final_output=final_text or "Done.",
                failure_reason=incomplete_reason_code if run_status != "complete" else "",
                duration_ms=response_duration_ms,
                usage=obs_usage_total,
                tool_count=len(persisted_tool_calls),
                tool_error_count=sum(
                    1
                    for tool_call in persisted_tool_calls
                    if str(tool_call.get("status") or "").lower() not in {"ok", "complete", "success"}
                ),
                metadata={
                    "completed_normally": completed_normally,
                    "budget_consumed": budget.consumed,
                    "budget_limit": budget.max_iterations,
                },
            )

            done_data = {
                "conversation_id": conversation_id,
                "summary": final_text or "Done.",
                "status": run_status,
                "attachments": response_attachments,
            }
            if response_duration_ms is not None:
                done_data["response_duration_ms"] = response_duration_ms
            if iteration_limit_hit or budget_exhausted or terminal_error or stop_requested:
                done_data["incomplete"] = True
                if incomplete_reason_code:
                    done_data["reason_code"] = incomplete_reason_code
                if stop_requested:
                    _transition(run_state.cancel, incomplete_reason_code or "cancelled")
                else:
                    _transition(run_state.fail, incomplete_reason_code or "incomplete")
            else:
                _transition(run_state.complete)
            _put({
                "event": "done",
                "data": done_data,
            })

            # Per-turn long-term learning runs heuristic extraction plus blocking
            # SQLite writes. Run it after the user-visible `done` and off the event
            # loop so it neither delays completion nor stalls other conversations.
            schedule_long_term_learning = getattr(self.memory, "schedule_long_term_learning", None)
            if callable(schedule_long_term_learning):
                try:
                    await asyncio.to_thread(
                        schedule_long_term_learning,
                        conversation_id=conversation_id,
                        user_message=message,
                        assistant_message=final_text,
                    )
                except Exception:
                    logger.exception("schedule_long_term_learning failed")
        except asyncio.CancelledError:
            cleanup_active_tasks = locals().get("_cancel_active_tool_tasks")
            if callable(cleanup_active_tasks):
                await cleanup_active_tasks()
            mark_active_calls = locals().get("_mark_active_tool_calls_cancelled")
            if callable(mark_active_calls):
                mark_active_calls()
            _transition(run_state.cancel, "cancelled")
            partial_text = _assistant_message_on_cancel()
            try:
                await _persist_turn_once(partial_text, status="paused")
                self.observability.finish_run(
                    run_id=obs_run_id,
                    status="paused",
                    final_output=partial_text,
                    failure_reason="cancelled",
                    duration_ms=_response_duration_ms(),
                    usage=obs_usage_total,
                    tool_count=len(_tool_calls_for_persist()),
                    tool_error_count=sum(
                        1
                        for tool_call in _tool_calls_for_persist()
                        if str(tool_call.get("status") or "").lower() not in {"ok", "complete", "success"}
                    ),
                )
                logger.info(
                    "turn_loop persisted cancelled turn conversation_id=%s tool_calls=%s last_tool=%s",
                    conversation_id,
                    len(_tool_calls_for_persist()),
                    last_tool_name,
                )
            except Exception:
                logger.exception(
                    "turn_loop failed to persist cancelled turn conversation_id=%s",
                    conversation_id,
                )
            raise
        except Exception as exc:
            cleanup_active_tasks = locals().get("_cancel_active_tool_tasks")
            if callable(cleanup_active_tasks):
                await cleanup_active_tasks()
            mark_active_calls = locals().get("_mark_active_tool_calls_cancelled")
            if callable(mark_active_calls):
                mark_active_calls()
            _transition(run_state.fail, "internal_error")
            logger.exception("react_worker error: %s", exc)
            _obs_error(
                str(exc),
                error_type=type(exc).__name__,
                metadata={"phase": "turn_loop"},
            )
            self.observability.finish_run(
                run_id=obs_run_id,
                status="error",
                final_output=final_text or "Error.",
                failure_reason="internal_error",
                duration_ms=_response_duration_ms(),
                usage=obs_usage_total,
                tool_count=len(_tool_calls_for_persist()),
                tool_error_count=sum(
                    1
                    for tool_call in _tool_calls_for_persist()
                    if str(tool_call.get("status") or "").lower() not in {"ok", "complete", "success"}
                ),
            )
            try:
                _put({
                    "event": "error",
                    "data": {"code": "internal_error", "message": str(exc)},
                })
                _put({
                    "event": "done",
                    "data": {"conversation_id": conversation_id, "summary": "Error."},
                })
            except Exception:
                pass
        finally:
            if obs_context_token is not None:
                reset_current_run_id(obs_context_token)
            reset_current_conversation_id(context_token)
        # Note: the None sentinel is placed by _run_worker_with_timeout, not here,
        # so timeout-injected error events arrive before the sentinel.

    async def run(
        self,
        message: str,
        conversation_id: str,
        *,
        system_prompt: str | Callable[[], str],
        attachments: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        async with _SESSION_LOCKS[conversation_id]:
            queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            last_flush: list[float] = [loop.time()]
            publisher = RunEventPublisher(queue, last_flush, loop=loop)
            stop_event = asyncio.Event()

            pump_task = asyncio.create_task(
                _heartbeat_pump(publisher, stop_event)
            )

            async def _run_worker_with_timeout():
                try:
                    await asyncio.wait_for(
                        self._react_worker(
                            message,
                            conversation_id,
                            system_prompt,
                            publisher,
                            attachments,
                        ),
                        timeout=self.max_turn_seconds,
                    )
                except asyncio.TimeoutError:
                    self.observability.log_error(
                        conversation_id=conversation_id,
                        message=f"Turn exceeded {self.max_turn_seconds}s limit.",
                        error_type="turn_timeout",
                        metadata={"timeout_s": self.max_turn_seconds},
                    )
                    self.observability.finish_open_run_for_conversation(
                        conversation_id=conversation_id,
                        status="paused",
                        failure_reason="turn_timeout",
                        final_output="Timed out.",
                    )
                    publisher.publish_nowait({
                        "event": "error",
                        "data": {"code": "turn_timeout", "message": f"Turn exceeded {self.max_turn_seconds}s limit."},
                    })
                    publisher.publish_nowait({
                        "event": "done",
                        "data": {"conversation_id": conversation_id, "summary": "Timed out."},
                    })
                finally:
                    await queue.put(None)  # sentinel always placed here

            worker_task = asyncio.create_task(_run_worker_with_timeout())

            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    last_flush[0] = loop.time()
                    yield event
            finally:
                # C4: Signal stop before cancelling so pump exits cleanly via wait_for.
                stop_event.set()
                pump_task.cancel()
                worker_task.cancel()
                try:
                    await asyncio.gather(pump_task, worker_task, return_exceptions=True)
                except BaseException:
                    # CancelledError is BaseException in Python 3.8+; swallow here
                    # so teardown always completes even if this coroutine is cancelled.
                    pass
