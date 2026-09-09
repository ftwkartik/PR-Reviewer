import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis


class LockNotAcquiredError(Exception):
    pass


@asynccontextmanager
async def redis_lock(
    redis: Redis, key: str, ttl_s: int = 900, wait_s: float = 0.0
) -> AsyncIterator[None]:
    """Best-effort mutual exclusion (SET NX PX + token-checked release).

    The TTL bounds how long a crashed worker can block others. Correctness of the pipeline
    never relies on the lock alone: DB uniqueness constraints are the real guard.
    """
    token = uuid.uuid4().hex
    deadline = time.monotonic() + wait_s
    while not await redis.set(f"lock:{key}", token, nx=True, px=ttl_s * 1000):
        if time.monotonic() >= deadline:
            raise LockNotAcquiredError(key)
        await asyncio.sleep(1.0)
    try:
        yield
    finally:
        await _release(redis, f"lock:{key}", token)


async def _release(redis: Redis, key: str, token: str) -> None:
    """Delete the lock only if we still own it (optimistic transaction, no Lua needed)."""
    async with redis.pipeline() as pipe:
        try:
            await pipe.watch(key)
            current = await pipe.get(key)
            if current is not None and current.decode() == token:
                pipe.multi()  # type: ignore[no-untyped-call]
                pipe.delete(key)
                await pipe.execute()
        except Exception:  # lost the race or Redis hiccup: the TTL will expire the lock
            return
