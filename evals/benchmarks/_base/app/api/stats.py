import asyncio

from fastapi import APIRouter

router = APIRouter()
COUNTERS: dict[str, int] = {}
_lock = asyncio.Lock()


@router.post("/stats/{name}")
async def bump(name: str):
    async with _lock:
        COUNTERS[name] = COUNTERS.get(name, 0) + 1
        return {"name": name, "count": COUNTERS[name]}
