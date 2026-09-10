"""Lazy-initialized Postgres pool and Redis client.

Both are optional: if DATABASE_URL/REDIS_URL are unset or unreachable, the
getters return None and callers (plans.py, sessions.py) fall back to
in-process memory so the app keeps working without external infra.
"""

import logging
import asyncio
import time

import asyncpg
import redis.asyncio as redis

from app.settings import settings

logger = logging.getLogger(__name__)

_pg_pool: asyncpg.Pool | None = None
_pg_unavailable = False
_pg_init_lock = asyncio.Lock()
_pg_retry_at = 0.0

_redis_client: redis.Redis | None = None
_redis_unavailable = False
_redis_init_lock = asyncio.Lock()
_redis_retry_at = 0.0

_PLANS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trade_plans (
    plan_id TEXT PRIMARY KEY,
    wallet_address TEXT NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trade_plans_unsettled ON trade_plans (status, created_at);
CREATE TABLE IF NOT EXISTS relay_executions (
    request_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE relay_executions ADD COLUMN IF NOT EXISTS session_id TEXT;
ALTER TABLE relay_executions ADD COLUMN IF NOT EXISTS revision BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS relay_execution_turn ON relay_executions (session_id, revision) WHERE session_id IS NOT NULL;
"""


async def get_pg_pool() -> asyncpg.Pool | None:
    # Startup reconcilers and requests may all arrive together. DDL/pool
    # initialization must occur once, not race across those tasks.
    async with _pg_init_lock:
        return await _initialize_pg_pool()


async def _initialize_pg_pool() -> asyncpg.Pool | None:
    global _pg_pool, _pg_unavailable, _pg_retry_at
    if _pg_unavailable and settings.database_url and time.monotonic() >= _pg_retry_at:
        _pg_unavailable = False
    if _pg_unavailable and not settings.allow_memory_fallback:
        raise RuntimeError("Postgres is unavailable and memory fallback is disabled")
    if _pg_pool is not None or _pg_unavailable:
        return _pg_pool
    if not settings.database_url:
        _pg_unavailable = True
        if not settings.allow_memory_fallback:
            raise RuntimeError("DATABASE_URL is required when memory fallback is disabled")
        return None
    try:
        _pg_pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=settings.postgres_pool_min_size,
            max_size=settings.postgres_pool_max_size,
        )
        async with _pg_pool.acquire() as conn:
            await conn.execute(_PLANS_TABLE_SQL)
    except Exception:
        logger.warning("Postgres unavailable; falling back to in-memory plans")
        if _pg_pool is not None:
            await _pg_pool.close()
        _pg_pool = None
        _pg_unavailable = True
        _pg_retry_at = time.monotonic() + 15
        if not settings.allow_memory_fallback:
            raise RuntimeError("Postgres is required when memory fallback is disabled")
    return _pg_pool


async def get_redis() -> "redis.Redis | None":
    async with _redis_init_lock:
        return await _initialize_redis()


async def _initialize_redis() -> "redis.Redis | None":
    global _redis_client, _redis_unavailable, _redis_retry_at
    if _redis_unavailable and settings.redis_url and time.monotonic() >= _redis_retry_at:
        _redis_unavailable = False
    if _redis_unavailable and not settings.allow_memory_fallback:
        raise RuntimeError("Redis is unavailable and memory fallback is disabled")
    if _redis_client is not None or _redis_unavailable:
        return _redis_client
    if not settings.redis_url:
        _redis_unavailable = True
        _redis_retry_at = time.monotonic() + 15
        if not settings.allow_memory_fallback:
            raise RuntimeError("REDIS_URL is required when memory fallback is disabled")
        return None
    try:
        client = redis.from_url(settings.redis_url, decode_responses=True)
        await client.ping()
        _redis_client = client
    except Exception:
        logger.warning("Redis unavailable; falling back to in-memory sessions")
        _redis_client = None
        _redis_unavailable = True
        _redis_retry_at = time.monotonic() + 15
        if not settings.allow_memory_fallback:
            raise RuntimeError("Redis is required when memory fallback is disabled")
    return _redis_client
