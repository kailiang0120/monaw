"""Exercise locked provider SDKs against wire responses without live credentials."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from google import genai
from google.genai import types

from app.agent.llm_client import LLMClient, ResponseInterruptedError
from app.agent.model_usage import ModelUsageBudget, current_model_budget


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('finish_reason', ['STOP', 'MAX_TOKENS', 'SAFETY', 'MALFORMED_FUNCTION_CALL', None])
def test_gemini_sdk_terminal_contract_and_model_limit(streaming, finish_reason):
    """Only completed calls survive SDK parsing; metadata constrains the request."""
    requests = []
    candidate = {'content': {'role': 'model', 'parts': [
        {'functionCall': {'id': 'call-1', 'name': 'inspect', 'args': {'query': 'value'}}},
    ]}}
    if finish_reason is not None:
        candidate['finishReason'] = finish_reason
    payload = {'candidates': [candidate], 'usageMetadata': {
        'promptTokenCount': 11, 'candidatesTokenCount': 7, 'thoughtsTokenCount': 5, 'totalTokenCount': 23,
    }}

    def transport(request):
        """Serve model metadata and generation JSON/SSE from the public wire shape."""
        if request.method == 'GET':
            return httpx.Response(200, json={'name': 'models/gemini-2.5-flash', 'outputTokenLimit': 512})
        requests.append(json.loads(request.content))
        if streaming:
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                  content='data: ' + json.dumps(payload) + '\n\n')
        return httpx.Response(200, json=payload)

    async def scenario():
        """Use the real SDK serializer and parser with a completely local transport."""
        client = LLMClient('gemini', 'gemini-2.5-flash', '')
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            client._genai_client = genai.Client(api_key='fixture-key', http_options=types.HttpOptions(
                base_url='https://fixture.invalid', httpx_async_client=http,
            ))
            budget = ModelUsageBudget(5000)
            token = current_model_budget.set(budget)
            async def receive(_text):
                """Allow streaming without creating external output."""
                pass
            try:
                call = client.chat_with_tools([], [{
                    'name': 'inspect', 'parameters': {'type': 'object', 'properties': {
                        'query': {'anyOf': [{'type': 'string'}, {'type': 'null'}]},
                    }},
                }], stream_callback=receive if streaming else None)
                if finish_reason is None:
                    with pytest.raises(ResponseInterruptedError):
                        await call
                else:
                    result = await call
                    assert bool(result.tool_calls) is (finish_reason == 'STOP')
                    assert bool(result.provider_messages) is (finish_reason == 'STOP')
                    expected = {
                        'STOP': 'tool_calls', 'MAX_TOKENS': 'length',
                        'SAFETY': 'blocked', 'MALFORMED_FUNCTION_CALL': 'incomplete',
                    }
                    assert result.finish_reason == expected[finish_reason]
                    assert result.provider_finish_reason == finish_reason
                    assert budget.usage.total_tokens == 23
            finally:
                current_model_budget.reset(token)
                await client._genai_client.aio.aclose()
            assert budget.reserved == 0
        assert len(requests) == 1
        assert requests[0]['generationConfig']['maxOutputTokens'] == 512
        assert requests[0]['tools'][0]['functionDeclarations'][0]['parameters_json_schema']['properties']['query']['anyOf'] == [
            {'type': 'string'}, {'type': 'null'},
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize('streaming', [False, True])
def test_gemini4_sdk_keeps_thinking_level_and_final_signature(streaming):
    """Gemini 4 controls and signed final text survive an actual SDK round trip."""
    from app.agent.harness.response_policy import TurnResponsePolicy

    requests = []
    payload = {'candidates': [{'content': {'role': 'model', 'parts': [
        {'text': 'Finished.', 'thoughtSignature': 'c2lnbmVk'},
    ]}, 'finishReason': 'STOP'}]}

    def transport(request):
        """Return a signed text response without making provider requests."""
        if request.method == 'GET':
            return httpx.Response(200, json={'name': 'models/gemini-4-neon', 'outputTokenLimit': 65536})
        requests.append(json.loads(request.content))
        if streaming:
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                  content='data: ' + json.dumps(payload) + '\n\n')
        return httpx.Response(200, json=payload)

    async def scenario():
        """Send the returned provider parts back through the SDK serializer."""
        client = LLMClient('gemini', 'gemini-4-neon', '', reasoning_effort='xhigh')
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            client._genai_client = genai.Client(api_key='fixture-key', http_options=types.HttpOptions(
                base_url='https://fixture.invalid', httpx_async_client=http,
            ))

            async def receive(_text):
                """Enable streaming without writing test output."""
                pass

            try:
                result = await client.chat_with_tools(
                    [{'role': 'user', 'content': 'Begin.'}], [],
                    stream_callback=receive if streaming else None,
                )
                assert result.content == 'Finished.' and not result.tool_calls
                decision = TurnResponsePolicy().decide(
                    result, tools_available=False, history=[], budget_exhausted=False,
                )
                assert decision.messages == result.provider_messages
                await client.chat_with_tools(
                    [{'role': 'user', 'content': 'Begin.'}] + decision.messages
                    + [{'role': 'user', 'content': 'Continue.'}], [],
                    stream_callback=receive if streaming else None,
                )
                assert requests[0]['generationConfig']['thinkingConfig']['thinking_level'] == 'XHIGH'
                assert 'thinking_budget' not in requests[0]['generationConfig']['thinkingConfig']
                assert requests[1]['contents'][1]['parts'][0]['thoughtSignature'] == 'c2lnbmVk'
            finally:
                await client._genai_client.aio.aclose()

    asyncio.run(scenario())


def test_account_and_unreported_reasoning_keep_conservative_reservations():
    """Account calls cannot promise an API cap and summaries omit hidden tokens."""
    from app.agent.model_capabilities import model_capabilities
    from app.agent.model_usage import estimated_usage, settle_estimated_usage
    from app.agent.llm_provider_responses import usage_from_gemini_metadata, usage_from_openai

    client = SimpleNamespace(provider='codex', model_name='gpt-6-sol', reasoning_effort='high')
    assert model_capabilities(client).output_parameter is None
    usage = settle_estimated_usage(client, estimated_usage(client, 20, 'summary'), output_allowance=1000, uncertain=False)
    assert usage.total_tokens == 1020 and usage.source == 'chatgpt_account'
    assert usage_from_openai({'input_tokens': 10, 'output_tokens': 20,
                              'output_tokens_details': {'reasoning_tokens': 15}}).total_tokens == 30
    gemini = usage_from_gemini_metadata({'promptTokenCount': 10, 'toolUsePromptTokenCount': 3,
        'candidatesTokenCount': 8, 'thoughtsTokenCount': 4,
        'promptTokensDetails': [{'modality': 'IMAGE', 'tokenCount': 7}]})
    assert gemini.total_tokens == 25 and gemini.image_tokens == 7


@pytest.mark.parametrize('terminal', ['completed', 'incomplete', 'content_filter', 'refusal', None, 'late_content'])
def test_openai_sdk_stream_requires_terminal_evidence(terminal):
    """Real SDK parsing must preserve completion guards and output-cap fields."""
    from openai import AsyncOpenAI

    requests = []
    item = {'type': 'function_call', 'id': 'fc-1', 'call_id': 'call-1',
            'name': 'inspect', 'arguments': '{}', 'status': 'completed'}
    events = [{'type': 'response.output_item.done', 'output_index': 0, 'sequence_number': 1, 'item': item}]
    if terminal is not None:
        status = 'completed' if terminal in {'late_content', 'refusal'} else 'incomplete' if terminal == 'content_filter' else terminal
        output = [{'type': 'message', 'id': 'msg-1', 'role': 'assistant', 'status': 'completed',
                   'content': [{'type': 'refusal', 'refusal': 'I cannot fulfill this request.'}]}] if terminal == 'refusal' else []
        events.append({'type': 'response.' + status, 'sequence_number': 2, 'response': {
            'id': 'resp-1', 'object': 'response', 'created_at': 1, 'model': 'gpt-4.1', 'status': status,
            'output': output, 'usage': {'input_tokens': 5, 'output_tokens': 10, 'total_tokens': 15},
            **({'incomplete_details': {'reason': 'content_filter' if terminal == 'content_filter' else 'max_output_tokens'}}
               if status == 'incomplete' else {}),
        }})
    if terminal == 'late_content':
        events.append({'type': 'response.output_item.done', 'output_index': 1, 'sequence_number': 3, 'item': item})

    def transport(request):
        """Keep all API traffic inside this test while exercising the SSE decoder."""
        requests.append(json.loads(request.content))
        body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n'
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content=body)

    async def scenario():
        """Make one bounded SDK call and confirm no transport retry occurs."""
        client = LLMClient('openai', 'gpt-4.1', 'fixture-key')
        await client._openai_client.close()
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            client._openai_client = AsyncOpenAI(api_key='fixture-key', base_url='https://fixture.invalid', http_client=http)
            budget = ModelUsageBudget(1000)
            token = current_model_budget.set(budget)
            async def receive(_text):
                """Activate streaming for this tool-only response."""
                pass
            try:
                call = client.chat_with_tools([], [], stream_callback=receive)
                if terminal in {None, 'late_content'}:
                    with pytest.raises(ResponseInterruptedError):
                        await call
                else:
                    result = await call
                    assert bool(result.tool_calls) is (terminal == 'completed')
                    expected = 'tool_calls' if terminal == 'completed' else 'blocked' if terminal in {'content_filter', 'refusal'} else 'length'
                    assert result.finish_reason == expected
                    if terminal == 'refusal':
                        assert result.content == 'I cannot fulfill this request.'
                assert len(requests) == 1
                assert 0 < requests[0]['max_output_tokens'] < 1000
                assert budget.reserved == 0
            finally:
                current_model_budget.reset(token)
                await client._openai_client.close()

    asyncio.run(scenario())


def test_screenshot_estimates_follow_dimensions_without_counting_base64(tmp_path):
    """Large screenshots cost more, while attachment encodings are not text."""
    from PIL import Image
    from app.agent.image_usage import estimate_image_tokens
    from app.agent.context_usage import estimate_message_tokens

    client = SimpleNamespace(provider='openai', model_name='gpt-4o', supports_vision=True)
    path = tmp_path / 'screenshot.png'
    Image.new('RGB', (2048, 2048)).save(path)
    assert estimate_image_tokens({'path': str(path)}, llm_client=client) == 765
    assert estimate_image_tokens({'path': str(path), 'detail': 'low'}, llm_client=client) == 85
    result = [{'type': 'function_call_output', 'call_id': 'screen', 'output': 'screenshot', 'images': [{'path': str(path)}]}]
    assert 765 < estimate_message_tokens(result, llm_client=client) < 900
    Image.new('RGB', (256, 256)).save(path)
    assert estimate_image_tokens({'path': str(path)}, llm_client=client) == 255
    Image.new('RGB', (1, 4096)).save(path)
    assert estimate_image_tokens({'path': str(path)}, llm_client=client) == 765
