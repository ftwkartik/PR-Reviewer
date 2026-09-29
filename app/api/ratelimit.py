"""Fixed-window rate limiting in Redis, keyed by API key (hashed) or client IP.

Fails OPEN if Redis is unreachable: losing rate limiting briefly is better than taking the API
down; every other abuse control (API key, body caps, per-repo budget) still applies.
"""

import hashlib
import time
from typing import Annotated

import structlog
from fastapi import Depends, Header, HTTPException, Request, status
from redis.asyncio import Redis

from app.api.dependencies import SettingsDep

log = structlog.get_logger()
WINDOW_S = 60
_redis: Redis | None = None


def get_redis(settings: SettingsDep) -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url, socket_timeout=1.0, socket_connect_timeout=1.0)
    return _redis


async def rate_limit(
    request: Request,
    settings: SettingsDep,
    redis: Annotated[Redis, Depends(get_redis)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return
    ident = (
        "k:" + hashlib.sha256(x_api_key.encode()).hexdigest()[:16]
        if x_api_key
        else f"ip:{request.client.host if request.client else 'unknown'}"
    )
    bucket = int(time.time() // WINDOW_S)
    key = f"rl:{ident}:{bucket}"
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, WINDOW_S * 2)
    except Exception:
        log.warning("rate_limit_redis_unavailable")
        return
    if count > limit:
        retry = WINDOW_S - int(time.time()) % WINDOW_S
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate limit exceeded",
            headers={"Retry-After": str(retry)},
        )
