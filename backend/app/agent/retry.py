"""Decorrelated jitter backoff and retry utilities.

Provides exponential backoff with jitter that prevents thundering herd
in concurrent retries, plus a thread-safe global counter for decorrelation.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Thread-safe global counter to decorrelate concurrent retries
_retry_counter = 0
_counter_lock = threading.Lock()


def _next_counter() -> int:
    global _retry_counter
    with _counter_lock:
        _retry_counter += 1
        return _retry_counter


def jittered_backoff(
    attempt: int,
    base: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """Compute decorrelated jitter backoff delay.

    Uses the global counter to offset concurrent retries, preventing
    thundering herd when multiple requests fail simultaneously.
    """
    delay = min(base * (2 ** (attempt - 1)), max_delay)
    jitter = random.uniform(0, jitter_ratio * delay)
    counter_offset = (_next_counter() % 7) * 0.3
    return delay + jitter + counter_offset


async def retry_with_backoff(
    coro_factory: Callable[[], Awaitable[T]],
    max_attempts: int = 4,
    is_retryable: Callable[[Exception], bool] | None = None,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> T:
    """Execute an async callable with decorrelated jitter backoff.

    Args:
        coro_factory: Zero-arg callable returning an awaitable.
        max_attempts: Total attempts (including the first).
        is_retryable: Predicate that returns True if the exception is transient.
        base_delay: Base delay in seconds for backoff calculation.
        max_delay: Maximum delay cap.
        on_retry: Optional callback(attempt, exception, delay) before sleeping.
    """
    if is_retryable is None:
        is_retryable = _default_retryable

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not is_retryable(exc):
                raise
            delay = jittered_backoff(attempt, base=base_delay, max_delay=max_delay)
            if on_retry:
                on_retry(attempt, exc, delay)
            else:
                logger.warning(
                    "Attempt %d/%d failed (%s: %s); retrying in %.1fs",
                    attempt, max_attempts, type(exc).__name__, exc, delay,
                )
            await asyncio.sleep(delay)

    raise last_exc  # type: ignore[misc]


def _default_retryable(exc: Exception) -> bool:
    """Default predicate: treat rate limits, timeouts, 5xx as transient.

    Checks exception types first (authoritative), then falls back to message
    substring matching as a catch-all for provider SDK exceptions.
    """
    # H2: Type-based checks are authoritative; avoids false positives from
    # incidental keyword matches in unrelated error messages.
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionResetError, ConnectionRefusedError)):
        return True

    # Provider SDK HTTP status exceptions (httpx / requests / google-genai / openai)
    exc_type = type(exc).__name__
    if exc_type in ("RateLimitError", "APIConnectionError", "ServiceUnavailableError",
                    "InternalServerError", "ResourceExhausted"):
        return True

    # HTTP status code embedded in exception attributes (httpx.HTTPStatusError, etc.)
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status_code, int) and status_code in (429, 500, 502, 503, 529):
        return True

    # Fallback: substring matching for SDK errors not caught above
    msg = str(exc).lower()
    keywords = (
        "rate limit", "ratelimit", "quota", "too many requests",
        "timeout", "timed out", "503", "529", "overloaded",
        "resource_exhausted", "service unavailable", "502", "500",
        "internal server error", "connection reset", "connection refused",
    )
    return any(kw in msg for kw in keywords)
