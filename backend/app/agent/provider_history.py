"""Private, model-scoped wire history for complete Gemini turns."""

from __future__ import annotations

import base64
from copy import deepcopy
import json

from app.agent.action_security.sensitive_data import public_tool_arguments, redact_sensitive


def _replayable_messages(messages: list[dict]) -> bool:
    """Require paired tools and the same privacy boundary as visible tool history."""
    pending: list[tuple[str, str]] = []
    for message in messages:
        response = message.get("gemini_function_response")
        if isinstance(response, dict):
            if redact_sensitive(response.get("response")) != response.get("response"):
                return False
            if redact_sensitive(message.get("content", "")) != message.get("content", ""):
                return False
            match = next((index for index, (call_id, name) in enumerate(pending)
                          if name == response.get("name") and (not call_id or call_id == response.get("id"))), None)
            if match is None:
                return False
            pending.pop(match)
            continue
        # Hints may separate parallel results; a new model round cannot precede them.
        if pending and message.get("role") in {"model", "assistant"}:
            return False
        parts = message.get("gemini_parts", [])
        if not isinstance(parts, list):
            return False
        for part in parts:
            if not isinstance(part, dict):
                return False
            call = part.get("function_call", part.get("functionCall"))
            if call is None:
                continue
            if not isinstance(call, dict) or not isinstance(call.get("name"), str):
                return False
            arguments = call.get("args") or {}
            if public_tool_arguments(call["name"], arguments) != arguments:
                # Rewriting signed arguments would invalidate their native context.
                return False
            pending.append((str(call.get("id") or ""), call["name"]))
    return not pending


def _json_native(value):
    """Preserve SDK parts and binary signatures in their JSON wire representation."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=True)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    raise TypeError(f"Unsupported provider history value: {type(value).__name__}")


def prepare_provider_history(history: dict | None, user_inputs: list[tuple[str, list[dict]]]) -> dict | None:
    """Keep validated attachment references, never durable authority to reopen paths."""
    if not history:
        return None
    prepared = deepcopy(history)
    pending_inputs = iter(user_inputs)
    next_input = next(pending_inputs, None)
    for message in prepared.get("messages", []):
        message.pop("images", None)
        message.pop("ephemeral", None)
        if next_input and message.get("role") == "user" and message.get("content") == next_input[0]:
            if next_input[1]:
                message["_history_attachments"] = next_input[1]
            next_input = next(pending_inputs, None)
    return prepared


def encode_provider_history(history: dict | None, covered_message_ids: list[int]) -> str:
    """Serialize one complete journal with the database rows it replaces."""
    if not history or history.get("provider") != "gemini" or not history.get("model_name"):
        return ""
    messages = history.get("messages")
    if not isinstance(messages, list) or not any(isinstance(item, dict) and item.get("gemini_parts") for item in messages):
        return ""
    serialized = json.dumps({
        "version": 1,
        "provider": "gemini",
        "model_name": history["model_name"],
        "messages": messages,
        "covered_message_ids": sorted(set(covered_message_ids)),
    }, ensure_ascii=False, default=_json_native)
    return serialized if _replayable_messages(json.loads(serialized)["messages"]) else ""


def decode_provider_history(raw: str, *, provider: str, model_name: str, owner_id: int, after_id: int = 0) -> dict | None:
    """Use intact matching journals; legacy, compacted, or malformed rows use text."""
    if provider != "gemini" or not raw:
        return None
    try:
        history = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(history, dict) or history.get("version") != 1:
        return None
    if history.get("provider") != provider or history.get("model_name") != model_name:
        return None
    ids = history.get("covered_message_ids")
    messages = history.get("messages")
    if not isinstance(ids, list) or owner_id not in ids or any(type(item) is not int or item <= after_id for item in ids):
        return None
    if not isinstance(messages, list) or not messages or any(not isinstance(item, dict) for item in messages):
        return None
    if not _replayable_messages(messages):
        return None
    return history
