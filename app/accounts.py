"""User accounts: email magic-link sign-in, user sessions, linked wallets and
server-side preferences.

Storage follows the repo contract: Postgres when configured, otherwise
in-process memory (sessions and magic-link tokens go to Redis when present).
Nothing here ever handles a password -- sign-in is a one-time emailed link or a
wallet signature (app/wallet_auth.py), and a wallet is linked to the signed-in
user rather than being an account of its own.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from uuid import uuid4

from app import emailer
from app.db import get_pg_pool, get_redis
from app.settings import settings

logger = logging.getLogger(__name__)

USER_COOKIE = "orbit_user"
USER_SESSION_TTL = 30 * 24 * 60 * 60
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# In-memory fallbacks (single process). Keyed the same way the Postgres/Redis
# rows are so the two paths behave identically.
_users: dict[str, dict] = {}
_users_by_email: dict[str, str] = {}
_wallets: dict[tuple[str, str], tuple[str, str, str]] = {}  # (chain, address_lower) -> (user_id, address, wallet_type)
_magic_tokens: dict[str, tuple[float, dict]] = {}
_user_sessions: dict[str, tuple[float, dict]] = {}
_chat_sessions: dict[str, dict[str, str]] = {}  # user_id -> {session_id: last_used}
_team_members: dict[tuple[str, str], dict] = {}  # (owner_id, email) -> member record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_email(email: str) -> str:
    value = (email or "").strip().lower()
    if not _EMAIL.match(value) or len(value) > 254:
        raise ValueError("Enter a valid email address")
    return value


def _row_to_user(row) -> dict:
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "plan_id": row["plan_id"],
        "stripe_customer_id": row["stripe_customer_id"],
        "stripe_subscription_id": row.get("stripe_subscription_id") if hasattr(row, "get") else row["stripe_subscription_id"],
        "subscription_status": row.get("subscription_status") if hasattr(row, "get") else row["subscription_status"],
        "subscription_created": (row.get("subscription_created") if hasattr(row, "get") else row["subscription_created"]),
        "subscription_event_at": (row.get("subscription_event_at") if hasattr(row, "get") else row["subscription_event_at"]),
        "checkout_session_id": (row.get("checkout_session_id") if hasattr(row, "get") else row["checkout_session_id"]),
        "checkout_started_at": (row.get("checkout_started_at") if hasattr(row, "get") else row["checkout_started_at"]),
        "team_owner_id": str(row["team_owner_id"]) if (row.get("team_owner_id") if hasattr(row, "get") else row["team_owner_id"]) else None,
        "preferences": json.loads(row["preferences"]) if isinstance(row["preferences"], str) else (row["preferences"] or {}),
        "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else row["created_at"],
    }


# ----------------------------------------------------------------------------
# Users
# ----------------------------------------------------------------------------

_user_locks: dict[tuple[int, str], "asyncio.Lock"] = {}


@asynccontextmanager
async def user_row_lock(user_id: str):
    """Serialise state transitions on one account: yields (connection, user).

    Every billing writer used to read a user snapshot, decide, and then write
    unconditionally; two webhooks could both pass the check against the same
    snapshot and the older one, finishing last, overwrote the newer plan. Team
    joins had the same shape. Under Postgres this is `SELECT ... FOR UPDATE` on
    the user row, and every write inside rides the same connection -- never a
    second one from the pool, which is how the task gate once deadlocked. Without
    Postgres it is an asyncio lock keyed by (loop, user)."""
    pool = await get_pg_pool()
    if pool is not None:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("SELECT * FROM users WHERE id = $1 FOR UPDATE", user_id)
                yield conn, (_row_to_user(row) if row else None)
        return
    import asyncio
    lock = _user_locks.setdefault((id(asyncio.get_running_loop()), user_id), asyncio.Lock())
    async with lock:
        yield None, (dict(_users[user_id]) if user_id in _users else None)


async def get_user(user_id: str, db=None) -> dict | None:
    pool = await get_pg_pool()
    if pool is not None:
        row = await (db or pool).fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        return _row_to_user(row) if row else None
    return dict(_users[user_id]) if user_id in _users else None


async def get_user_by_email(email: str) -> dict | None:
    email = normalize_email(email)
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow("SELECT * FROM users WHERE email = $1", email)
        return _row_to_user(row) if row else None
    user_id = _users_by_email.get(email)
    return dict(_users[user_id]) if user_id else None


async def search_users(query: str = "", limit: int = 25) -> list[dict]:
    """Newest accounts first, optionally filtered by an email substring."""
    query = (query or "").strip().lower()
    limit = max(1, min(int(limit), 200))
    pool = await get_pg_pool()
    if pool is not None:
        rows = await pool.fetch(
            "SELECT * FROM users WHERE ($1 = '' OR email LIKE '%' || $1 || '%') ORDER BY created_at DESC LIMIT $2", query, limit,
        )
        return [_row_to_user(row) for row in rows]
    users = [dict(u) for u in _users.values() if not query or query in (u.get("email") or "")]
    return sorted(users, key=lambda u: u.get("created_at") or "", reverse=True)[:limit]


async def user_stats() -> dict:
    """Counts by plan and subscription status, plus sign-ups per day (30d)."""
    pool = await get_pg_pool()
    if pool is not None:
        by_plan = {r["plan_id"]: int(r["n"]) for r in await pool.fetch("SELECT plan_id, COUNT(*) AS n FROM users GROUP BY plan_id")}
        by_status = {r["subscription_status"] or "none": int(r["n"]) for r in await pool.fetch("SELECT subscription_status, COUNT(*) AS n FROM users GROUP BY subscription_status")}
        signups = {r["day"].isoformat(): int(r["n"]) for r in await pool.fetch(
            "SELECT created_at::date AS day, COUNT(*) AS n FROM users WHERE created_at >= NOW() - INTERVAL '30 days' GROUP BY 1 ORDER BY 1")}
        total = int(await pool.fetchval("SELECT COUNT(*) FROM users"))
        members = int(await pool.fetchval("SELECT COUNT(*) FROM users WHERE team_owner_id IS NOT NULL"))
    else:
        by_plan: dict[str, int] = {}
        by_status: dict[str, int] = {}
        signups: dict[str, int] = {}
        for u in _users.values():
            by_plan[u.get("plan_id") or "free"] = by_plan.get(u.get("plan_id") or "free", 0) + 1
            status = u.get("subscription_status") or "none"
            by_status[status] = by_status.get(status, 0) + 1
            day = (u.get("created_at") or "")[:10]
            if day:
                signups[day] = signups.get(day, 0) + 1
        total = len(_users)
        members = sum(1 for u in _users.values() if u.get("team_owner_id"))
    return {"total": total, "by_plan": by_plan, "by_subscription_status": by_status, "signups_by_day": signups, "team_members": members}


async def get_or_create_user(email: str) -> tuple[dict, bool]:
    """(user, created). New users start on the Free plan."""
    email = normalize_email(email)
    existing = await get_user_by_email(email)
    if existing:
        return existing, False
    user = {
        "id": str(uuid4()),
        "email": email,
        "display_name": None,
        "plan_id": "free",
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
        "subscription_status": None,
        "subscription_created": None,
        "subscription_event_at": None,
        "checkout_session_id": None,
        "checkout_started_at": None,
        "team_owner_id": None,
        "preferences": {},
        "created_at": _now(),
    }
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow(
            """
            INSERT INTO users (id, email, display_name, plan_id, preferences)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
            RETURNING *
            """,
            user["id"], email, None, "free", "{}",
        )
        stored = _row_to_user(row)
        return stored, stored["id"] == user["id"]
    _users[user["id"]] = user
    _users_by_email[email] = user["id"]
    return dict(user), True


async def get_user_by_stripe_customer(customer_id: str) -> dict | None:
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow("SELECT * FROM users WHERE stripe_customer_id = $1", customer_id)
        return _row_to_user(row) if row else None
    for user in _users.values():
        if user.get("stripe_customer_id") == customer_id:
            return dict(user)
    return None


async def update_user(user_id: str, db=None, **fields) -> dict | None:
    """Update a whitelisted set of columns; preferences are merged, not replaced.
    `db` is a connection already holding this user's row lock (see
    user_row_lock); the write must ride it rather than ask the pool."""
    allowed = {"display_name", "plan_id", "stripe_customer_id", "stripe_subscription_id", "subscription_status", "subscription_created", "subscription_event_at", "checkout_session_id", "checkout_started_at", "preferences", "team_owner_id"}
    changes = {key: value for key, value in fields.items() if key in allowed}
    if not changes:
        return await get_user(user_id)
    pool = await get_pg_pool()
    if pool is not None:
        current = await get_user(user_id, db=db)
        if current is None:
            return None
        if "preferences" in changes:
            changes["preferences"] = json.dumps({**current.get("preferences", {}), **(changes["preferences"] or {})})
        assignments = ", ".join(f"{key} = ${index}" for index, key in enumerate(changes, start=2))
        row = await (db or pool).fetchrow(
            f"UPDATE users SET {assignments} WHERE id = $1 RETURNING *", user_id, *changes.values()
        )
        return _row_to_user(row) if row else None
    user = _users.get(user_id)
    if user is None:
        return None
    if "preferences" in changes:
        changes["preferences"] = {**user.get("preferences", {}), **(changes["preferences"] or {})}
    user.update(changes)
    return dict(user)


# ----------------------------------------------------------------------------
# Wallets linked to a user
# ----------------------------------------------------------------------------

def is_evm_chain(chain: str) -> bool:
    """Every chain label except Solana names an EVM network.

    This matters for identity, not just naming: an EVM address is the same
    secp256k1 key on Ethereum, Base, Arbitrum and everywhere else, so the same
    wallet must be the same account regardless of which network it happened to
    be connected to when it signed in. Solana is a different curve and a
    different address space, so it stays separate.
    """
    return chain != "solana"


EOA = "eoa"
CONTRACT = "contract"


def spans_evm_networks(chain: str, wallet_type: str) -> bool:
    """Is this identity the same on every EVM network, or only on its own?

    An EOA address is derived from a secp256k1 key, so the holder controls it
    identically on Ethereum, Base, Arbitrum and the rest -- one account owns it
    everywhere. A contract wallet's address is derived from its deployer and
    nonce, not from a key: the same address on two networks can be two entirely
    different contracts with two different controllers. Treating those as one
    identity meant authenticating against a contract you control on one network
    could resume an account linked to somebody else's contract on another.
    """
    return is_evm_chain(chain) and wallet_type != CONTRACT


async def link_wallet(user_id: str, chain: str, address: str, wallet_type: str = EOA) -> None:
    # The stored chain is provenance -- the network this wallet first signed in
    # from, which is what Settings displays. For an EOA, identity is resolved by
    # find_wallet_owner across networks, so a second network for an address
    # already linked to this account adds nothing and would only show as a
    # duplicate row. A contract wallet is network-scoped, so each network it is
    # verified on is a separate, real link.
    if spans_evm_networks(chain, wallet_type):
        existing = await find_wallet_owner(chain, address, wallet_type)
        if existing is not None and existing["id"] == user_id:
            return
    pool = await get_pg_pool()
    if pool is not None:
        await pool.execute(
            """
            INSERT INTO user_wallets (user_id, chain, address, wallet_type) VALUES ($1, $2, $3, $4)
            ON CONFLICT (chain, address) DO UPDATE SET user_id = EXCLUDED.user_id, wallet_type = EXCLUDED.wallet_type, linked_at = NOW()
            """,
            user_id, chain, address, wallet_type,
        )
        return
    _wallets[(chain, address.lower())] = (user_id, address, wallet_type)


async def list_wallets(user_id: str) -> list[dict]:
    pool = await get_pg_pool()
    if pool is not None:
        rows = await pool.fetch("SELECT chain, address, wallet_type, linked_at FROM user_wallets WHERE user_id = $1 ORDER BY linked_at", user_id)
        return [{"chain": r["chain"], "address": r["address"], "wallet_type": r["wallet_type"], "linked_at": r["linked_at"].isoformat()} for r in rows]
    return [{"chain": chain, "address": address, "wallet_type": kind, "linked_at": None}
            for (chain, _), (owner, address, kind) in _wallets.items() if owner == user_id]


async def get_user_by_wallet(chain: str, address: str) -> dict | None:
    """The account a wallet is linked to, or None. Case-sensitive on `address`
    for EVM checksum forms -- callers normalize (lowercase for EVM, as-is
    base58 for Solana) before calling, matching link_wallet's own key."""
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow(
            "SELECT u.* FROM users u JOIN user_wallets w ON w.user_id = u.id WHERE w.chain = $1 AND w.address = $2",
            chain, address,
        )
        return _row_to_user(row) if row else None
    owner = _wallets.get((chain, address.lower()))
    return dict(_users[owner[0]]) if owner and owner[0] in _users else None


async def find_wallet_owner(chain: str, address: str, wallet_type: str = EOA) -> dict | None:
    """The account this wallet signs in as -- the identity lookup.

    Differs from get_user_by_wallet, which matches one exact (chain, address)
    row, in the one way that matters: for EVM it matches the address on *any*
    EVM network. Without that, the same MetaMask wallet signing in on Base
    instead of Ethereum, or through the Coinbase entry point (which labels the
    chain "evm") instead of the picker, resolved to no owner and minted a
    second account -- silently splitting one person's history, credits and
    plan in two.
    """
    pool = await get_pg_pool()
    if pool is not None:
        if spans_evm_networks(chain, wallet_type):
            # An EOA: one key, every EVM network. Match other EOA rows only --
            # a contract row on another network is a different controller.
            row = await pool.fetchrow(
                "SELECT u.* FROM users u JOIN user_wallets w ON w.user_id = u.id "
                "WHERE w.chain <> 'solana' AND w.wallet_type <> 'contract' AND w.address = $1 "
                "ORDER BY w.linked_at LIMIT 1",
                address,
            )
        else:
            # Solana, or a contract wallet: this exact network only.
            row = await pool.fetchrow(
                "SELECT u.* FROM users u JOIN user_wallets w ON w.user_id = u.id "
                "WHERE w.chain = $1 AND w.address = $2 LIMIT 1",
                chain, address,
            )
        return _row_to_user(row) if row else None
    for (stored_chain, stored_address), entry in _wallets.items():
        owner_id, _, stored_type = entry
        if stored_address != address.lower():
            continue
        if spans_evm_networks(chain, wallet_type):
            if not is_evm_chain(stored_chain) or stored_type == CONTRACT:
                continue
        elif stored_chain != chain:
            continue
        return dict(_users[owner_id]) if owner_id in _users else None
    return None


async def create_wallet_user(chain: str, address: str, wallet_type: str = EOA) -> dict:
    """A brand-new account with no email, for a wallet no one has linked yet.
    Callers that need "find-or-create" must check find_wallet_owner first (not
    get_user_by_wallet, which matches one exact chain label and so misses the
    same EVM address on another network) -- a wallet already linked always
    resolves to its existing account; this never reassigns one."""
    user = {
        "id": str(uuid4()), "email": None, "display_name": None, "plan_id": "free",
        "stripe_customer_id": None, "stripe_subscription_id": None, "subscription_status": None,
        "team_owner_id": None, "preferences": {}, "created_at": _now(),
    }
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow(
            "INSERT INTO users (id, email, display_name, plan_id, preferences) VALUES ($1, NULL, NULL, 'free', '{}'::jsonb) RETURNING *",
            user["id"],
        )
        stored = _row_to_user(row)
    else:
        _users[user["id"]] = user
        stored = dict(user)
    await link_wallet(stored["id"], chain, address, wallet_type)
    return stored


async def sign_in_with_wallet(chain: str, address: str, ip: str | None = None, user_agent: str | None = None, wallet_type: str = EOA) -> tuple[dict, str, bool]:
    """A verified wallet signature -> (user, session token, created). The
    wallet's existing owner, if any, always wins -- this never creates a
    second account for a wallet that's already linked to one."""
    existing = await find_wallet_owner(chain, address, wallet_type)
    user = existing or await create_wallet_user(chain, address, wallet_type)
    session = await create_user_session(user["id"], ip, user_agent)
    return user, session, existing is None


# ----------------------------------------------------------------------------
# Magic-link sign-in
# ----------------------------------------------------------------------------

def _prune_memory() -> None:
    now = time.time()
    for store in (_magic_tokens, _user_sessions):
        for key in [k for k, (expires, _) in store.items() if expires <= now]:
            store.pop(key, None)


async def start_email_signin(email: str, base_url: str) -> dict:
    """Issue a one-time sign-in token and email the link. Returns what the UI
    needs: whether an email went out and, only when development exposure is on
    and no email provider is configured, the link itself."""
    email = normalize_email(email)
    token = secrets.token_urlsafe(32)
    value = {"email": email, "issued_at": _now()}
    ttl = settings.magic_link_ttl_minutes * 60
    redis = await get_redis()
    if redis is not None:
        await redis.setex(f"magic_link:{token}", ttl, json.dumps(value))
    else:
        _prune_memory()
        _magic_tokens[token] = (time.time() + ttl, value)
    link = f"{base_url.rstrip('/')}/ui/?signin={token}"
    sent = await emailer.send_email(
        email, f"Sign in to {settings.product_name}", emailer.magic_link_html(link, settings.product_name),
        text=f"Sign in to {settings.product_name}: {link}",
    )
    result = {"sent": sent, "email": email}
    if not sent and settings.dev_expose_magic_links:
        result["dev_link"] = link
    return result


async def consume_magic_token(token: str) -> str:
    """Return the email for a valid, unused token and burn it."""
    if not token or len(token) > 128:
        raise ValueError("Invalid sign-in link")
    redis = await get_redis()
    if redis is not None:
        raw = await redis.getdel(f"magic_link:{token}")
        if not raw:
            raise ValueError("This sign-in link has expired or was already used")
        return json.loads(raw)["email"]
    _prune_memory()
    entry = _magic_tokens.pop(token, None)
    if entry is None:
        raise ValueError("This sign-in link has expired or was already used")
    return entry[1]["email"]


# ----------------------------------------------------------------------------
# User sessions (cookie)
# ----------------------------------------------------------------------------

def _session_record(user_id: str, ip: str | None, user_agent: str | None) -> dict:
    return {"user_id": user_id, "created_at": _now(), "ip": ip, "user_agent": (user_agent or "")[:200]}


async def create_user_session(user_id: str, ip: str | None = None, user_agent: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    record = _session_record(user_id, ip, user_agent)
    redis = await get_redis()
    if redis is not None:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.setex(f"user_session:{token}", USER_SESSION_TTL, json.dumps(record))
            pipe.sadd(f"user_sessions_index:{user_id}", token)
            pipe.expire(f"user_sessions_index:{user_id}", USER_SESSION_TTL)
            await pipe.execute()
    else:
        _prune_memory()
        _user_sessions[token] = (time.time() + USER_SESSION_TTL, record)
    return token


def _parse_session(raw) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"user_id": str(value)}
    except (TypeError, ValueError):
        return {"user_id": str(raw)}  # legacy: bare user id


async def get_session_user(token: str | None) -> dict | None:
    if not token:
        return None
    redis = await get_redis()
    if redis is not None:
        record = _parse_session(await redis.get(f"user_session:{token}"))
    else:
        _prune_memory()
        entry = _user_sessions.get(token)
        record = entry[1] if entry else None
    return await get_user(record["user_id"]) if record and record.get("user_id") else None


async def list_user_sessions(user_id: str, current_token: str | None = None) -> list[dict]:
    """Active sign-ins for the account (device summary, never the token)."""
    out: list[dict] = []
    redis = await get_redis()
    if redis is not None:
        tokens = [t.decode() if isinstance(t, bytes) else t for t in await redis.smembers(f"user_sessions_index:{user_id}")]
        for token in tokens:
            record = _parse_session(await redis.get(f"user_session:{token}"))
            if record is None:
                await redis.srem(f"user_sessions_index:{user_id}", token)
                continue
            out.append({**record, "current": token == current_token})
    else:
        _prune_memory()
        for token, (_, record) in _user_sessions.items():
            if record.get("user_id") == user_id:
                out.append({**record, "current": token == current_token})
    for item in out:
        item.pop("user_id", None)
    return sorted(out, key=lambda r: r.get("created_at") or "", reverse=True)


async def delete_user_session(token: str | None) -> None:
    if not token:
        return
    redis = await get_redis()
    if redis is not None:
        record = _parse_session(await redis.get(f"user_session:{token}"))
        await redis.delete(f"user_session:{token}")
        if record and record.get("user_id"):
            await redis.srem(f"user_sessions_index:{record['user_id']}", token)
    else:
        _user_sessions.pop(token, None)


async def revoke_user_sessions(user_id: str, keep_token: str | None = None) -> int:
    """Sign out everywhere (optionally keeping the current browser). Returns how many."""
    revoked = 0
    redis = await get_redis()
    if redis is not None:
        tokens = [t.decode() if isinstance(t, bytes) else t for t in await redis.smembers(f"user_sessions_index:{user_id}")]
        for token in tokens:
            if token == keep_token:
                continue
            await redis.delete(f"user_session:{token}")
            await redis.srem(f"user_sessions_index:{user_id}", token)
            revoked += 1
    else:
        for token in [t for t, (_, r) in _user_sessions.items() if r.get("user_id") == user_id and t != keep_token]:
            _user_sessions.pop(token, None)
            revoked += 1
    return revoked


async def sign_in_with_token(token: str, ip: str | None = None, user_agent: str | None = None) -> tuple[dict, str, bool]:
    """Magic link -> (user, session token, created)."""
    email = await consume_magic_token(token)
    user, created = await get_or_create_user(email)
    session = await create_user_session(user["id"], ip, user_agent)
    return user, session, created


# ----------------------------------------------------------------------------
# Conversations that belong to a user (export / delete-all / delete account)
# ----------------------------------------------------------------------------

async def chat_session_owner(session_id: str) -> str | None:
    """The user a conversation belongs to, or None when nobody has claimed it
    (anonymous conversations are not mapped)."""
    pool = await get_pg_pool()
    if pool is not None:
        owner = await pool.fetchval("SELECT user_id FROM user_chat_sessions WHERE session_id = $1", session_id)
        return str(owner) if owner is not None else None   # UUID column; identities carry the id as text
    for user_id, sessions in _chat_sessions.items():
        if session_id in sessions:
            return user_id
    return None


async def touch_chat_session(user_id: str, session_id: str) -> bool:
    """Claim an unowned conversation for `user_id` or refresh one they own.
    Never transfers ownership: returns False (and writes nothing) when the
    conversation belongs to someone else."""
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow(
            """
            INSERT INTO user_chat_sessions (user_id, session_id, last_used) VALUES ($1, $2, NOW())
            ON CONFLICT (session_id) DO UPDATE SET last_used = NOW()
              WHERE user_chat_sessions.user_id = EXCLUDED.user_id
            RETURNING user_id
            """,
            user_id, session_id,
        )
        return row is not None
    owner = await chat_session_owner(session_id)
    if owner is not None and owner != user_id:
        return False
    _chat_sessions.setdefault(user_id, {})[session_id] = _now()
    if await chat_session_owner(session_id) != user_id:      # lost a concurrent claim
        _chat_sessions[user_id].pop(session_id, None)
        return False
    return True


async def list_chat_sessions(user_id: str) -> list[str]:
    pool = await get_pg_pool()
    if pool is not None:
        rows = await pool.fetch("SELECT session_id FROM user_chat_sessions WHERE user_id = $1 ORDER BY last_used DESC", user_id)
        return [row["session_id"] for row in rows]
    return sorted(_chat_sessions.get(user_id, {}), key=lambda sid: _chat_sessions[user_id][sid], reverse=True)


async def list_chat_sessions_with_times(user_id: str, limit: int = 200) -> list[dict]:
    """[{session_id, last_used}] newest first -- the account's own conversations."""
    pool = await get_pg_pool()
    if pool is not None:
        rows = await pool.fetch(
            "SELECT session_id, last_used FROM user_chat_sessions WHERE user_id = $1 ORDER BY last_used DESC LIMIT $2", user_id, limit,
        )
        return [{"session_id": r["session_id"], "last_used": r["last_used"].isoformat()} for r in rows]
    own = _chat_sessions.get(user_id, {})
    ordered = sorted(own.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [{"session_id": sid, "last_used": used} for sid, used in ordered]


async def forget_chat_sessions(user_id: str, session_ids: list[str] | None = None) -> None:
    pool = await get_pg_pool()
    if pool is not None:
        if session_ids is None:
            await pool.execute("DELETE FROM user_chat_sessions WHERE user_id = $1", user_id)
        else:
            await pool.execute("DELETE FROM user_chat_sessions WHERE user_id = $1 AND session_id = ANY($2::text[])", user_id, session_ids)
        return
    if session_ids is None:
        _chat_sessions.pop(user_id, None)
    else:
        for sid in session_ids:
            _chat_sessions.get(user_id, {}).pop(sid, None)


async def delete_user(user_id: str) -> None:
    """Remove the account row (wallets, keys and session mappings cascade in
    Postgres); the credit ledger is kept as a financial record."""
    pool = await get_pg_pool()
    if pool is not None:
        await pool.execute("DELETE FROM team_members WHERE owner_id = $1 OR user_id = $1", user_id)
        await pool.execute("DELETE FROM users WHERE id = $1", user_id)
        return
    user = _users.pop(user_id, None)
    if user:
        _users_by_email.pop(user["email"], None)
    for key in [k for k, entry in _wallets.items() if entry[0] == user_id]:
        _wallets.pop(key, None)
    _chat_sessions.pop(user_id, None)
    for key in [k for k, m in _team_members.items() if k[0] == user_id or m.get("user_id") == user_id]:
        _team_members.pop(key, None)


# ----------------------------------------------------------------------------
# Team members (plans with seats > 1): members share the owner's plan and credits
# ----------------------------------------------------------------------------

def _member_public(record: dict) -> dict:
    return {k: record.get(k) for k in ("email", "role", "status", "invited_at", "accepted_at", "user_id")}


async def list_team_members(owner_id: str) -> list[dict]:
    pool = await get_pg_pool()
    if pool is not None:
        rows = await pool.fetch("SELECT * FROM team_members WHERE owner_id = $1 ORDER BY invited_at", owner_id)
        return [_member_public({**dict(r), "user_id": str(r["user_id"]) if r["user_id"] else None,
                                "invited_at": r["invited_at"].isoformat(), "accepted_at": r["accepted_at"].isoformat() if r["accepted_at"] else None}) for r in rows]
    return [_member_public(m) for (owner, _), m in _team_members.items() if owner == owner_id]


_team_locks: dict[tuple[int, str], "asyncio.Lock"] = {}


@asynccontextmanager
async def team_seat_gate(owner_id: str):
    """Serialise seat allocation per team. Counting the members and inserting
    an invite are two operations; two concurrent invitations both read the
    same free seat and both succeeded past the purchased count."""
    pool = await get_pg_pool()
    if pool is not None:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"team:{owner_id}")
                yield conn
        return
    import asyncio
    lock = _team_locks.setdefault((id(asyncio.get_running_loop()), owner_id), asyncio.Lock())
    async with lock:
        yield None


class SeatsExhausted(ValueError):
    """Every purchased seat is taken."""


async def invite_team_member_within_seats(owner_id: str, email: str, seats: int, role: str = "member") -> dict:
    """Invite under the seat gate: the count and the insert cannot interleave
    with another invitation for the same team."""
    email = normalize_email(email)
    async with team_seat_gate(owner_id) as conn:
        if conn is not None:
            rows = await conn.fetch("SELECT email FROM team_members WHERE owner_id = $1", owner_id)
            taken = {r["email"] for r in rows}
        else:
            taken = {k[1] for k in _team_members if k[0] == owner_id}
        if email not in taken and len(taken) + 1 >= seats:
            raise SeatsExhausted(f"All {seats} seats are in use (owner + {seats - 1} members)")
        return await invite_team_member(owner_id, email, role, db=conn)


async def invite_team_member(owner_id: str, email: str, role: str = "member", db=None) -> dict:
    email = normalize_email(email)
    role = role if role in ("member", "owner") else "member"
    record = {"owner_id": owner_id, "email": email, "role": role, "status": "invited", "invited_at": _now(), "accepted_at": None, "user_id": None}
    pool = await get_pg_pool()
    if pool is not None:
        await (db or pool).execute(
            """
            INSERT INTO team_members (owner_id, email, role, status) VALUES ($1, $2, $3, 'invited')
            ON CONFLICT (owner_id, email) DO UPDATE SET role = EXCLUDED.role
            """,
            owner_id, email, role,
        )
    else:
        existing = _team_members.get((owner_id, email))
        if existing:
            existing["role"] = role
            return _member_public(existing)
        _team_members[(owner_id, email)] = record
    return _member_public(record)


async def remove_team_member(owner_id: str, email: str) -> bool:
    """Remove a member. The user's team pointer is cleared only if it still
    points at THIS owner -- an owner removing a stale record must not detach
    the user from the team they have since joined."""
    email = normalize_email(email)
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow("DELETE FROM team_members WHERE owner_id = $1 AND email = $2 RETURNING user_id", owner_id, email)
        if row is None:
            return False
        if row["user_id"]:
            await pool.execute("UPDATE users SET team_owner_id = NULL WHERE id = $1 AND team_owner_id = $2", str(row["user_id"]), owner_id)
        return True
    record = _team_members.pop((owner_id, email), None)
    if record is None:
        return False
    if record.get("user_id"):
        current = _users.get(record["user_id"])
        if current and current.get("team_owner_id") == owner_id:
            await update_user(record["user_id"], team_owner_id=None)
    return True


async def pending_invites_for(email: str | None) -> list[dict]:
    if not email:   # a wallet-only account has none, and therefore no email-addressed invites
        return []
    email = normalize_email(email)
    pool = await get_pg_pool()
    if pool is not None:
        rows = await pool.fetch(
            "SELECT m.owner_id, m.role, u.email AS owner_email, u.display_name AS owner_name FROM team_members m JOIN users u ON u.id = m.owner_id "
            "WHERE m.email = $1 AND m.status = 'invited'", email,
        )
        return [{"owner_id": str(r["owner_id"]), "owner_email": r["owner_email"], "owner_name": r["owner_name"], "role": r["role"]} for r in rows]
    out = []
    for (owner_id, member_email), record in _team_members.items():
        if member_email == email and record["status"] == "invited":
            owner = _users.get(owner_id) or {}
            out.append({"owner_id": owner_id, "owner_email": owner.get("email"), "owner_name": owner.get("display_name"), "role": record["role"]})
    return out


async def accept_team_invite(user: dict, owner_id: str) -> dict | None:
    """The signed-in user joins the owner's team (the invite must be for their
    email). One active membership per user: joining team B leaves team A in
    the same step, so A's owner cannot later "remove" a record that should not
    exist and clear the user's pointer to B with it."""
    email = user["email"]
    pool = await get_pg_pool()
    if pool is not None:
        # The user's row is locked first, so two simultaneous joins run one
        # after the other: the second sees the first's membership and removes
        # it before activating its own. The invitation is established, and
        # locked, BEFORE anything is removed: a join with no valid invite used
        # to delete the user's current membership and then return None from
        # inside the transaction, committing that deletion while the user's
        # team pointer still named the team they had just vanished from. A
        # rejected join now writes nothing. The partial unique index on active
        # memberships is the backstop should anything bypass this path.
        async with user_row_lock(user["id"]) as (conn, _current):
            invite = await conn.fetchrow(
                "SELECT role FROM team_members WHERE owner_id = $1 AND email = $2 AND status = 'invited' FOR UPDATE",
                owner_id, email,
            )
            if invite is None:
                return None
            await conn.execute("DELETE FROM team_members WHERE user_id = $1 AND owner_id <> $2", user["id"], owner_id)
            await conn.execute(
                "UPDATE team_members SET status = 'active', accepted_at = NOW(), user_id = $3 WHERE owner_id = $1 AND email = $2 AND status = 'invited'",
                owner_id, email, user["id"],
            )
            await conn.execute("UPDATE users SET team_owner_id = $2 WHERE id = $1", user["id"], owner_id)
        return await get_user(user["id"])
    record = _team_members.get((owner_id, email))
    if record is None or record["status"] != "invited":
        return None
    for key in [k for k, m in _team_members.items() if m.get("user_id") == user["id"] and k[0] != owner_id]:
        _team_members.pop(key, None)
    record.update({"status": "active", "accepted_at": _now(), "user_id": user["id"]})
    return await update_user(user["id"], team_owner_id=owner_id)


async def leave_team(user: dict) -> dict | None:
    owner_id = user.get("team_owner_id")
    if owner_id:
        await remove_team_member(owner_id, user["email"])
    return await update_user(user["id"], team_owner_id=None)


def reset() -> None:
    """Test hook: clear the in-memory stores."""
    _users.clear()
    _users_by_email.clear()
    _wallets.clear()
    _magic_tokens.clear()
    _user_sessions.clear()
    _chat_sessions.clear()
    _team_members.clear()
