from __future__ import annotations

import asyncio

from app.agent.context_compression import compress_context


def test_compression_does_not_report_phantom_integrity_phase():
    messages = [
        {"role": "user", "content": f"message {index}"}
        for index in range(8)
    ]

    result = asyncio.run(compress_context(messages))

    assert result.phases_applied == ["boundary_protection"]
    assert "integrity_repair" not in result.phases_applied


def test_compression_returns_without_llm_when_boundaries_have_no_middle():
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]

    result = asyncio.run(compress_context(messages, llm_chat_fn=lambda *_args: None))

    assert result.messages == messages
    assert result.phases_applied == ["boundary_protection"]
