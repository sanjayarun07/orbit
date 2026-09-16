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
# The event loop the cached async client + lock belong to. A redis.asyncio client
# binds its connections to the loop that created it; production runs one loop for
# the process lifetime, but anything that spins up a fresh loop (tests calling
# asyncio.run per assertion, a worker that restarts its loop) would otherwise
# reuse a client bound to a now-closed loop -> "Event loop is closed". We rebind
# when the running loop changes.
_redis_loop: "asyncio.AbstractEventLoop | None" = None

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
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY,
    email TEXT UNIQUE,
    display_name TEXT,
    plan_id TEXT NOT NULL DEFAULT 'free',
    stripe_customer_id TEXT UNIQUE,
    preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS team_owner_id UUID;
ALTER TABLE users ALTER COLUMN email DROP NOT NULL;   -- a wallet-only account has none
CREATE TABLE IF NOT EXISTS user_chat_sessions (
    session_id TEXT PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_used TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS user_chat_sessions_user ON user_chat_sessions (user_id, last_used DESC);
CREATE TABLE IF NOT EXISTS team_members (
    owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    status TEXT NOT NULL DEFAULT 'invited',
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    invited_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_at TIMESTAMPTZ,
    PRIMARY KEY (owner_id, email)
);
CREATE TABLE IF NOT EXISTS stripe_events (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS user_tasks (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    spec JSONB NOT NULL DEFAULT '{}'::jsonb,
    schedule JSONB NOT NULL DEFAULT '{}'::jsonb,
    channel TEXT NOT NULL DEFAULT 'inapp',
    status TEXT NOT NULL DEFAULT 'active',
    tz_offset_min INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    next_run_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ,
    last_result TEXT,
    fire_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS user_tasks_due ON user_tasks (next_run_at) WHERE status = 'active';
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS claimed_until TIMESTAMPTZ;
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS claimed_occurrence TEXT;
DO $$ BEGIN
    -- Scoped to the relation the search_path resolves (the one CREATE TABLE above targets), not every schema.
    IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'stripe_events'::regclass AND attname = 'processed_at' AND NOT attisdropped) THEN
        ALTER TABLE stripe_events ADD COLUMN processed_at TIMESTAMPTZ;
        UPDATE stripe_events SET processed_at = received_at;   -- everything recorded before this column existed was handled
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS user_tasks_user ON user_tasks (user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS user_inbox (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'task',
    task_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    read_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS user_inbox_user ON user_inbox (user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS user_wallets (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chain, address)
);
CREATE INDEX IF NOT EXISTS user_wallets_user ON user_wallets (user_id);
CREATE TABLE IF NOT EXISTS credit_ledger (
    id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (account_id, ref_type, ref_id)
);
CREATE INDEX IF NOT EXISTS credit_ledger_account ON credit_ledger (account_id, created_at DESC);
CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    prefix TEXT NOT NULL,
    key_hash TEXT UNIQUE NOT NULL,
    scopes TEXT[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS api_keys_user ON api_keys (user_id);
CREATE TABLE IF NOT EXISTS tool_outcomes (
    tool_name TEXT NOT NULL,
    day DATE NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tool_name, day)
);
ALTER TABLE tool_outcomes ADD COLUMN IF NOT EXISTS feedback_up INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tool_outcomes ADD COLUMN IF NOT EXISTS feedback_down INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS chat_feedback (
    session_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    rating TEXT NOT NULL,
    comment TEXT,
    account_id TEXT,
    tools TEXT[] NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (session_id, revision)
);
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
    global _redis_loop, _redis_init_lock, _redis_client, _redis_unavailable, _redis_retry_at
    loop = asyncio.get_running_loop()
    if _redis_loop is not loop:
        # New loop: the cached client and lock are bound to the previous one and
        # cannot be reused. Drop the stale client (it cannot be awaited-closed on
        # a dead loop) and rebind fresh state to this loop.
        _redis_loop = loop
        _redis_init_lock = asyncio.Lock()
        _redis_client = None
        _redis_unavailable = False
        _redis_retry_at = 0.0
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
