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
_user_sessions: dict[str, tuple[float, str]] = {}


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
    allowed = {"display_name", "plan_id", "stripe_customer_id", "stripe_subscription_id", "subscription_status", "preferences"}
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

async def create_user_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    redis = await get_redis()
    if redis is not None:
        await redis.setex(f"user_session:{token}", USER_SESSION_TTL, user_id)
    else:
        _prune_memory()
        _user_sessions[token] = (time.time() + USER_SESSION_TTL, user_id)
    return token


async def get_session_user(token: str | None) -> dict | None:
    if not token:
        return None
    redis = await get_redis()
    if redis is not None:
        user_id = await redis.get(f"user_session:{token}")
        if isinstance(user_id, bytes):
            user_id = user_id.decode()
    else:
        _prune_memory()
        entry = _user_sessions.get(token)
        user_id = entry[1] if entry else None
    return await get_user(user_id) if user_id else None


async def delete_user_session(token: str | None) -> None:
    if not token:
        return
    redis = await get_redis()
    if redis is not None:
        await redis.delete(f"user_session:{token}")
    else:
        _user_sessions.pop(token, None)


async def sign_in_with_token(token: str) -> tuple[dict, str, bool]:
    """Magic link -> (user, session token, created)."""
    email = await consume_magic_token(token)
    user, created = await get_or_create_user(email)
    session = await create_user_session(user["id"])
    return user, session, created


def reset() -> None:
    """Test hook: clear the in-memory stores."""
    _users.clear()
    _users_by_email.clear()
    _wallets.clear()
    _magic_tokens.clear()
    _user_sessions.clear()
