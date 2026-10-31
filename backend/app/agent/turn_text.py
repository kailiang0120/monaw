"""Text a turn writes for the model and reads back as its answer."""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Callable
from urllib.parse import urlparse

FINAL_RESPONSE_TYPEWRITER_CHARS = 8
FINAL_RESPONSE_TYPEWRITER_MAX_CHUNKS = 240

FINAL_ANSWER_TOOL_NAME = "final_answer"


def conversation_title_from_prompt(message: str) -> str:
    """Make a compact, readable title from the first line or sentence of a prompt."""
    first_line = " ".join(message.splitlines()[0].split()) if message.splitlines() else ""
    sentence_end = re.search(r"[.!?](?:\s|$)", first_line)
    if sentence_end:
        first_line = first_line[: sentence_end.start()]
    first_line = first_line.strip(" .!?\t")
    if len(first_line) <= 48:
        return first_line or "New Conversation"
    shortened = first_line[:48].rsplit(" ", 1)[0].rstrip(" ,:;-")
    return f"{shortened or first_line[:48].rstrip()}…"


def completion_protocol_prompt(system_prompt: str) -> str:
    protocol_lines = [
        "## Completion Protocol",
        "- When the task is complete, reply to the user directly. A normal assistant answer finishes the turn.",
        "- If more work is needed, call the next real tool. You may include a short progress update with that tool call.",
        "- Do not send a separate text-only progress message saying you will act later; call the tool now.",
        "- When delivering a file created by a tool, include its exact local path in the final answer.",
        "- Do not try to inspect, expand, collapse, click, or control the chat UI, reasoning trace, progress messages, message bubbles, or tool cards. Those are frontend display surfaces, not part of the task.",
        "- Do not claim that you already answered a fresh user question unless the persisted conversation history shows that exact answer.",
    ]
    protocol = "\n".join(protocol_lines)
    return f"{system_prompt}\n\n{protocol}" if system_prompt else protocol


TOOL_REPROMPT_MESSAGE = (
    "Your last message described an action you intend to take, but did not call a tool. "
    "Call the next real task tool now. If no action is needed, give the user your final answer directly."
)


WEB_SEARCH_RESULT_REPROMPT_MESSAGE = (
    "Your final answer did not include the actual web_search results. Use the web_search "
    "tool output above and provide a concrete answer with the result titles, source URLs, "
    "and key snippets. Do not just say the result is ready or visible above; include the "
    "results in your answer."
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


def final_answer_includes_web_search_results(answer: str, tool_calls: list[dict]) -> bool:
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


def emit_progress_message(emit: Callable[[dict], None], text: str) -> None:
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


async def emit_typewriter_tokens(emit: Callable[[dict], None], text: str) -> None:
    chunks = _typewriter_chunks(text)
    if not chunks:
        return
    # The client already paces rendering. Delaying these SSE events adds a
    # second playback stage between reasoning and the first visible answer.
    for chunk in chunks:
        emit({"event": "token", "data": {"content": chunk}})


def final_answer_text(arguments: dict, fallback_content: str) -> str:
    answer = str(arguments.get("answer") or arguments.get("summary") or "").strip()
    return answer or fallback_content.strip() or "Done."


def final_answer_attachment_paths(arguments: dict) -> list[str]:
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


def json_text_as_final_answer(text: str) -> str | None:
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
            return final_answer_text(arguments, content)

    function_payload = payload.get("function")
    if isinstance(function_payload, dict) and str(function_payload.get("name") or "") == FINAL_ANSWER_TOOL_NAME:
        arguments = _coerce_final_answer_args(function_payload.get("arguments"))
        if arguments is not None:
            return final_answer_text(arguments, content)

    return None


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


_PROGRESS_OPENERS = (
    "searching ",
    "checking ",
    "opening ",
    "inspecting ",
    "running ",
    "fetching ",
    "taking ",
    "still checking ",
    "still searching ",
)


def plain_text_describes_next_action(text: str) -> bool:
    """Distinguish a short tool intent from a completed natural-language answer."""
    content = str(text or "").strip()
    if not content or len(content) > 320 or len(content.splitlines()) > 2:
        return False
    last_sentence = re.split(r"(?<=[.!?])\s+", content)[-1].lower().lstrip(" \t-:—–")
    return last_sentence.startswith((*_PENDING_ACTION_PHRASES, *_PROGRESS_OPENERS))
