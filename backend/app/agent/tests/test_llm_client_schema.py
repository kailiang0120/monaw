"""Schema-conversion regressions for LLM provider payloads."""

import asyncio
import shutil
from types import SimpleNamespace
from pathlib import Path

from app.agent.llm_client import (
    LLMClient,
    VisionDescriber,
    build_tool_result_message,
    build_vision_describer,
    _deepseek_reasoning_effort,
    _gemini_thinking_config,
    _json_schema_prop_to_gemini,
    _model_supports_vision,
    _normalise_messages_for_gemini,
    _normalise_messages_for_openai,
    _normalise_messages_for_openai_responses,
    _openai_reasoning_effort,
    _tools_to_gemini,
)
from app.agent.harness.tool_protocol import ToolCallResult
from app.skills.memory.tools import register_tools as register_memory_tools


def test_object_schema_does_not_emit_additional_properties_for_gemini():
    from google.genai import types

    prop_def = {
        "type": "object",
        "title": "Params",
        "additionalProperties": {"type": "string"},
    }
    schema = _json_schema_prop_to_gemini(prop_def, types)
    payload = schema.model_dump(exclude_none=True)
    assert "additional_properties" not in payload


def test_gemini_schema_drops_empty_string_enum_values():
    from google.genai import types

    schema = _json_schema_prop_to_gemini(
        {
            "type": "string",
            "title": "Category",
            "enum": ["", "preference", "behavior"],
            "default": "",
        },
        types,
    )

    assert schema.enum == ["preference", "behavior"]


def test_memory_search_tool_schema_is_valid_for_gemini_enum_rules():
    tools: list[dict] = []
    register_memory_tools(tools)
    memory_search = next(tool for tool in tools if tool["name"] == "memory_search")

    gemini_tool = _tools_to_gemini([memory_search])
    category_schema = gemini_tool.function_declarations[0].parameters.properties["category"]

    assert "" not in category_schema.enum
    assert category_schema.enum == ["preference", "behavior", "fact", "workflow", "project", "style"]


def test_openai_message_normalization_attaches_image_parts():
    tmp_dir = Path.cwd() / ".tmp-llm-client-schema-openai"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    image_path = tmp_dir / "browser.png"
    image_path.write_bytes(b"image bytes")

    try:
        messages = _normalise_messages_for_openai(
            [{"role": "user", "content": "Inspect this", "images": [{"path": str(image_path)}]}],
            "",
            include_images=True,
        )

        content = messages[0]["content"]
        assert content[0] == {"type": "text", "text": "Inspect this"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_gemini_message_normalization_attaches_inline_image_parts():
    tmp_dir = Path.cwd() / ".tmp-llm-client-schema-gemini"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    image_path = tmp_dir / "browser.png"
    image_path.write_bytes(b"image bytes")

    try:
        messages = _normalise_messages_for_gemini(
            [{"role": "user", "content": "Inspect this", "images": [{"path": str(image_path)}]}],
            "",
            include_images=True,
        )

        parts = messages[0]["parts"]
        assert parts[0] == {"text": "Inspect this"}
        assert parts[1].inline_data.mime_type == "image/png"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_deepseek_uses_openai_client_with_base_url(monkeypatch):
    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

    client = LLMClient(
        provider="deepseek",
        model_name="deepseek-v4-pro",
        api_key="deepseek-key",
        base_url="https://api.deepseek.com",
        reasoning_effort="high",
    )

    assert captured == {
        "api_key": "deepseek-key",
        "base_url": "https://api.deepseek.com",
    }
    assert client.reasoning_effort == "high"


def test_deepseek_reasoning_effort_maps_to_provider_contract():
    assert _deepseek_reasoning_effort("low") == "high"
    assert _deepseek_reasoning_effort("medium") == "high"
    assert _deepseek_reasoning_effort("high") == "high"
    assert _deepseek_reasoning_effort("xhigh") == "max"
    assert _deepseek_reasoning_effort("max") == "max"


def test_openai_reasoning_effort_maps_to_responses_contract():
    assert _openai_reasoning_effort("none") == "minimal"
    assert _openai_reasoning_effort("minimal") == "minimal"
    assert _openai_reasoning_effort("low") == "low"
    assert _openai_reasoning_effort("medium") == "medium"
    assert _openai_reasoning_effort("high") == "high"
    assert _openai_reasoning_effort("xhigh") == "high"
    assert _openai_reasoning_effort("max") == "high"
    assert _openai_reasoning_effort("unknown") == "medium"


def test_gemini_thinking_effort_maps_to_provider_contract():
    from google.genai import types

    gemini_31 = _gemini_thinking_config("gemini-3.1-pro-preview", "medium", types)
    gemini_31_low = _gemini_thinking_config("gemini-3.1-pro-preview", "low", types)
    gemini_31_none = _gemini_thinking_config("gemini-3.1-pro-preview", "none", types)
    gemini_3_flash_minimal = _gemini_thinking_config("gemini-3-flash-preview", "minimal", types)
    gemini_3_flash_medium = _gemini_thinking_config("gemini-3-flash-preview", "medium", types)
    gemini_3_flash = _gemini_thinking_config("gemini-3-flash-preview", "high", types)
    gemini_25_pro = _gemini_thinking_config("gemini-2.5-pro", "none", types)
    gemini_25_flash = _gemini_thinking_config("gemini-2.5-flash", "none", types)

    assert gemini_31.thinking_level.value == "MEDIUM"
    assert gemini_31_low.thinking_level.value == "LOW"
    assert gemini_31_none.thinking_level.value == "LOW"
    assert gemini_3_flash_minimal.thinking_level.value == "MINIMAL"
    assert gemini_3_flash_medium.thinking_level.value == "MEDIUM"
    assert gemini_3_flash.thinking_level.value == "HIGH"
    assert gemini_25_pro.thinking_budget == 128
    assert gemini_25_flash.thinking_budget == 0
    assert gemini_31.include_thoughts is True
    assert gemini_25_pro.include_thoughts is True


def test_model_supports_vision_detects_text_only_providers():
    assert _model_supports_vision("deepseek", "deepseek-v4-pro") is False
    assert _model_supports_vision("openai", "gpt-5.4") is True
    assert _model_supports_vision("gemini", "gemini-2.5-flash") is True
    assert _model_supports_vision("new-compatible-provider", "vision-model") is True


def test_gemini_uses_google_genai_client(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("google.genai.Client", FakeClient)

    client = LLMClient(
        provider="gemini",
        model_name="gemini-3.1-pro-preview",
        api_key="google-key",
        reasoning_effort="medium",
    )

    assert captured == {"api_key": "google-key"}
    assert client.supports_vision is True


def test_gemini_chat_payload_uses_generate_content_with_thinking_config(monkeypatch):
    captured = {}

    class FakeModels:
        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(text="ok", function_calls=[])

    class FakeClient:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(models=FakeModels())

    monkeypatch.setattr("google.genai.Client", FakeClient)

    client = LLMClient(
        provider="gemini",
        model_name="gemini-3-flash-preview",
        api_key="google-key",
        reasoning_effort="high",
    )
    response = asyncio.run(client._gemini_chat([{"role": "user", "content": "hello"}], [], "sys", None))

    assert response.content == "ok"
    assert captured["model"] == "gemini-3-flash-preview"
    assert captured["config"].thinking_config.thinking_level.value == "HIGH"
    assert captured["config"].thinking_config.include_thoughts is True
    assert captured["config"].system_instruction == "sys"


def test_gemini_chat_separates_thought_summaries_from_answer(monkeypatch):
    captured = {}
    response_payload = SimpleNamespace(
        text="fallback should not be used",
        function_calls=[],
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(text="thought summary", thought=True),
                        SimpleNamespace(text="final answer", thought=False),
                    ]
                )
            )
        ],
    )

    class FakeModels:
        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return response_payload

    class FakeClient:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(models=FakeModels())

    monkeypatch.setattr("google.genai.Client", FakeClient)

    client = LLMClient(
        provider="gemini",
        model_name="gemini-2.5-flash",
        api_key="google-key",
        reasoning_effort="medium",
    )
    response = asyncio.run(client._gemini_chat([{"role": "user", "content": "hello"}], [], "", None))

    assert captured["config"].thinking_config.include_thoughts is True
    assert response.reasoning_content == "thought summary"
    assert response.content == "final answer"


def test_gemini_stream_separates_thought_summaries_from_answer(monkeypatch):
    streamed: list[str] = []

    def chunk_with_part(text: str, thought: bool):
        return SimpleNamespace(
            text=text,
            function_calls=[],
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=[SimpleNamespace(text=text, thought=thought)])
                )
            ],
        )

    async def fake_stream():
        yield chunk_with_part("planning", True)
        yield chunk_with_part("answer", False)

    class FakeModels:
        async def generate_content_stream(self, **_kwargs):
            return fake_stream()

    class FakeClient:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(models=FakeModels())

    async def stream_callback(text: str):
        streamed.append(text)

    monkeypatch.setattr("google.genai.Client", FakeClient)

    client = LLMClient(
        provider="gemini",
        model_name="gemini-2.5-flash",
        api_key="google-key",
        reasoning_effort="medium",
    )
    response = asyncio.run(
        client._gemini_chat([{"role": "user", "content": "hello"}], [], "", stream_callback)
    )

    assert streamed == ["answer"]
    assert response.reasoning_content == "planning"
    assert response.content == "answer"


def test_gemini_tool_round_trip_uses_function_response_parts():
    messages = _normalise_messages_for_gemini(
        [
            {
                "role": "user",
                "content": "Tool result for browser_snapshot",
                "gemini_function_response": {
                    "id": "call-1",
                    "name": "browser_snapshot",
                    "response": {"status": "ok"},
                },
            }
        ],
        "",
    )

    response_part = messages[0]["parts"][0].function_response
    assert messages[0]["role"] == "user"
    assert response_part.id == "call-1"
    assert response_part.name == "browser_snapshot"
    assert response_part.response == {"status": "ok"}


def test_build_vision_describer_only_for_text_only_primary_with_google_key():
    settings = SimpleNamespace(
        model_provider="deepseek",
        model_name="deepseek-v4-pro",
        google_api_key="google-key",
        llm=SimpleNamespace(
            provider="deepseek",
            model_name="deepseek-v4-pro",
            vision_fallback_enabled=True,
            vision_fallback_model="gemini-3.1-flash-lite-preview",
            vision_fallback_max_output_tokens=1200,
        ),
    )

    describer = build_vision_describer(settings)

    assert isinstance(describer, VisionDescriber)
    assert describer.model_name == "gemini-3.1-flash-lite-preview"
    assert describer.max_output_tokens == 1200


def test_build_vision_describer_skips_native_vision_or_missing_key():
    native_vision = SimpleNamespace(
        model_provider="openai",
        model_name="gpt-5.4",
        google_api_key="google-key",
        llm=SimpleNamespace(vision_fallback_enabled=True, vision_fallback_model="gemini-2.5-flash"),
    )
    missing_key = SimpleNamespace(
        model_provider="deepseek",
        model_name="deepseek-v4-pro",
        google_api_key="",
        llm=SimpleNamespace(vision_fallback_enabled=True, vision_fallback_model="gemini-2.5-flash"),
    )

    assert build_vision_describer(native_vision) is None
    assert build_vision_describer(missing_key) is None


def test_deepseek_chat_payload_enables_thinking_with_high_or_max_effort(monkeypatch):
    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content="ok", tool_calls=[]),
                    )
                ]
            )

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

    client = LLMClient(
        provider="deepseek",
        model_name="deepseek-v4-pro",
        api_key="deepseek-key",
        reasoning_effort="xhigh",
    )
    response = asyncio.run(client._openai_chat([{"role": "user", "content": "hello"}], [], "", None))

    assert response.content == "ok"
    assert captured["reasoning_effort"] == "max"
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}


def test_deepseek_chat_payload_omits_tool_choice_when_tools_are_available(monkeypatch):
    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="tool_calls",
                        message=SimpleNamespace(
                            content="",
                            tool_calls=[
                                SimpleNamespace(
                                    id="call-final",
                                    function=SimpleNamespace(
                                        name="final_answer",
                                        arguments='{"answer":"done"}',
                                    ),
                                )
                            ],
                        ),
                    )
                ]
            )

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

    client = LLMClient(
        provider="deepseek",
        model_name="deepseek-v4-pro",
        api_key="deepseek-key",
        reasoning_effort="high",
    )
    response = asyncio.run(client._openai_chat(
        [{"role": "user", "content": "hello"}],
        [
            {
                "name": "final_answer",
                "description": "Finish",
                "parameters": {"type": "object", "properties": {"answer": {"type": "string"}}},
            }
        ],
        "",
        None,
    ))

    assert "tool_choice" not in captured
    assert response.tool_calls[0].tool_name == "final_answer"


def test_openai_responses_payload_can_require_tool_choice(monkeypatch):
    captured = {}

    class FakeResponses:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status="completed",
                output=[
                    SimpleNamespace(
                        type="function_call",
                        id="fc-final",
                        call_id="call-final",
                        name="final_answer",
                        arguments='{"answer":"done"}',
                    ),
                ],
            )

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs):
            self.responses = FakeResponses()

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

    client = LLMClient(provider="openai", model_name="gpt-test", api_key="openai-key")
    response = asyncio.run(
        client._openai_chat(
            [{"role": "user", "content": "hello"}],
            [
                {
                    "name": "final_answer",
                    "description": "Finish",
                    "parameters": {"type": "object", "properties": {"answer": {"type": "string"}}},
                }
            ],
            "",
            None,
            tool_choice="required",
        )
    )

    assert captured["tool_choice"] == "required"
    assert captured["tools"][0]["type"] == "function"
    assert captured["tools"][0]["name"] == "final_answer"
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].tool_name == "final_answer"
    assert response.tool_calls[0].arguments == {"answer": "done"}


def test_openai_responses_payload_requests_reasoning_summary(monkeypatch):
    captured = {}

    class FakeResponses:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status="completed",
                output=[
                    SimpleNamespace(
                        type="reasoning",
                        summary=[SimpleNamespace(type="summary_text", text="short reasoning")],
                    ),
                    SimpleNamespace(
                        type="message",
                        content=[SimpleNamespace(type="output_text", text="final answer")],
                    ),
                ],
                output_text="final answer",
            )

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs):
            self.responses = FakeResponses()

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

    client = LLMClient(
        provider="openai",
        model_name="gpt-5.4",
        api_key="openai-key",
        reasoning_effort="high",
    )
    response = asyncio.run(
        client._openai_chat([{"role": "user", "content": "hello"}], [], "system prompt", None)
    )

    assert captured["instructions"] == "system prompt"
    assert captured["reasoning"] == {"effort": "high", "summary": "auto"}
    assert response.content == "final answer"
    assert response.reasoning_content == "short reasoning"
    assert response.provider_messages[0]["type"] == "reasoning"
    assert response.provider_messages[0]["summary"][0]["text"] == "short reasoning"


def test_openai_responses_extracts_provider_usage(monkeypatch):
    class FakeResponses:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                status="completed",
                output=[
                    SimpleNamespace(
                        type="message",
                        content=[SimpleNamespace(type="output_text", text="final answer")],
                    ),
                ],
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=20,
                    total_tokens=130,
                    input_tokens_details=SimpleNamespace(cached_tokens=7),
                    output_tokens_details=SimpleNamespace(reasoning_tokens=10),
                ),
            )

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs):
            self.responses = FakeResponses()

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

    client = LLMClient(provider="openai", model_name="gpt-test", api_key="openai-key")
    response = asyncio.run(client._openai_chat([{"role": "user", "content": "hello"}], [], "", None))

    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 20
    assert response.usage.reasoning_tokens == 10
    assert response.usage.cached_tokens == 7
    assert response.usage.total_tokens == 130
    assert response.usage.source == "provider"


def test_deepseek_chat_extracts_openai_compatible_usage(monkeypatch):
    class FakeCompletions:
        async def create(self, **_kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content="ok", tool_calls=[]),
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=8,
                    completion_tokens=4,
                    total_tokens=12,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=3),
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
                ),
            )

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

    client = LLMClient(provider="deepseek", model_name="deepseek-test", api_key="deepseek-key")
    response = asyncio.run(client._openai_chat([{"role": "user", "content": "hello"}], [], "", None))

    assert response.usage.input_tokens == 8
    assert response.usage.output_tokens == 4
    assert response.usage.reasoning_tokens == 2
    assert response.usage.cached_tokens == 3
    assert response.usage.total_tokens == 12
    assert response.usage.source == "provider"


def test_gemini_extracts_usage_metadata(monkeypatch):
    response_payload = SimpleNamespace(
        text="fallback should not be used",
        function_calls=[],
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[SimpleNamespace(text="final answer", thought=False)]
                )
            )
        ],
        usage_metadata=SimpleNamespace(
            prompt_token_count=9,
            candidates_token_count=5,
            thoughts_token_count=4,
            cached_content_token_count=2,
            total_token_count=18,
        ),
    )

    class FakeModels:
        async def generate_content(self, **_kwargs):
            return response_payload

    class FakeClient:
        def __init__(self, **_kwargs):
            self.aio = SimpleNamespace(models=FakeModels())

    monkeypatch.setattr("google.genai.Client", FakeClient)

    client = LLMClient(provider="gemini", model_name="gemini-2.5-flash", api_key="google-key")
    response = asyncio.run(client._gemini_chat([{"role": "user", "content": "hello"}], [], "", None))

    assert response.usage.input_tokens == 9
    assert response.usage.output_tokens == 5
    assert response.usage.reasoning_tokens == 4
    assert response.usage.cached_tokens == 2
    assert response.usage.total_tokens == 18
    assert response.usage.source == "provider"


def test_openai_responses_stream_separates_reasoning_from_answer(monkeypatch):
    streamed: list[str] = []

    async def fake_stream():
        yield SimpleNamespace(type="response.reasoning_summary_text.delta", delta="why")
        yield SimpleNamespace(type="response.output_text.delta", delta="answer")

    class FakeResponses:
        async def create(self, **kwargs):
            assert kwargs["stream"] is True
            return fake_stream()

    class FakeAsyncOpenAI:
        def __init__(self, **_kwargs):
            self.responses = FakeResponses()

    async def stream_callback(text: str):
        streamed.append(text)

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

    client = LLMClient(
        provider="openai",
        model_name="gpt-5.4",
        api_key="openai-key",
        reasoning_effort="medium",
    )
    response = asyncio.run(
        client._openai_chat([{"role": "user", "content": "hello"}], [], "", stream_callback)
    )

    assert streamed == ["answer"]
    assert response.content == "answer"
    assert response.reasoning_content == "why"


def test_openai_tool_results_round_trip_as_responses_function_call_output():
    result = ToolCallResult.from_output(
        call_id="call-1",
        name="web_search",
        output='{"status":"ok","result":"found"}',
    )

    message = build_tool_result_message(result, provider="openai")
    normalised = _normalise_messages_for_openai_responses([message])

    assert message == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": '{"status":"ok","result":"found"}',
    }
    assert normalised == [message]
