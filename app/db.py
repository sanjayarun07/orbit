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
-- Stripe events arrive late and out of order. Keeping the active
-- subscription's creation time lets a stale event for a superseded
-- subscription be recognised and ignored instead of overwriting it.
ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_created BIGINT;
-- The envelope time of the last subscription-state event applied, so a
-- later-delivered but earlier-issued event for the SAME subscription is
-- recognised as stale. A subscription's own creation time cannot do that.
ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_event_at BIGINT;
-- A subscription checkout in flight: recorded when the session is created and
-- cleared when its completion webhook lands, so a second subscription checkout
-- cannot be started underneath the first before Stripe has reported either.
ALTER TABLE users ADD COLUMN IF NOT EXISTS checkout_session_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS checkout_started_at BIGINT;
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
-- One active membership per user, enforced by the database and not only by
-- the code that tries to keep it so. Earlier versions allowed several, so the
-- data is reconciled before the constraint the first time this runs (when the
-- index does not exist yet): of a user's active memberships the one their
-- team pointer names survives -- with no pointer to any of them, the most
-- recently accepted -- and the rest are moved to team_members_reconciled,
-- not lost. The pointer is then set to the survivor. Without this, a database
-- carrying the duplicates could not be initialised at all.
CREATE TABLE IF NOT EXISTS team_members_reconciled (
    owner_id UUID NOT NULL,
    email TEXT NOT NULL,
    role TEXT,
    status TEXT,
    user_id UUID,
    invited_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    reason TEXT NOT NULL,
    reconciled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
DO $$ BEGIN
    IF to_regclass('team_members_one_active') IS NULL THEN
        WITH ranked AS (
            SELECT m.owner_id, m.email,
                   ROW_NUMBER() OVER (
                       PARTITION BY m.user_id
                       ORDER BY (m.owner_id = u.team_owner_id) IS TRUE DESC, m.accepted_at DESC NULLS LAST, m.invited_at DESC
                   ) AS rank
            FROM team_members m JOIN users u ON u.id = m.user_id
            WHERE m.status = 'active' AND m.user_id IS NOT NULL
        ), removed AS (
            DELETE FROM team_members m USING ranked r
            WHERE m.owner_id = r.owner_id AND m.email = r.email AND r.rank > 1
            RETURNING m.owner_id, m.email, m.role, m.status, m.user_id, m.invited_at, m.accepted_at
        )
        INSERT INTO team_members_reconciled (owner_id, email, role, status, user_id, invited_at, accepted_at, reason)
        SELECT owner_id, email, role, status, user_id, invited_at, accepted_at, 'duplicate active membership before team_members_one_active'
        FROM removed;
        UPDATE users u SET team_owner_id = m.owner_id
        FROM team_members m
        WHERE m.user_id = u.id AND m.status = 'active' AND u.team_owner_id IS DISTINCT FROM m.owner_id;
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS team_members_one_active ON team_members (user_id) WHERE status = 'active' AND user_id IS NOT NULL;
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
-- Attempts made at the current occurrence; a bounded retry refunds and moves on.
ALTER TABLE user_tasks ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
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
-- The inbox row is the durable delivery record for a task occurrence, so a
-- retry of an occurrence that already delivered is a no-op.
ALTER TABLE user_inbox ADD COLUMN IF NOT EXISTS occurrence TEXT;
-- An item can carry a one-tap action (an armed exit's prepared sell line) and the receipt it came from.
ALTER TABLE user_inbox ADD COLUMN IF NOT EXISTS data JSONB;
CREATE UNIQUE INDEX IF NOT EXISTS user_inbox_occurrence ON user_inbox (task_id, occurrence) WHERE occurrence IS NOT NULL;
CREATE TABLE IF NOT EXISTS user_wallets (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (chain, address)
);
CREATE INDEX IF NOT EXISTS user_wallets_user ON user_wallets (user_id);
-- An EOA address is the same key on every EVM network, so one account owns it
-- everywhere. A contract wallet's address is not key-derived: the same address
-- on two networks can be two different contracts with two different owners, so
-- those identities are scoped to the network they were verified on. Rows
-- linked before this column existed default to 'eoa', which is what they were
-- treated as; a pre-existing smart-wallet link keeps its old, broader scope.
ALTER TABLE user_wallets ADD COLUMN IF NOT EXISTS wallet_type TEXT NOT NULL DEFAULT 'eoa';
CREATE TABLE IF NOT EXISTS user_identities (
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    display TEXT,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (provider, external_id)
);
CREATE INDEX IF NOT EXISTS user_identities_user ON user_identities (user_id);
-- A messaging account is a way IN to an Orbit account, exactly as a wallet is.
-- The primary key is the pair, so one Telegram user maps to one Orbit account
-- and re-linking moves the pointer rather than forking a second account. The
-- provider column is here so the next surface (Discord, Slack) needs no
-- migration; `display` is the @handle for the settings screen only -- it can
-- change under the user at any time and is never an identifier.
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
CREATE TABLE IF NOT EXISTS oauth_clients (
    provider TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    client_secret TEXT,
    redirect_uri TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS user_integrations (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at DOUBLE PRECISION,
    scope TEXT,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, provider)
);
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
ALTER TABLE chat_feedback ADD COLUMN IF NOT EXISTS principal_id TEXT;   -- who rated: a team member, not the owner's billing account
CREATE INDEX IF NOT EXISTS chat_feedback_principal ON chat_feedback (principal_id);
-- Deleted accounts (also declared by app/turn_log.py): created here as well so
-- the marker exists from the first pool, whichever module touches it first.
CREATE TABLE IF NOT EXISTS deleted_users (
    user_id TEXT PRIMARY KEY,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Rows from before the column existed: owned by whoever owns the conversation
-- they rate (user_chat_sessions is the verified ownership). Idempotent.
-- The app's principal id is "user:<uuid>" (app/identity.py); an earlier backfill wrote the bare uuid.
UPDATE chat_feedback SET principal_id = 'user:' || principal_id WHERE principal_id ~ '^[0-9a-fA-F-]{36}$';
UPDATE chat_feedback f SET principal_id = 'user:' || s.user_id::text FROM user_chat_sessions s WHERE f.principal_id IS NULL AND s.session_id = f.session_id;
"""


SCHEMA_LOCK = "orbit-schema"


async def apply_schema(pool, sql: str) -> None:
    """Run schema statements (CREATE ... IF NOT EXISTS, ADD COLUMN IF NOT
    EXISTS) under one deployment-wide advisory lock, in a transaction. Every
    worker of every replica runs the same statements at startup; two of them
    running at once deadlocked on a managed-store boot with two uvicorn
    workers (2026-09-22). The lock serialises them; the statements are
    idempotent, so the second runner finds everything in place."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", SCHEMA_LOCK)
            await conn.execute(sql)


class schema_lock:
    """The same lock as a session-level context manager, for schema work
    that cannot be one transaction (the knowledge schema tries the vector
    extension and falls back)."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        await self.conn.execute("SELECT pg_advisory_lock(hashtext($1))", SCHEMA_LOCK)
        return self.conn

    async def __aexit__(self, *exc):
        await self.conn.execute("SELECT pg_advisory_unlock(hashtext($1))", SCHEMA_LOCK)
        return False


def memory_is_the_store() -> bool:
    """Is process memory the configured store (no DATABASE_URL: tests and
    keyless dev), rather than a fallback for a database that is down? A
    store may keep a person's words in memory only in the first case. When
    Postgres is configured and a write fails, the memory copy keeps no
    private content and no owner -- a deletion later scrubs the database,
    and cannot reach a fallback copy on another worker (review, 2026-09-22)."""
    return not settings.database_url


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
        await apply_schema(_pg_pool, _PLANS_TABLE_SQL)
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
