"""Append-only credit ledger.

Every change to a balance is a row: grants (trial, monthly plan allowance,
purchases), reservations for a chat turn, and the refund of whatever the turn
did not use. The balance is the sum. Rows carry (account_id, ref_type, ref_id)
and that triple is unique, which is what makes grants idempotent -- a Stripe
webhook redelivered with the same event id, or a monthly allowance applied
twice, inserts nothing the second time.

Accounts are either a signed-in user (`user:<id>`) or an anonymous visitor
(`anon:<hash of ip/device>`) on the one-time trial.
"""

from __future__ import annotations

import asyncio

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.billing_plans import Plan, get_plan
from app.db import get_pg_pool
from app.settings import settings

logger = logging.getLogger(__name__)

_ledger: dict[str, list[dict]] = {}
_refs: set[tuple[str, str, str]] = set()


class InsufficientCredits(Exception):
    def __init__(self, balance: int, required: int):
        super().__init__(f"Insufficient credits: balance {balance}, required {required}")
        self.balance = balance
        self.required = required


def anonymous_account_id(ip: str, device_id: str | None) -> str:
    """Trial accounts are keyed on the device id the UI stores, falling back to
    the IP -- both are limits, not identities, which is why the trial is small."""
    key = (device_id or "").strip()[:128] or f"ip:{ip}"
    return "anon:" + hashlib.sha256(key.encode()).hexdigest()[:32]


def user_account_id(user_id: str) -> str:
    return f"user:{user_id}"


def month_key(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


# ----------------------------------------------------------------------------
# Core ledger operations
# ----------------------------------------------------------------------------

async def append(account_id: str, delta: int, reason: str, ref_type: str, ref_id: str, meta: dict | None = None) -> bool:
    """Insert one row. Returns False when (account, ref_type, ref_id) already
    exists -- the idempotency guarantee every caller relies on."""
    pool = await get_pg_pool()
    if pool is not None:
        row = await pool.fetchrow(
            """
            INSERT INTO credit_ledger (id, account_id, delta, reason, ref_type, ref_id, meta)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (account_id, ref_type, ref_id) DO NOTHING
            RETURNING id
            """,
            str(uuid4()), account_id, int(delta), reason, ref_type, ref_id, json.dumps(meta or {}),
        )
        return row is not None
    key = (account_id, ref_type, ref_id)
    if key in _refs:
        return False
    _refs.add(key)
    _ledger.setdefault(account_id, []).append({
        "id": str(uuid4()), "account_id": account_id, "delta": int(delta), "reason": reason,
        "ref_type": ref_type, "ref_id": ref_id, "meta": meta or {}, "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return True


async def balance(account_id: str) -> int:
    pool = await get_pg_pool()
    if pool is not None:
        value = await pool.fetchval("SELECT COALESCE(SUM(delta), 0) FROM credit_ledger WHERE account_id = $1", account_id)
        return int(value or 0)
    return sum(row["delta"] for row in _ledger.get(account_id, []))


async def clawed_back(account_id: str, charge_id: str) -> int:
    """Credits already deducted for refunds of one Stripe charge (a positive
    number), so successive partial refunds deduct only their increment."""
    pool = await get_pg_pool()
    if pool is not None:
        total = await pool.fetchval(
            "SELECT COALESCE(SUM(-delta), 0) FROM credit_ledger WHERE account_id = $1 AND reason LIKE 'refund:%' AND meta->>'charge' = $2",
            account_id, charge_id,
        )
        return int(total or 0)
    return sum(-row["delta"] for row in _ledger.get(account_id, [])
               if row["reason"].startswith("refund:") and str((row.get("meta") or {}).get("charge")) == charge_id)


_clawback_locks: dict[tuple[int, str], asyncio.Lock] = {}   # keyed per event loop, like _reserve_locks


async def claw_back(account_id: str, charge_id: str, target: int, reason: str, ref_type: str, ref_id: str, meta: dict | None = None) -> tuple[int, int]:
    """Bring the credits clawed back for one charge up to `target` (a
    cumulative figure, like Stripe's amount_refunded) and return
    (deducted_now, total_clawed_back). Reading the running total and appending
    the increment happen under a per-charge lock -- a Postgres advisory lock
    inside one transaction, or an asyncio lock in memory -- so two overlapping
    refund events cannot both read the old total and over-deduct."""
    pool = await get_pg_pool()
    if pool is not None:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"clawback:{charge_id}")
                already = int(await conn.fetchval(
                    "SELECT COALESCE(SUM(-delta), 0) FROM credit_ledger WHERE account_id = $1 AND reason LIKE 'refund:%' AND meta->>'charge' = $2",
                    account_id, charge_id,
                ) or 0)
                increment = target - already
                if increment <= 0:
                    return 0, already
                row = await conn.fetchrow(
                    """
                    INSERT INTO credit_ledger (id, account_id, delta, reason, ref_type, ref_id, meta)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (account_id, ref_type, ref_id) DO NOTHING
                    RETURNING id
                    """,
                    str(uuid4()), account_id, -increment, reason, ref_type, ref_id, json.dumps(meta or {}),
                )
                return (increment, target) if row is not None else (0, already)
    lock = _clawback_locks.setdefault((id(asyncio.get_running_loop()), charge_id), asyncio.Lock())
    async with lock:
        already = await clawed_back(account_id, charge_id)
        increment = target - already
        if increment <= 0:
            return 0, already
        applied = await append(account_id, -increment, reason, ref_type, ref_id, meta)
        return (increment, target) if applied else (0, already)


async def has_ref(account_id: str, ref_type: str, ref_id: str) -> bool:
    """Whether a ledger row with this idempotency reference already exists
    (e.g. an occurrence charged by a run that died before delivering)."""
    pool = await get_pg_pool()
    if pool is not None:
        return await pool.fetchval("SELECT 1 FROM credit_ledger WHERE account_id = $1 AND ref_type = $2 AND ref_id = $3", account_id, ref_type, ref_id) is not None
    return (account_id, ref_type, ref_id) in _refs


async def history(account_id: str, limit: int = 50) -> list[dict]:
    pool = await get_pg_pool()
    if pool is not None:
        rows = await pool.fetch(
            "SELECT delta, reason, ref_type, ref_id, created_at FROM credit_ledger WHERE account_id = $1 ORDER BY created_at DESC LIMIT $2",
            account_id, limit,
        )
        return [{"delta": r["delta"], "reason": r["reason"], "ref_type": r["ref_type"], "ref_id": r["ref_id"],
                 "created_at": r["created_at"].isoformat()} for r in rows]
    rows = list(reversed(_ledger.get(account_id, [])))[:limit]
    return [{k: row[k] for k in ("delta", "reason", "ref_type", "ref_id", "created_at")} for row in rows]


# ----------------------------------------------------------------------------
# Grants
# ----------------------------------------------------------------------------

async def ensure_trial_grant(account_id: str, plan: Plan) -> None:
    if plan.trial_credits > 0:
        await append(account_id, plan.trial_credits, "trial_grant", "grant", "trial")


async def ensure_monthly_grant(account_id: str, plan: Plan, now: datetime | None = None) -> None:
    """Free-tier allowance, applied lazily once per calendar month. Paid plans
    get theirs from the invoice webhook (Phase 2) but this also covers a paid
    plan set by an admin without Stripe."""
    if plan.monthly_credits > 0:
        await append(account_id, plan.monthly_credits, "monthly_grant", "grant", f"month:{plan.id}:{month_key(now)}")


async def grant(account_id: str, amount: int, reason: str, ref_type: str, ref_id: str, meta: dict | None = None) -> bool:
    if amount <= 0:
        raise ValueError("Grant amount must be positive")
    return await append(account_id, amount, reason, ref_type, ref_id, meta)


# ----------------------------------------------------------------------------
# Charging a chat turn: reserve up front, settle what was used, refund the rest
# ----------------------------------------------------------------------------

def turn_cost(intent: str | None, trajectory: dict | None, team_report: dict | None) -> tuple[int, str]:
    """Credits for a finished turn, from what it actually did."""
    tools = _tool_names(trajectory or {})
    if team_report:
        return settings.credit_cost_team_turn, "team_desk"
    if "token_deep_dive" in tools:
        return settings.credit_cost_deep_dive, "deep_dive"
    if intent in {"trade", "cross_chain_swap"}:
        return settings.credit_cost_trade_turn, "trade"
    if tools - {"semantic_cache", "finish"}:
        return settings.credit_cost_tool_turn, "tool_turn"
    return settings.credit_cost_chat_turn, "chat"


def _tool_names(trajectory: dict) -> set[str]:
    names: set[str] = set()
    for key, value in trajectory.items():
        if key.startswith("tool_name") and isinstance(value, str):
            names.add(value)
        elif isinstance(value, dict):
            names |= _tool_names(value)
    return names


def max_turn_cost() -> int:
    return max(
        settings.credit_cost_chat_turn, settings.credit_cost_tool_turn, settings.credit_cost_deep_dive,
        settings.credit_cost_trade_turn, settings.credit_cost_team_turn,
    )


_reserve_locks: dict[tuple[int, str], asyncio.Lock] = {}   # keyed per event loop: a Lock must not cross loops


async def reserve(account_id: str, turn_id: str, amount: int | None = None) -> int:
    """Hold the maximum a turn can cost. Raises InsufficientCredits when the
    balance can't cover even the cheapest turn (so a user with 1 credit left can
    still ask a plain question).

    Check-and-hold is atomic per account: in Postgres the balance is read and
    the hold inserted inside one transaction under an account-level advisory
    lock, so two concurrent turns cannot both reserve the last credits; the
    in-memory store serialises on a per-account lock."""
    amount = amount if amount is not None else max_turn_cost()
    minimum = settings.credit_cost_chat_turn
    pool = await get_pg_pool()
    if pool is not None:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", account_id)
                current = int(await conn.fetchval("SELECT COALESCE(SUM(delta), 0) FROM credit_ledger WHERE account_id = $1", account_id) or 0)
                if current < minimum:
                    raise InsufficientCredits(current, minimum)
                amount = min(amount, current)
                await conn.execute(
                    "INSERT INTO credit_ledger (id, account_id, delta, reason, ref_type, ref_id, meta) VALUES ($1, $2, $3, 'reserve', 'turn', $4, '{}') ON CONFLICT DO NOTHING",
                    str(uuid4()), account_id, -amount, turn_id,
                )
        return amount
    lock = _reserve_locks.setdefault((id(asyncio.get_running_loop()), account_id), asyncio.Lock())
    async with lock:
        current = await balance(account_id)
        if current < minimum:
            raise InsufficientCredits(current, minimum)
        amount = min(amount, current)
        await append(account_id, -amount, "reserve", "turn", turn_id)
        return amount


async def settle(account_id: str, turn_id: str, reserved: int, actual: int, kind: str, api_key_id: str | None = None) -> int:
    """Refund the unused part of a reservation and record what the turn cost
    (one `turn_settle` row per turn, delta = refund, possibly 0 -- the usage
    report is built from these). Returns the charge that stuck."""
    actual = max(0, min(actual, reserved))
    refund = reserved - actual
    meta = {"kind": kind, "charged": actual}
    if api_key_id:
        meta["api_key_id"] = api_key_id
    await append(account_id, refund, "settle", "turn_settle", turn_id, meta)
    return actual


async def usage(account_id: str, days: int = 30) -> dict:
    """Credits spent per day and per feature over the last `days` days, from
    the settle rows -- plus a per-API-key split for the keys tab."""
    days = max(1, min(int(days), 365))
    since = datetime.now(timezone.utc) - timedelta(days=days - 1)
    since = since.replace(hour=0, minute=0, second=0, microsecond=0)
    rows: list[tuple[str, dict]] = []
    pool = await get_pg_pool()
    if pool is not None:
        fetched = await pool.fetch(
            "SELECT created_at, meta FROM credit_ledger WHERE account_id = $1 AND ref_type = 'turn_settle' AND created_at >= $2",
            account_id, since,
        )
        for row in fetched:
            meta = row["meta"]
            if isinstance(meta, str):
                meta = json.loads(meta)
            rows.append((row["created_at"].isoformat(), meta or {}))
    else:
        for row in _ledger.get(account_id, []):
            if row["ref_type"] == "turn_settle" and row["created_at"] >= since.isoformat():
                rows.append((row["created_at"], row["meta"] or {}))
    by_day: dict[str, dict] = {}
    for offset in range(days):
        day = (since + timedelta(days=offset)).date().isoformat()
        by_day[day] = {"date": day, "charged": 0, "turns": 0, "by_kind": {}}
    by_kind: dict[str, int] = {}
    by_key: dict[str, int] = {}
    total = 0
    for created_at, meta in rows:
        day = created_at[:10]
        charged = int(meta.get("charged") or 0)
        kind = str(meta.get("kind") or "chat")
        bucket = by_day.setdefault(day, {"date": day, "charged": 0, "turns": 0, "by_kind": {}})
        bucket["charged"] += charged
        bucket["turns"] += 1
        bucket["by_kind"][kind] = bucket["by_kind"].get(kind, 0) + charged
        by_kind[kind] = by_kind.get(kind, 0) + charged
        if meta.get("api_key_id"):
            by_key[meta["api_key_id"]] = by_key.get(meta["api_key_id"], 0) + charged
        total += charged
    return {"days": days, "since": since.date().isoformat(), "total": total, "turns": len(rows),
            "by_day": list(by_day.values()), "by_kind": by_kind, "by_api_key": by_key}


async def release(account_id: str, turn_id: str, reserved: int) -> None:
    """The turn failed: give the whole reservation back."""
    if reserved > 0:
        await append(account_id, reserved, "release", "turn_refund", turn_id, {"charged": 0})


async def usage_all(days: int = 30) -> dict:
    """Credits burned per feature across every account (ops dashboard), plus
    credits granted by reason in the same window."""
    days = max(1, min(int(days), 365))
    since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    burned: dict[str, int] = {}
    granted: dict[str, int] = {}
    turns = 0
    active_accounts: set[str] = set()
    pool = await get_pg_pool()
    if pool is not None:
        for row in await pool.fetch(
            "SELECT account_id, ref_type, reason, delta, meta FROM credit_ledger WHERE created_at >= $1 AND (ref_type = 'turn_settle' OR delta > 0)", since,
        ):
            meta = row["meta"]
            if isinstance(meta, str):
                meta = json.loads(meta)
            _fold_usage(row["account_id"], row["ref_type"], row["reason"], int(row["delta"]), meta or {}, burned, granted, active_accounts)
            turns += int(row["ref_type"] == "turn_settle")
    else:
        for account_id, rows in _ledger.items():
            for row in rows:
                if row["created_at"] < since.isoformat():
                    continue
                if row["ref_type"] == "turn_settle" or row["delta"] > 0:
                    _fold_usage(account_id, row["ref_type"], row["reason"], row["delta"], row["meta"] or {}, burned, granted, active_accounts)
                    turns += int(row["ref_type"] == "turn_settle")
    return {"days": days, "since": since.date().isoformat(), "turns": turns, "burned_by_kind": burned,
            "burned_total": sum(burned.values()), "granted_by_reason": granted, "active_accounts": len(active_accounts)}


def _fold_usage(account_id: str, ref_type: str, reason: str, delta: int, meta: dict, burned: dict, granted: dict, active: set) -> None:
    if ref_type == "turn_settle":
        kind = str(meta.get("kind") or "chat")
        burned[kind] = burned.get(kind, 0) + int(meta.get("charged") or 0)
        active.add(account_id)
    elif delta > 0 and ref_type not in ("turn_refund", "turn_settle"):
        key = reason.split(":", 1)[0]
        granted[key] = granted.get(key, 0) + delta


def reset() -> None:
    _ledger.clear()
    _refs.clear()
