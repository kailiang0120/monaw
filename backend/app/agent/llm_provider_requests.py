"""Provider request conversion helpers for LLM chat calls."""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any
from app.agent.model_usage import current_output_limit

logger = logging.getLogger(__name__)


def json_schema_prop_to_gemini(prop_def: dict, types_mod):
    """Preserve supported schema fields; reject unsupported conversions explicitly."""
    return types_mod.Schema.model_validate(prop_def)


def tools_to_gemini(tools: list[dict]):
    """Pass original JSON Schemas through Gemini's native schema field."""
    from copy import deepcopy
    from google.genai import types

    declarations = [
        types.FunctionDeclaration(
            name=tool["name"],
            description=tool.get("description", ""),
            parameters_json_schema=deepcopy(tool.get("parameters", {"type": "object", "properties": {}})),
        )
        for tool in tools
    ]
    return types.Tool(function_declarations=declarations)


def tools_to_openai_responses(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            "strict": False,
        }
        for t in tools
    ]



def openai_reasoning_effort(value: str, model_name: str = "") -> str:
    effort = str(value or "").strip().lower()
    model = str(model_name or "").strip().lower()
    if model.startswith("gpt-6"):
        if effort == "none" and model in {"gpt-6-astra", "gpt-6.1-sol"}:
            return "low"
        if effort == "minimal":
            return "low"
        return effort if effort in {"none", "low", "medium", "high", "xhigh", "max"} else "medium"
    if effort in {"none", "minimal"}:
        return "minimal"
    if effort in {"low", "medium", "high"}:
        return effort
    if effort in {"xhigh", "max"}:
        return "high"
    return "medium"


def openai_model_supports_reasoning_config(model_name: str) -> bool:
    model = str(model_name or "").strip().lower()
    return model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))


def gemini_thinking_config(model_name: str, reasoning_effort: str, types_mod):
    effort = str(reasoning_effort or "medium").strip().lower()
    model = str(model_name or "").strip().lower()
    if not model.startswith("gemini-"):
        return None

    if model.startswith("gemini-3") or model in {"gemini-pro-latest", "gemini-flash-latest", "gemini-flash-lite-latest"}:
        if "pro" in model:
            level_by_effort = {
                "none": "low",
                "minimal": "low",
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "high",
                "max": "high",
            }
            level = level_by_effort.get(effort, "high")
        else:
            level_by_effort = {
                "none": "minimal",
                "minimal": "minimal",
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "high",
                "max": "high",
            }
            level = level_by_effort.get(effort, "medium")
        if level == "minimal" and model in {"gemini-flash-latest", "gemini-3.7-flash", "gemini-3.8-flash"}:
            level = "low"
        return types_mod.ThinkingConfig(thinking_level=level, include_thoughts=True)

    if model.startswith("gemini-2.5"):
        if "pro" in model:
            budget_by_effort = {
                "none": 128,
                "low": 128,
                "medium": -1,
                "high": 32768,
                "xhigh": 32768,
                "max": 32768,
            }
        else:
            budget_by_effort = {
                "none": 0,
                "low": 1024,
                "medium": -1,
                "high": 24576,
                "xhigh": 24576,
                "max": 24576,
            }
        return types_mod.ThinkingConfig(
            thinking_budget=budget_by_effort.get(effort, -1),
            include_thoughts=True,
        )

    return None


def image_payloads_for_message(msg: dict, include_images: bool) -> list[dict]:
    if not include_images:
        return []
    raw_images = msg.get("images") or []
    if not isinstance(raw_images, list):
        return []

    payloads: list[dict] = []
    for image in raw_images:
        if not isinstance(image, dict):
            continue
        path = str(image.get("path") or "").strip()
        if not path:
            continue
        try:
            data = Path(path).read_bytes()
        except OSError:
            logger.warning("Skipping missing image attachment: %s", path)
            continue
        mime_type = str(image.get("mime_type") or "").strip()
        if not mime_type:
            mime_type = mimetypes.guess_type(path)[0] or "image/png"
        payloads.append({"path": path, "mime_type": mime_type, "data": data})
    return payloads


def normalise_messages_for_gemini(
    messages: list[dict],
    system_prompt: str,
    include_images: bool = False,
) -> list[dict]:
    result: list[dict] = []
    from google.genai import types

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        image_payloads = image_payloads_for_message(msg, include_images)

        gemini_parts = msg.get("gemini_parts")
        if isinstance(gemini_parts, list):
            gemini_role = "model" if role in {"assistant", "model"} else "user"
            result.append({"role": gemini_role, "parts": gemini_parts or [{"text": ""}]})
            continue

        function_response = msg.get("gemini_function_response")
        if isinstance(function_response, dict):
            response_payload = function_response.get("response")
            if not isinstance(response_payload, dict):
                response_payload = {"result": response_payload}
            function_response_kwargs: dict[str, Any] = {
                "name": str(function_response.get("name") or ""),
                "response": response_payload,
            }
            call_id = str(function_response.get("id") or "").strip()
            if call_id:
                function_response_kwargs["id"] = call_id
            result.append(
                {
                    "role": "user",
                    "parts": [
                        types.Part(
                            function_response=types.FunctionResponse(**function_response_kwargs)
                        )
                    ],
                }
            )
            continue

        if role == "system":
            continue

        if role in ("user", "human"):
            parts = [{"text": str(content)}] if content else []
            parts.extend(
                types.Part.from_bytes(data=payload["data"], mime_type=payload["mime_type"])
                for payload in image_payloads
            )
            result.append({"role": "user", "parts": parts or [{"text": ""}]})
        elif role in ("assistant", "model"):
            result.append({"role": "model", "parts": [{"text": content}]})

    if not result or result[0]["role"] != "user":
        result.insert(0, {"role": "user", "parts": [{"text": "Hello"}]})

    return result


def normalise_messages_for_openai_responses(
    messages: list[dict],
    include_images: bool = False,
) -> list[dict]:
    result: list[dict] = []
    for msg in messages:
        item_type = msg.get("type")
        if item_type == "function_call_output":
            # Monaw marks screenshots on the result with images/ephemeral; the
            # API accepts only call_id and output, so images go in the output.
            output = str(msg.get("output") or "")
            image_payloads = image_payloads_for_message(msg, include_images)
            if image_payloads:
                output_parts: list[dict] = [{"type": "input_text", "text": output}]
                output_parts += [
                    {
                        "type": "input_image",
                        "image_url": f"data:{payload['mime_type']};base64,"
                        f"{base64.b64encode(payload['data']).decode('ascii')}",
                    }
                    for payload in image_payloads
                ]
                result.append({"type": item_type, "call_id": msg.get("call_id", ""), "output": output_parts})
            else:
                result.append({"type": item_type, "call_id": msg.get("call_id", ""), "output": output})
            continue
        if item_type in {"function_call", "reasoning"}:
            result.append(dict(msg))
            continue

        role = msg.get("role", "user")
        if role == "model":
            role = "assistant"
        if role == "system":
            continue

        content = msg.get("content", "")
        image_payloads = image_payloads_for_message(msg, include_images and role == "user")
        if image_payloads:
            content_parts: list[dict] = [{"type": "input_text", "text": str(content)}]
            for payload in image_payloads:
                encoded = base64.b64encode(payload["data"]).decode("ascii")
                content_parts.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{payload['mime_type']};base64,{encoded}",
                    }
                )
            result.append({"role": role, "content": content_parts})
        else:
            result.append({"role": role, "content": str(content)})
    return result


def gemini_request_payload(
    *,
    model_name: str,
    messages: list[dict],
    tools: list[dict],
    system_prompt: str,
    reasoning_effort: str,
    include_images: bool,
    tool_choice: str | dict | None = None,
) -> tuple[list[dict], Any]:
    from google.genai import types

    contents = normalise_messages_for_gemini(
        messages,
        system_prompt,
        include_images=include_images,
    )
    config_kwargs: dict = {}
    if current_output_limit.get() is not None:
        config_kwargs["max_output_tokens"] = current_output_limit.get()
    if system_prompt:
        config_kwargs["system_instruction"] = system_prompt
    thinking_config = gemini_thinking_config(model_name, reasoning_effort, types)
    if thinking_config is not None:
        output_limit = current_output_limit.get()
        thinking_budget = getattr(thinking_config, "thinking_budget", None)
        if output_limit is not None and thinking_budget is not None and thinking_budget > 0:
            thinking_config.thinking_budget = min(thinking_budget, max(0, output_limit - 1))
        config_kwargs["thinking_config"] = thinking_config
    if tools:
        config_kwargs["tools"] = [tools_to_gemini(tools)]
        if tool_choice in {"required", "any"}:
            config_kwargs["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY")
            )

    config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
    return contents, config


def openai_responses_kwargs(
    *,
    model_name: str,
    messages: list[dict],
    tools: list[dict],
    system_prompt: str,
    reasoning_effort: str,
    include_images: bool,
    tool_choice: str | dict | None = None,
) -> dict:
    kwargs: dict = {
        "model": model_name,
        "input": normalise_messages_for_openai_responses(
            messages,
            include_images=include_images,
        ),
    }
    if current_output_limit.get() is not None:
        kwargs["max_output_tokens"] = current_output_limit.get()
    if system_prompt:
        kwargs["instructions"] = system_prompt
    if openai_model_supports_reasoning_config(model_name):
        effort = openai_reasoning_effort(reasoning_effort, model_name)
        reasoning: dict[str, str] = {"effort": effort}
        if effort != "none":
            reasoning["summary"] = "auto"
        kwargs["reasoning"] = reasoning
    if tools:
        kwargs["tools"] = tools_to_openai_responses(tools)
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
    return kwargs
