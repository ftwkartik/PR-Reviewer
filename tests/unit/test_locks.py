import fakeredis
import pytest

from app.core.locks import LockNotAcquiredError, redis_lock


async def test_mutual_exclusion_and_release() -> None:
    r = fakeredis.FakeAsyncRedis()
    async with redis_lock(r, "k"):
        with pytest.raises(LockNotAcquiredError):
            async with redis_lock(r, "k"):
                pass
    async with redis_lock(r, "k"):  # released after the first block
        pass


async def test_does_not_release_someone_elses_lock() -> None:
    r = fakeredis.FakeAsyncRedis()
    async with redis_lock(r, "k"):
        await r.set("lock:k", "other-owner")
    assert await r.get("lock:k") == b"other-owner"


async def test_wait_acquires_after_release(monkeypatch: pytest.MonkeyPatch) -> None:
    r = fakeredis.FakeAsyncRedis()
    await r.set("lock:k", "x", px=50)
    async with redis_lock(r, "k", wait_s=5):
        pass
