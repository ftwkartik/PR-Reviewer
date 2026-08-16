import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""


class LockNotAcquiredError(Exception):
    pass


@asynccontextmanager
async def redis_lock(redis: Redis, key: str, ttl_s: int = 900) -> AsyncIterator[None]:
    """Best-effort mutual exclusion (SET NX PX + token-checked release).

    The TTL bounds how long a crashed worker can block others. Correctness of the pipeline
    never relies on the lock alone: DB uniqueness constraints are the real guard.
    """
    token = uuid.uuid4().hex
    if not await redis.set(f"lock:{key}", token, nx=True, px=ttl_s * 1000):
        raise LockNotAcquiredError(key)
    try:
        yield
    finally:
        await redis.eval(_RELEASE, 1, f"lock:{key}", token)  # type: ignore[misc]
