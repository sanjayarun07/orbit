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

import hashlib
import logging
import time
from datetime import datetime, timezone
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
            str(uuid4()), account_id, int(delta), reason, ref_type, ref_id, __import__("json").dumps(meta or {}),
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


async def reserve(account_id: str, turn_id: str, amount: int | None = None) -> int:
    """Hold the maximum a turn can cost. Raises InsufficientCredits when the
    balance can't cover even the cheapest turn (so a user with 1 credit left can
    still ask a plain question)."""
    amount = amount if amount is not None else max_turn_cost()
    current = await balance(account_id)
    minimum = settings.credit_cost_chat_turn
    if current < minimum:
        raise InsufficientCredits(current, minimum)
    amount = min(amount, current)
    await append(account_id, -amount, "reserve", "turn", turn_id)
    return amount


async def settle(account_id: str, turn_id: str, reserved: int, actual: int, kind: str) -> int:
    """Refund the unused part of a reservation. Returns the charge that stuck."""
    actual = max(0, min(actual, reserved))
    refund = reserved - actual
    if refund > 0:
        await append(account_id, refund, "settle_refund", "turn_refund", turn_id, {"kind": kind, "charged": actual})
    return actual


async def release(account_id: str, turn_id: str, reserved: int) -> None:
    """The turn failed: give the whole reservation back."""
    if reserved > 0:
        await append(account_id, reserved, "release", "turn_refund", turn_id, {"charged": 0})


def reset() -> None:
    _ledger.clear()
    _refs.clear()
