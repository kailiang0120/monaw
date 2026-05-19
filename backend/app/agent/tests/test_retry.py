"""Tests for decorrelated jitter backoff and retry utilities."""

import asyncio
import pytest
from app.agent.retry import jittered_backoff, retry_with_backoff, _default_retryable


def test_jittered_backoff_increases():
    delays = [jittered_backoff(i, base=1.0, max_delay=100.0, jitter_ratio=0.0) for i in range(1, 6)]
    for i in range(1, len(delays)):
        assert delays[i] >= delays[i - 1] * 0.5


def test_jittered_backoff_capped():
    delay = jittered_backoff(100, base=5.0, max_delay=10.0, jitter_ratio=0.0)
    assert delay <= 15.0


def test_default_retryable_rate_limit():
    assert _default_retryable(Exception("429 Too Many Requests")) is True
    assert _default_retryable(Exception("rate limit exceeded")) is True


def test_default_retryable_timeout():
    assert _default_retryable(Exception("Connection timed out")) is True


def test_default_retryable_permanent():
    assert _default_retryable(Exception("Invalid API key")) is False


def test_retry_success_first_try():
    calls = []

    async def factory():
        calls.append(1)
        return "ok"

    result = asyncio.run(retry_with_backoff(factory, max_attempts=3))
    assert result == "ok"
    assert len(calls) == 1


def test_retry_transient_then_success():
    attempt = [0]

    async def factory():
        attempt[0] += 1
        if attempt[0] < 3:
            raise Exception("503 Service Unavailable")
        return "recovered"

    result = asyncio.run(retry_with_backoff(factory, max_attempts=4, base_delay=0.01))
    assert result == "recovered"
    assert attempt[0] == 3


def test_retry_permanent_fails_immediately():
    attempt = [0]

    async def factory():
        attempt[0] += 1
        raise Exception("Invalid API key - permanent")

    with pytest.raises(Exception, match="Invalid API key"):
        asyncio.run(retry_with_backoff(factory, max_attempts=4, base_delay=0.01))
    assert attempt[0] == 1
