"""Request admission controls with Redis coordination and local fallback."""

import asyncio
import time
from collections import defaultdict

from app.db import get_redis
from app.settings import settings

_chat_slots = asyncio.Semaphore(settings.max_concurrent_chat_requests)
_knowledge_slots = asyncio.Semaphore(settings.max_concurrent_knowledge_searches)
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


async def allow_chat_request(identity: str, plan_limit: int | None = None) -> tuple[bool, int]:
    """The caller's own chat bucket, at the rate their plan publishes (the
    global setting stays as the ceiling). Plans advertise different rates;
    admission used to ignore them and apply the global number to everyone."""
    limit = settings.chat_requests_per_minute
    if plan_limit and plan_limit > 0:
        limit = min(limit, plan_limit)
    return await _allow("chat", identity, limit)


async def allow_chat_request_from_ip(ip: str) -> tuple[bool, int]:
    """A second chat bucket keyed by network address. The identity bucket is
    keyed by a device id the caller chooses, so rotating that id gave a fresh
    bucket each time; this one cannot be rotated from the client."""
    return await _allow("chat-ip", ip, settings.chat_requests_per_minute_per_ip)


async def allow_knowledge_search(identity: str, ip: str) -> tuple[bool, int]:
    """Admission for the public knowledge route: the caller's own bucket and
    the network bucket, at the chat rates."""
    allowed, retry = await _allow("kb", identity, settings.chat_requests_per_minute)
    if not allowed:
        return False, retry
    return await _allow("kb-ip", ip, settings.chat_requests_per_minute_per_ip)


async def spend_daily_budget(scope: str, limit: int, cost: int = 1) -> bool:
    """A cumulative ceiling for the whole deployment, per UTC day -- the thing
    a per-request rate limit does not give you when the request costs money."""
    if limit <= 0:
        return True
    day = int(time.time() // 86400)
    redis = await get_redis()
    if redis is not None:
        key = f"budget:{scope}:{day}"
        count = await redis.incrby(key, max(1, cost))
        if count == cost:
            await redis.expire(key, 86400 * 2)
        return count <= limit
    async with _local_lock:
        previous_day, previous_count = _local_windows[f"budget:{scope}"]
        count = previous_count + max(1, cost) if previous_day == day else max(1, cost)
        _local_windows[f"budget:{scope}"] = (day, count)
        return count <= limit


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
