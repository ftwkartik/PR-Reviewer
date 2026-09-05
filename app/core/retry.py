"""Exponential backoff with full jitter for outbound API calls."""

import asyncio
import random
from collections.abc import Awaitable, Callable

import structlog

from app.core.errors import TransientError

log = structlog.get_logger()


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Full jitter: uniform(0, min(cap, base * 2^attempt))."""
    return random.uniform(0, min(cap, base * 2**attempt))  # noqa: S311 - jitter, not crypto


async def retry_transient[T](
    call: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 4,
    base: float = 1.0,
    cap: float = 30.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    operation: str = "call",
) -> T:
    """Retry `call` while it raises TransientError; permanent errors propagate immediately."""
    for attempt in range(max_attempts):
        try:
            return await call()
        except TransientError as exc:
            if attempt == max_attempts - 1:
                raise
            delay = (
                exc.retry_after
                if exc.retry_after is not None
                else backoff_delay(attempt, base, cap)
            )
            log.warning(
                "retrying", operation=operation, attempt=attempt + 1, delay_s=round(delay, 2)
            )
            await sleep(delay)
    raise TransientError(f"{operation}: retries exhausted")  # pragma: no cover
