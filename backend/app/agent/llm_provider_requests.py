"""Provider request conversion helpers for LLM chat calls."""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def json_schema_prop_to_gemini(prop_def: dict, types_mod):
    """Map a JSON Schema property to google.genai.types.Schema."""

    T = types_mod.Type
    desc = str(prop_def.get("description") or prop_def.get("title") or "")

    if "anyOf" in prop_def:
        variants = prop_def["anyOf"]
        non_null = [v for v in variants if isinstance(v, dict) and v.get("type") != "null"]
        nullable = any(isinstance(v, dict) and v.get("type") == "null" for v in variants)
        if len(non_null) == 1:
            inner = json_schema_prop_to_gemini(non_null[0], types_mod)
            if nullable:
                return inner.model_copy(update={"nullable": True})
            return inner
        if non_null:
            return json_schema_prop_to_gemini(non_null[0], types_mod)
        return types_mod.Schema(type=T.STRING, description=desc)

    ptype = prop_def.get("type")
    if isinstance(ptype, list):
        ptype = next((x for x in ptype if x and x != "null"), "string")

    if ptype == "string":
        kw: dict = {"type": T.STRING, "description": desc}
        if "enum" in prop_def and prop_def["enum"]:
            enum_values = [str(e) for e in prop_def["enum"] if str(e)]
            if enum_values:
                kw["enum"] = enum_values
        return types_mod.Schema(**kw)
    if ptype == "number":
        return types_mod.Schema(type=T.NUMBER, description=desc)
    if ptype == "integer":
        return types_mod.Schema(type=T.INTEGER, description=desc)
    if ptype == "boolean":
        return types_mod.Schema(type=T.BOOLEAN, description=desc)
    if ptype == "array":
        items_src = prop_def.get("items") if isinstance(prop_def.get("items"), dict) else None
        items_schema = json_schema_prop_to_gemini(items_src or {"type": "string"}, types_mod)
        return types_mod.Schema(type=T.ARRAY, description=desc, items=items_schema)
    if ptype == "object":
        raw_props = prop_def.get("properties")
        properties: dict = {}
        if isinstance(raw_props, dict):
            for name, sub in raw_props.items():
                if isinstance(sub, dict):
                    properties[name] = json_schema_prop_to_gemini(sub, types_mod)
        req = prop_def.get("required")
        required_list = req if isinstance(req, list) else None
        return types_mod.Schema(
            type=T.OBJECT,
            description=desc,
            properties=properties or None,
            required=required_list,
        )

    return types_mod.Schema(type=T.STRING, description=desc)


def tools_to_gemini(tools: list[dict]):
    from google.genai import types

    declarations = []
    for t in tools:
        params = t.get("parameters", {})
        props_raw = params.get("properties", {})

        properties: dict = {}
        for prop_name, prop_def in props_raw.items():
            if isinstance(prop_def, dict):
                properties[prop_name] = json_schema_prop_to_gemini(prop_def, types)
            else:
                properties[prop_name] = types.Schema(type=types.Type.STRING, description="")

        declarations.append(
            types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties=properties,
                    required=params.get("required", []),
                ),
            )
        )

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



def openai_reasoning_effort(value: str) -> str:
    effort = str(value or "").strip().lower()
    if effort in {"none", "minimal"}:
        return "minimal"
    if effort in {"low", "medium", "high"}:
        return effort
    if effort in {"xhigh", "max"}:
        return "high"
    return "medium"


def openai_model_supports_reasoning_config(model_name: str) -> bool:
    model = str(model_name or "").strip().lower()
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def gemini_thinking_config(model_name: str, reasoning_effort: str, types_mod):
    effort = str(reasoning_effort or "medium").strip().lower()
    model = str(model_name or "").strip().lower()
    if not model.startswith("gemini-"):
        return None

    if model.startswith("gemini-3"):
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
        if item_type in {"function_call", "function_call_output", "reasoning"}:
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
    if system_prompt:
        config_kwargs["system_instruction"] = system_prompt
    thinking_config = gemini_thinking_config(model_name, reasoning_effort, types)
    if thinking_config is not None:
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
    if system_prompt:
        kwargs["instructions"] = system_prompt
    if openai_model_supports_reasoning_config(model_name):
        effort = openai_reasoning_effort(reasoning_effort)
        reasoning: dict[str, str] = {"effort": effort}
        if effort != "none":
            reasoning["summary"] = "auto"
        kwargs["reasoning"] = reasoning
    if tools:
        kwargs["tools"] = tools_to_openai_responses(tools)
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
    return kwargs
