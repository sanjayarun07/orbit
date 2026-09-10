"""Request admission controls with Redis coordination and local fallback."""

import asyncio
import time
from collections import defaultdict

from app.db import get_redis
from app.settings import settings

_chat_slots = asyncio.Semaphore(settings.max_concurrent_chat_requests)
_local_windows: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
_local_lock = asyncio.Lock()


async def _allow(scope: str, identity: str, limit: int, cost: int = 1) -> tuple[bool, int]:
    """Return admission for a weighted fixed window, coordinated by Redis when present."""
    if limit <= 0:
        return True, 0
    cost = max(1, cost)
    window = int(time.time() // 60)
    redis = await get_redis()
    if redis is not None:
        key = f"rate:{scope}:{identity}:{window}"
        count = await redis.incrby(key, cost)
        if count == cost:
            await redis.expire(key, 61)
    else:
        async with _local_lock:
            local_key = f"{scope}:{identity}"
            previous_window, previous_count = _local_windows[local_key]
            count = previous_count + cost if previous_window == window else cost
            _local_windows[local_key] = (window, count)
            if len(_local_windows) > 10_000:
                stale = [key for key, (item_window, _) in _local_windows.items() if item_window < window]
                for key in stale:
                    _local_windows.pop(key, None)
    retry_after = 60 - int(time.time() % 60)
    return count <= limit, retry_after


async def allow_chat_request(identity: str) -> tuple[bool, int]:
    return await _allow("chat", identity, settings.chat_requests_per_minute)


async def allow_auth_request(identity: str) -> tuple[bool, int]:
    return await _allow("auth", identity, settings.auth_requests_per_minute)


async def allow_rpc_request(identity: str, cost: int = 1) -> tuple[bool, int]:
    return await _allow("rpc", identity, settings.rpc_requests_per_minute, cost)


async def acquire_chat_slot() -> None:
    await asyncio.wait_for(
        _chat_slots.acquire(), timeout=settings.request_queue_timeout_seconds
    )


def release_chat_slot() -> None:
    _chat_slots.release()
