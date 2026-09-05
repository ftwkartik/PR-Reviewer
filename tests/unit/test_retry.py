import pytest

from app.core.errors import PermanentError, TransientError
from app.core.retry import backoff_delay, retry_transient


async def test_honors_retry_after_and_stops_on_success() -> None:
    sleeps: list[float] = []
    calls = {"n": 0}

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    async def call() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("x", retry_after=7)
        return "ok"

    assert await retry_transient(call, sleep=fake_sleep) == "ok"
    assert sleeps == [7, 7]


async def test_permanent_not_retried() -> None:
    calls = {"n": 0}

    async def call() -> None:
        calls["n"] += 1
        raise PermanentError("no")

    with pytest.raises(PermanentError):
        await retry_transient(call)
    assert calls["n"] == 1


def test_backoff_bounded_with_jitter() -> None:
    for attempt in range(10):
        assert 0 <= backoff_delay(attempt, base=1, cap=30) <= 30
