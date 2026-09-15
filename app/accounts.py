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
_wallets: dict[tuple[str, str], tuple[str, str]] = {}  # (chain, address_lower) -> (user_id, address)
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
        "team_owner_id": str(row["team_owner_id"]) if (row.get("team_owner_id") if hasattr(row, "get") else row["team_owner_id"]) else None,
        "preferences": json.loads(row["preferences"]) if isinstance(row["preferences"], str) else (row["preferences"] or {}),
        "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else row["created_at"],
    }


# ----------------------------------------------------------------------------
# Users
# ----------------------------------------------------------------------------

async def get_user(user_id: str) -> dict | None:
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
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
    users = [dict(u) for u in _users.values() if not query or query in u["email"]]
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


async def update_user(user_id: str, **fields) -> dict | None:
    """Update a whitelisted set of columns; preferences are merged, not replaced."""
    allowed = {"display_name", "plan_id", "stripe_customer_id", "stripe_subscription_id", "subscription_status", "preferences", "team_owner_id"}
    changes = {key: value for key, value in fields.items() if key in allowed}
    if not changes:
        return await get_user(user_id)
    pool = await get_pg_pool()
    if pool is not None:
        current = await get_user(user_id)
        if current is None:
            return None
        if "preferences" in changes:
            changes["preferences"] = json.dumps({**current.get("preferences", {}), **(changes["preferences"] or {})})
        assignments = ", ".join(f"{key} = ${index}" for index, key in enumerate(changes, start=2))
        row = await pool.fetchrow(
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

async def link_wallet(user_id: str, chain: str, address: str) -> None:
    pool = await get_pg_pool()
    if pool is not None:
        await pool.execute(
            """
            INSERT INTO user_wallets (user_id, chain, address) VALUES ($1, $2, $3)
            ON CONFLICT (chain, address) DO UPDATE SET user_id = EXCLUDED.user_id, linked_at = NOW()
            """,
            user_id, chain, address,
        )
        return
    _wallets[(chain, address.lower())] = (user_id, address)


async def list_wallets(user_id: str) -> list[dict]:
    pool = await get_pg_pool()
    if pool is not None:
        rows = await pool.fetch("SELECT chain, address, linked_at FROM user_wallets WHERE user_id = $1 ORDER BY linked_at", user_id)
        return [{"chain": r["chain"], "address": r["address"], "linked_at": r["linked_at"].isoformat()} for r in rows]
    return [{"chain": chain, "address": address, "linked_at": None} for (chain, _), (owner, address) in _wallets.items() if owner == user_id]


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

async def touch_chat_session(user_id: str, session_id: str) -> None:
    pool = await get_pg_pool()
    if pool is not None:
        await pool.execute(
            """
            INSERT INTO user_chat_sessions (user_id, session_id, last_used) VALUES ($1, $2, NOW())
            ON CONFLICT (session_id) DO UPDATE SET user_id = EXCLUDED.user_id, last_used = NOW()
            """,
            user_id, session_id,
        )
        return
    _chat_sessions.setdefault(user_id, {})[session_id] = _now()


async def list_chat_sessions(user_id: str) -> list[str]:
    pool = await get_pg_pool()
    if pool is not None:
        rows = await pool.fetch("SELECT session_id FROM user_chat_sessions WHERE user_id = $1 ORDER BY last_used DESC", user_id)
        return [row["session_id"] for row in rows]
    return sorted(_chat_sessions.get(user_id, {}), key=lambda sid: _chat_sessions[user_id][sid], reverse=True)


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
    for key in [k for k, (owner, _) in _wallets.items() if owner == user_id]:
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


async def invite_team_member(owner_id: str, email: str, role: str = "member") -> dict:
    email = normalize_email(email)
    role = role if role in ("member", "owner") else "member"
    record = {"owner_id": owner_id, "email": email, "role": role, "status": "invited", "invited_at": _now(), "accepted_at": None, "user_id": None}
    pool = await get_pg_pool()
    if pool is not None:
        await pool.execute(
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
    email = normalize_email(email)
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow("DELETE FROM team_members WHERE owner_id = $1 AND email = $2 RETURNING user_id", owner_id, email)
        if row is None:
            return False
        if row["user_id"]:
            await update_user(str(row["user_id"]), team_owner_id=None)
        return True
    record = _team_members.pop((owner_id, email), None)
    if record is None:
        return False
    if record.get("user_id"):
        await update_user(record["user_id"], team_owner_id=None)
    return True


async def pending_invites_for(email: str) -> list[dict]:
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
    """The signed-in user joins the owner's team (the invite must be for their email)."""
    email = user["email"]
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow(
            "UPDATE team_members SET status = 'active', accepted_at = NOW(), user_id = $3 WHERE owner_id = $1 AND email = $2 AND status = 'invited' RETURNING role",
            owner_id, email, user["id"],
        )
        if row is None:
            return None
    else:
        record = _team_members.get((owner_id, email))
        if record is None or record["status"] != "invited":
            return None
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
