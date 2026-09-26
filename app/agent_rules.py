"""Armed rules: the agent prepares, the user signs.

Phase 0 of docs/agentic-wallets-plan.md. An exit watch can carry an arm
rule: when its deterioration alert fires, the sized exit is quoted at that
moment, checked against the built-in caps and the user's risk charter, and a
receipt is written BEFORE anything is shown. The user is then handed the
exact sell line to send; that line goes through the ordinary trade path (a
fresh quote, the charter, the CONFIRM card, the wallet's own signature).
Nothing here holds a key, prepares a transaction or signs.

The receipt is the record the morning brief, the Tasks screen and the
reflection loop read: what fired, what was quoted, what the checks said,
whether a plan followed and how it ended.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

from app.db import apply_schema, get_pg_pool, memory_is_the_store
from app.settings import settings

logger = logging.getLogger(__name__)

KIND_EXIT = "exit_rule"
DEFAULT_MAX_PER_DAY = 3
DEFAULT_SLIPPAGE_BPS = 50
COUNTED = ("armed", "paper", "prepared")                      # what the daily cap counts

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agent_receipts (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL,
    signer TEXT NOT NULL DEFAULT 'user',
    reason TEXT,
    trigger JSONB,
    quote JSONB,
    proposal JSONB,
    checks JSONB,
    plan_id TEXT,
    outcome JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS agent_receipts_user_idx ON agent_receipts (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS agent_receipts_source_idx ON agent_receipts (source_id, created_at DESC);
"""
_ready = False
_receipts: dict[str, dict] = {}
_lock = threading.Lock()
_JSON = ("trigger", "quote", "proposal", "checks", "outcome")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def reset_for_test() -> None:
    global _ready
    with _lock:
        _receipts.clear()
    _ready = False


async def _pool():
    global _ready
    try:
        pool = await get_pg_pool()
    except Exception:
        if memory_is_the_store():
            return None
        raise
    if pool is not None and not _ready:
        await apply_schema(pool, _TABLE_SQL)
        _ready = True
    return pool


def _from_db(r) -> dict:
    row = dict(r)
    row["id"] = str(row["id"]).replace("-", "")
    row["user_id"] = str(row["user_id"]) if row.get("user_id") else None
    for key in _JSON:
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    for key in ("created_at", "resolved_at"):
        if isinstance(row.get(key), datetime):
            row[key] = row[key].isoformat()
    return row


async def _insert(receipt: dict) -> None:
    pool = await _pool()
    if pool is not None:
        await pool.execute(
            "INSERT INTO agent_receipts (id, user_id, kind, source_id, status, signer, reason, trigger, quote, proposal, checks, plan_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10::jsonb, $11::jsonb, $12)",
            uuid.UUID(receipt["id"]), receipt.get("user_id"), receipt["kind"], receipt["source_id"], receipt["status"], receipt["signer"],
            receipt.get("reason"), json.dumps(receipt.get("trigger")), json.dumps(receipt.get("quote")), json.dumps(receipt.get("proposal")),
            json.dumps(receipt.get("checks")), receipt.get("plan_id"))
        return
    with _lock:
        _receipts[receipt["id"]] = json.loads(json.dumps(receipt))


async def get(receipt_id: str) -> dict | None:
    pool = await _pool()
    if pool is not None:
        r = await pool.fetchrow("SELECT * FROM agent_receipts WHERE id = $1", uuid.UUID(receipt_id))
        return _from_db(r) if r else None
    with _lock:
        row = _receipts.get(receipt_id)
        return json.loads(json.dumps(row)) if row else None


async def list_for(user_id: str, limit: int = 50) -> list[dict]:
    pool = await _pool()
    if pool is not None:
        return [_from_db(r) for r in await pool.fetch("SELECT * FROM agent_receipts WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2", user_id, limit)]
    with _lock:
        rows = [json.loads(json.dumps(r)) for r in _receipts.values() if r.get("user_id") == user_id]
    return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]


async def count_today(source_id: str) -> int:
    """Receipts the daily cap counts for one rule source since midnight UTC."""
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    pool = await _pool()
    if pool is not None:
        return int(await pool.fetchval("SELECT COUNT(*) FROM agent_receipts WHERE source_id = $1 AND created_at >= $2 AND status = ANY($3::text[])",
                                       source_id, start, list(COUNTED)) or 0)
    with _lock:
        return sum(1 for r in _receipts.values() if r.get("source_id") == source_id and r.get("status") in COUNTED
                   and datetime.fromisoformat(r["created_at"]) >= start)


async def find_by_trigger(source_id: str, at: str | None) -> dict | None:
    """The receipt already written for one firing of a rule (its trigger time)."""
    if not at:
        return None
    pool = await _pool()
    if pool is not None:
        r = await pool.fetchrow("SELECT * FROM agent_receipts WHERE source_id = $1 AND trigger->>'at' = $2 ORDER BY created_at DESC LIMIT 1", source_id, at)
        return _from_db(r) if r else None
    with _lock:
        rows = [r for r in _receipts.values() if r.get("source_id") == source_id and (r.get("trigger") or {}).get("at") == at]
    return json.loads(json.dumps(rows[-1])) if rows else None


async def set_outcome(receipt_id: str, status: str, plan_id: str | None = None, outcome: dict | None = None) -> None:
    pool = await _pool()
    if pool is not None:
        await pool.execute("UPDATE agent_receipts SET status = $1, plan_id = COALESCE($2, plan_id), outcome = $3::jsonb, resolved_at = NOW() WHERE id = $4",
                           status, plan_id, json.dumps(outcome), uuid.UUID(receipt_id))
        return
    with _lock:
        row = _receipts.get(receipt_id)
        if row is not None:
            row.update(status=status, plan_id=plan_id or row.get("plan_id"), outcome=outcome, resolved_at=_now().isoformat())


async def scrub_user(user_id: str) -> int:
    pool = await _pool()
    if pool is not None:
        status = await pool.execute("DELETE FROM agent_receipts WHERE user_id = $1", user_id)
        return int(status.split()[-1]) if status else 0
    with _lock:
        gone = [k for k, r in _receipts.items() if r.get("user_id") == user_id]
        for k in gone:
            del _receipts[k]
    return len(gone)


async def _latest_armed(user_id: str, mint: str, amount_raw: int) -> dict | None:
    """The newest armed receipt this plan matches by content: same user, same
    mint, the same size the receipt proposed, within a day. Content, not an
    id threaded through the UI: the line works pasted from email or Telegram too."""
    since = _now() - timedelta(hours=24)
    for r in await list_for(user_id, limit=100):
        proposal = r.get("proposal") or {}
        if (r.get("status") == "armed" and proposal.get("mint") == mint and int(proposal.get("amount_raw") or 0) == int(amount_raw)
                and datetime.fromisoformat(r["created_at"]) >= since):
            return r
    return None


async def link_plan(user_id: str, plan) -> dict | None:
    """Called by the chat path when a trade plan is created: the armed
    receipt whose sell line produced it is marked prepared and carries the
    plan id, so the plan's outcome reaches the receipt."""
    proposal = getattr(plan, "proposal", None)
    if proposal is None or getattr(plan, "status", None) != "pending_confirmation":
        return None
    receipt = await _latest_armed(user_id, proposal.input_mint, proposal.amount_atomic)
    if receipt is None:
        return None
    await set_outcome(receipt["id"], "prepared", plan_id=plan.plan_id, outcome={"plan_status": "pending_confirmation", "at": _now().isoformat()})
    return await get(receipt_id=receipt["id"])


_PLAN_OUTCOMES = {"submitting": "submitting", "submitted": "submitted", "submission_unknown": "submitted", "executed": "executed",
                  "failed": "failed", "expired": "expired", "superseded": "superseded", "rejected": "rejected"}


async def on_plan_status(plan_id: str, status: str) -> None:
    """The plan's status change, written to the receipt that produced it.
    Never raises into the plan store."""
    mapped = _PLAN_OUTCOMES.get(status)
    if not mapped or not plan_id:
        return
    try:
        pool = await _pool()
        if pool is not None:
            rows = [_from_db(r) for r in await pool.fetch("SELECT * FROM agent_receipts WHERE plan_id = $1", plan_id)]
        else:
            with _lock:
                rows = [json.loads(json.dumps(r)) for r in _receipts.values() if r.get("plan_id") == plan_id]
        for r in rows:
            await set_outcome(r["id"], mapped, plan_id=plan_id, outcome={**(r.get("outcome") or {}), "plan_status": status, "at": _now().isoformat()})
    except Exception:
        logger.warning("agent_rules: plan status %s for %s not recorded", status, plan_id, exc_info=True)


def public(receipt: dict) -> dict:
    return {k: receipt.get(k) for k in ("id", "kind", "source_id", "status", "signer", "reason", "trigger", "quote", "proposal", "checks", "plan_id", "outcome", "created_at", "resolved_at")}


# ----------------------------------------------------------------------------
# the checks a background rule can make deterministically
# ----------------------------------------------------------------------------

def charter_precheck(fields: dict | None, notional_usd: float | None, slippage_bps: int, price_impact_pct: float | None) -> dict:
    """The built-in caps and the structured charter rules that a quote alone
    can settle. The rest (verified tokens only, position share, chains) is
    checked again by the trade path at confirm time and is listed as
    deferred, never assumed."""
    violations: list[str] = []
    unresolved: list[str] = []
    fields = fields or {}
    if notional_usd is None:
        unresolved.append("the quote carries no USD value, so the per-trade caps cannot be checked")
    else:
        if notional_usd > settings.max_trade_usd:
            violations.append(f"${notional_usd:,.2f} is over the built-in max per trade of ${settings.max_trade_usd:,.2f}")
        cap = fields.get("max_trade_usd")
        if cap is not None and notional_usd > float(cap):
            violations.append(f"${notional_usd:,.2f} is over your max per trade of ${float(cap):,.2f}")
    if slippage_bps > settings.max_slippage_bps:
        violations.append(f"{slippage_bps} bps is over the built-in max slippage of {settings.max_slippage_bps} bps")
    bps = fields.get("max_slippage_bps")
    if bps is not None and slippage_bps > int(bps):
        violations.append(f"{slippage_bps} bps is over your max slippage of {int(bps)} bps")
    if price_impact_pct is not None and price_impact_pct > settings.max_price_impact_pct:
        violations.append(f"price impact {price_impact_pct:.2f}% is over the built-in max of {settings.max_price_impact_pct:g}%")
    deferred = [k for k in ("verified_only", "max_position_pct", "allowed_chains") if fields.get(k)]
    return {"ok": not violations and not unresolved, "violations": violations, "unresolved": unresolved,
            "deferred": deferred, "checked": ["max_trade_usd", "max_slippage_bps", "max_price_impact_pct"]}


def _ui_amount(amount_raw: int, decimals: int) -> str:
    text = f"{amount_raw / (10 ** decimals):.{decimals}f}" if decimals else str(amount_raw)
    return text.rstrip("0").rstrip(".") if "." in text else text


def proposal_for(row: dict, fraction: float, quote: dict, slippage_bps: int) -> dict:
    """The exact sell the user is handed: the size is the fraction of the
    quantity the quote was taken for, the token is pinned by its mint."""
    decimals = int(row.get("decimals") or 0)
    amount_raw = int(quote.get("amount_raw") or int(int(row["quantity_raw"]) * fraction))
    amount = _ui_amount(amount_raw, decimals)
    mint = row["mint"]
    return {"action": "sell", "chain": row.get("chain") or "solana", "mint": mint, "symbol": row.get("symbol") or mint[:6], "fraction": fraction,
            "amount_raw": amount_raw, "amount": amount, "output": "USDC", "slippage_bps": slippage_bps,
            "prompt": f"sell {amount} {mint} for USDC on solana with {slippage_bps} bps slippage"}


def _usd(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "unknown"


async def _charter_fields(user_id: str) -> dict | None:
    from app import accounts
    try:
        user = await accounts.get_user(user_id)
    except Exception:
        logger.warning("agent_rules: user lookup failed for %s", user_id[:8], exc_info=True)
        return None
    return ((user or {}).get("preferences") or {}).get("risk_charter_fields") or {}


# ----------------------------------------------------------------------------
# the armed exit
# ----------------------------------------------------------------------------

async def arm_exit(row: dict, alert: dict, rows: list[dict]) -> dict | None:
    """Called by the exit monitor after a deterioration or discount alert was
    stored. Returns the receipt, or None when the position carries no arm
    rule. Never raises into the monitor."""
    rules = row.get("rules") or {}
    try:
        fraction = float(rules.get("arm_fraction") or 0)
    except (TypeError, ValueError):
        return None
    if not (0 < fraction <= 1) or alert.get("kind") not in ("deterioration", "discount") or not row.get("user_id"):
        return None
    from app import exit_monitor, tasks
    user_id = row["user_id"]
    paper = bool(rules.get("arm_paper"))
    max_per_day = int(rules.get("arm_max_per_day") or DEFAULT_MAX_PER_DAY)
    slippage_bps = int(rules.get("arm_slippage_bps") or DEFAULT_SLIPPAGE_BPS)
    symbol = row.get("symbol") or row["mint"][:6]
    existing = await find_by_trigger(row["id"], alert.get("at"))
    if existing is not None:
        # This firing was already receipted (a delivery failed after the
        # receipt was written): deliver again, never a second receipt.
        await _deliver(row, alert, existing, rules)
        return existing
    # The size is a fraction of the quantity these quotes were taken for (the
    # full-exit row carries it), never the position row's stored quantity,
    # which can be a tick stale (review 2026-09-27).
    quoted_raw = next((int(r["amount_raw"]) for r in rows if r.get("fraction") == 1.0 and r.get("amount_raw")), int(row["quantity_raw"]))
    quote = next((r for r in rows if r.get("fraction") == fraction), None)
    if quote is None:
        try:
            quote = (await exit_monitor.quote_exit(row["mint"], quoted_raw, fractions=(fraction,)))[0]
        except Exception:
            logger.info("agent_rules: sized quote failed for %s", row["id"], exc_info=True)
            quote = None
    receipt = {"id": uuid.uuid4().hex, "user_id": user_id, "kind": KIND_EXIT, "source_id": row["id"], "signer": "paper" if paper else "user",
               "trigger": {k: alert.get(k) for k in ("at", "kind", "drop_pct", "discount_pct")} | {"baseline": alert.get("baseline")},
               "quote": quote, "proposal": None, "checks": None, "plan_id": None, "reason": None, "created_at": _now().isoformat(), "resolved_at": None, "outcome": None}
    today = await count_today(row["id"])
    if today >= max_per_day:
        receipt.update(status="refused", reason=f"daily cap reached: {today} of {max_per_day} already prepared today")
    elif quote is None or not quote.get("ok"):
        receipt.update(status="refused", reason=f"no quote for a {fraction:.0%} exit: {(quote or {}).get('error') or 'quote unavailable'}")
    else:
        proposal = proposal_for({**row, "quantity_raw": quoted_raw}, fraction, quote, slippage_bps)
        checks = charter_precheck(await _charter_fields(user_id), quote.get("quoted_usdc"), slippage_bps, quote.get("price_impact_pct"))
        receipt.update(proposal=proposal, checks=checks)
        if checks["violations"] or checks["unresolved"]:
            receipt.update(status="refused", reason="; ".join(checks["violations"] + checks["unresolved"]))
        else:
            receipt["status"] = "paper" if paper else "armed"
    await _insert(receipt)                                          # the record exists before anyone is told
    await _deliver(row, alert, receipt, rules)
    return receipt


async def _deliver(row: dict, alert: dict, receipt: dict, rules: dict) -> None:
    """The inbox item (once per firing: the occurrence key de-duplicates a
    retry), email or Telegram per the position's channel. An armed receipt
    carries the one-tap action: the exact sell line to send."""
    from app import exit_monitor, tasks
    user_id = row["user_id"]
    symbol = row.get("symbol") or row["mint"][:6]
    today = await count_today(row["id"])
    max_per_day = int(rules.get("arm_max_per_day") or DEFAULT_MAX_PER_DAY)
    title, body = _notification(symbol, row, alert, receipt, max(today - 1, 0), max_per_day)
    data = ({"receipt_id": receipt["id"], "action": {"label": "Prepare the sale", "prompt": receipt["proposal"]["prompt"]}}
            if receipt.get("status") == "armed" and receipt.get("proposal") else {"receipt_id": receipt["id"]})
    occurrence = f"{row['id']}:{alert.get('at')}:armed"
    _item, new = await tasks.notify(user_id, title, body, kind="exit_armed", task_id=row["id"], occurrence=occurrence, data=data)
    if new and rules.get("channel") == "email":
        await exit_monitor._email(user_id, title, body)
    if new and rules.get("channel") == "telegram":
        await _telegram(user_id, title, body)


async def _telegram(user_id: str, title: str, body: str) -> None:
    from app import accounts, notifications
    try:
        user = await accounts.get_user(user_id)
        if user:
            await notifications.send_telegram(user, title, body)
    except Exception:
        logger.warning("agent_rules: telegram delivery failed for %s", user_id[:8], exc_info=True)


def _notification(symbol: str, row: dict, alert: dict, receipt: dict, today: int, max_per_day: int) -> tuple[str, str]:
    fired = (f"deteriorated {alert['drop_pct']:.0f}%" if alert.get("kind") == "deterioration" and alert.get("drop_pct") is not None
             else f"discount {alert['discount_pct']:.1f}%" if alert.get("discount_pct") is not None else "rule fired")
    at = str(alert.get("at") or "")[:16].replace("T", " ")
    proposal = receipt.get("proposal") or {}
    quote = receipt.get("quote") or {}
    pct = f"{float(proposal.get('fraction') or 0):.0%}" if proposal else ""
    if receipt["status"] == "refused":
        title = f"{symbol} exit rule fired, not prepared"
        body = (f"Your {symbol} exit {fired} at {at} UTC. The armed exit was not prepared: {receipt['reason']}. "
                "Nothing was quoted for you to confirm; the watch continues.")
        return title, body
    route = " → ".join(x for x in quote.get("route") or [] if x) or "unknown"
    quoted = (f"{pct} of the position ({proposal['amount']} {symbol}) is quoted at {_usd(quote.get('quoted_usdc'))} now, minimum {_usd(quote.get('minimum_usdc'))} "
              f"at {proposal['slippage_bps']} bps, price impact {float(quote.get('price_impact_pct') or 0):.2f}%, route {route}.")
    checks = receipt.get("checks") or {}
    checked = "Checked now: built-in caps" + (" and your charter's per-trade and slippage rules" if checks.get("checked") else "") + "."
    deferred = (" Checked again at confirm: " + ", ".join(c.replace("_", " ") for c in checks["deferred"]) + ".") if checks.get("deferred") else ""
    count = f" Rule: {today + 1} of {max_per_day} prepared today."
    if receipt["status"] == "paper":
        title = f"{symbol}: paper exit recorded ({pct})"
        body = (f"Your {symbol} exit {fired} at {at} UTC. Paper mode: {quoted} Recorded only; nothing to confirm. "
                f"To make this real, say `arm my {symbol} exit: sell {pct} if it drops {float((row.get('rules') or {}).get('drop_pct') or settings.exit_alert_drop_pct):g}%`." + count)
        return title, body
    title = f"{symbol}: {pct} exit ready to confirm"
    body = (f"Your {symbol} exit {fired} at {at} UTC. {quoted} {checked}{deferred}\n\n"
            f"Ready to confirm: send `{proposal['prompt']}` in {settings.product_name}. You get a fresh quote and the CONFIRM card, and your wallet signs it. "
            f"Token pinned by mint {row['mint']}. Nothing is prepared or signed until you do; the watch continues." + count)
    return title, body


def describe_rules(rules: dict) -> str:
    """The armed part of a position's rules, for the exit list."""
    try:
        fraction = float((rules or {}).get("arm_fraction") or 0)
    except (TypeError, ValueError):
        return ""
    if not (0 < fraction <= 1):
        return ""
    mode = "paper" if rules.get("arm_paper") else "ready to confirm"
    return f" · armed: {fraction:.0%} exit {mode}, up to {int(rules.get('arm_max_per_day') or DEFAULT_MAX_PER_DAY)}/day"
