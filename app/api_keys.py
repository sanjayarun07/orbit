"""Per-user API keys for the chat API and the MCP server.

A key is shown once at creation (`orb_live_<random>`); only its SHA-256 lands
in storage. Scopes bound what a key may do: `chat` (POST /chat), `data`
(read-only data endpoints) and `mcp` (the MCP server). A key never unlocks
execution -- trade confirmation and signing stay wallet-side exactly as for
the web UI.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone
from uuid import uuid4

from app.db import get_pg_pool

KEY_PREFIX = "orb_live_"
SCOPES = ("chat", "data", "mcp")
MAX_KEYS_PER_USER = 10

_keys: dict[str, dict] = {}  # id -> record (memory fallback)
_by_hash: dict[str, str] = {}
_last_touch: dict[str, float] = {}


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _public(record: dict) -> dict:
    return {
        "id": record["id"],
        "name": record["name"],
        "prefix": record["prefix"],
        "scopes": list(record["scopes"]),
        "created_at": record["created_at"],
        "last_used_at": record.get("last_used_at"),
        "revoked_at": record.get("revoked_at"),
    }


def _row(row) -> dict:
    return {
        "id": str(row["id"]), "user_id": str(row["user_id"]), "name": row["name"], "prefix": row["prefix"],
        "key_hash": row["key_hash"], "scopes": list(row["scopes"] or []),
        "created_at": row["created_at"].isoformat(),
        "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
        "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
    }


async def create(user_id: str, name: str, scopes: list[str] | None = None) -> tuple[dict, str]:
    """(public record, full secret). The secret is never retrievable again."""
    name = (name or "").strip()[:60] or "API key"
    # A scope list is a restriction the caller asked for. Filtering unknown
    # names out and then falling back to every scope when nothing was left
    # turned a typo -- or a test's guess at a scope name -- into a full-power
    # key. Unknown scopes are refused; an omitted list still means all.
    requested = list(scopes) if scopes is not None else list(SCOPES)
    unknown = [scope for scope in requested if scope not in SCOPES]
    if unknown:
        raise ValueError(f"Unknown scope(s) {', '.join(sorted(set(unknown)))}; choose from {', '.join(SCOPES)}")
    if not requested:
        raise ValueError(f"Choose at least one scope from {', '.join(SCOPES)}")
    chosen = tuple(scope for scope in SCOPES if scope in requested)
    active = [key for key in await list_for_user(user_id) if not key["revoked_at"]]
    if len(active) >= MAX_KEYS_PER_USER:
        raise ValueError(f"You already have {MAX_KEYS_PER_USER} active keys; revoke one first")
    secret = KEY_PREFIX + secrets.token_urlsafe(30)
    record = {
        "id": str(uuid4()), "user_id": user_id, "name": name, "prefix": secret[: len(KEY_PREFIX) + 6],
        "key_hash": _hash(secret), "scopes": list(chosen),
        "created_at": datetime.now(timezone.utc).isoformat(), "last_used_at": None, "revoked_at": None,
    }
    pool = await get_pg_pool()
    if pool is not None:
        await pool.execute(
            "INSERT INTO api_keys (id, user_id, name, prefix, key_hash, scopes) VALUES ($1, $2, $3, $4, $5, $6)",
            record["id"], user_id, name, record["prefix"], record["key_hash"], record["scopes"],
        )
    else:
        _keys[record["id"]] = record
        _by_hash[record["key_hash"]] = record["id"]
    return _public(record), secret


async def list_for_user(user_id: str) -> list[dict]:
    pool = await get_pg_pool()
    if pool is not None:
        rows = await pool.fetch("SELECT * FROM api_keys WHERE user_id = $1 ORDER BY created_at", user_id)
        return [_public(_row(row)) for row in rows]
    return [_public(record) for record in _keys.values() if record["user_id"] == user_id]


async def revoke(user_id: str, key_id: str) -> bool:
    pool = await get_pg_pool()
    if pool is not None:
        result = await pool.execute(
            "UPDATE api_keys SET revoked_at = NOW() WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL", key_id, user_id
        )
        return result.endswith("1")
    record = _keys.get(key_id)
    if record is None or record["user_id"] != user_id or record["revoked_at"]:
        return False
    record["revoked_at"] = datetime.now(timezone.utc).isoformat()
    return True


async def authenticate(secret: str | None) -> dict | None:
    """The active key record for a presented secret, or None. Constant-time on
    the hash compare; `last_used_at` is touched at most once a minute."""
    if not secret or not secret.startswith(KEY_PREFIX) or len(secret) > 120:
        return None
    digest = _hash(secret)
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow("SELECT * FROM api_keys WHERE key_hash = $1 AND revoked_at IS NULL", digest)
        record = _row(row) if row else None
    else:
        key_id = _by_hash.get(digest)
        record = _keys.get(key_id) if key_id else None
        if record and record["revoked_at"]:
            record = None
    if record is None or not hmac.compare_digest(record["key_hash"], digest):
        return None
    now = time.time()
    if now - _last_touch.get(record["id"], 0) > 60:
        _last_touch[record["id"]] = now
        record["last_used_at"] = datetime.now(timezone.utc).isoformat()
        if pool is not None:
            await pool.execute("UPDATE api_keys SET last_used_at = NOW() WHERE id = $1", record["id"])
    return record


def reset() -> None:
    _keys.clear()
    _by_hash.clear()
    _last_touch.clear()
