import json
import secrets
from contextvars import ContextVar
import re
import threading
from datetime import datetime, timedelta, timezone

from app.db import get_pg_pool, get_redis
from app.jupiter import jupiter, normalize_mint
from app.models import SwapProposal, TokenInfo, TradePlan
from app.settings import settings
from app.solana_rpc import simulate_transaction


# Who this turn's plans belong to. A contextvar for the same reason the call
# budget is one (app/call_budget.py): threading it through AgentState would
# touch every node for a value only plan creation needs.
_plan_owner: ContextVar[str | None] = ContextVar("orbit_plan_owner", default=None)


def set_plan_owner(account_id: str | None):
    """Returns the token to reset with; call from the request/turn boundary."""
    return _plan_owner.set(account_id)


def reset_plan_owner(token) -> None:
    _plan_owner.reset(token)


def plan_owner() -> str | None:
    return _plan_owner.get()


_plans: dict[str, TradePlan] = {}  # in-memory fallback when Postgres is unavailable
_prepared_transactions: dict[str, str] = {}
_memory_plan_lock = threading.Lock()
_BLOCKING_SHIELD_WARNING = re.compile(
    r"honeypot|known[\s_-]*scam|malicious|freeze[\s_-]*authority|freezable|frozen[\s_-]*token",
    re.IGNORECASE,
)


def _prune_memory() -> None:
    now = datetime.now(timezone.utc)
    unresolved = {"submitting", "submitted", "submission_unknown"}
    expired = [plan_id for plan_id, plan in _plans.items() if plan.expires_at <= now and plan.status not in unresolved]
    for plan_id in expired:
        _plans.pop(plan_id, None)
        _prepared_transactions.pop(plan_id, None)
    while len(_plans) >= settings.memory_plan_max_entries:
        removable = [item for item in _plans.values() if item.status not in unresolved]
        if not removable:
            raise RuntimeError("Pending execution capacity reached; reconcile existing submissions first")
        oldest = min(removable, key=lambda item: item.created_at)
        _plans.pop(oldest.plan_id, None)
        _prepared_transactions.pop(oldest.plan_id, None)


async def store_prepared_transaction(plan_id: str, transaction: str) -> None:
    redis = await get_redis()
    if redis is not None:
        await redis.set(f"trade_transaction:{plan_id}", transaction, ex=settings.plan_ttl_seconds)
        return
    _prune_memory()
    _prepared_transactions[plan_id] = transaction


async def get_prepared_transaction(plan: TradePlan) -> str:
    redis = await get_redis()
    transaction = (
        await redis.get(f"trade_transaction:{plan.plan_id}")
        if redis is not None
        else _prepared_transactions.get(plan.plan_id)
    )
    if transaction:
        return transaction
    raise ValueError("The exact reviewed transaction is no longer available; create a fresh trade plan")


async def _store_plan(plan: TradePlan) -> None:
    pool = await get_pg_pool()
    if pool is None:
        _prune_memory()
        _plans[plan.plan_id] = plan
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO trade_plans (plan_id, wallet_address, status, payload) VALUES ($1, $2, $3, $4)",
            plan.plan_id,
            plan.wallet_address,
            plan.status,
            plan.model_dump_json(),
        )


async def _load_plan(plan_id: str) -> TradePlan | None:
    pool = await get_pg_pool()
    if pool is None:
        return _plans.get(plan_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT payload FROM trade_plans WHERE plan_id = $1", plan_id)
    if row is None:
        return None
    return TradePlan.model_validate(json.loads(row["payload"]))




async def create_trade_plan(wallet_address: str, proposal: SwapProposal) -> TradePlan:
    """Create a non-executing Jupiter quote and an expiring confirmation plan."""
    proposal = proposal.model_copy(
        update={
            "input_mint": normalize_mint(proposal.input_mint),
            "output_mint": normalize_mint(proposal.output_mint),
        }
    )
    if proposal.slippage_bps > settings.max_slippage_bps:
        raise ValueError("Requested slippage exceeds the configured maximum")

    input_raw = await jupiter.token_by_mint(proposal.input_mint)
    output_raw = await jupiter.token_by_mint(proposal.output_mint)
    input_token = _token_info(input_raw)
    output_token = _token_info(output_raw)
    input_value_usd = None
    if input_token.usd_price is not None:
        input_value_usd = proposal.amount_atomic / (10 ** input_token.decimals) * input_token.usd_price
        if input_value_usd > settings.max_trade_usd:
            raise ValueError(
                f"Trade value ${input_value_usd:.2f} exceeds MAX_TRADE_USD=${settings.max_trade_usd:.2f}"
            )
    else:
        raise ValueError("Input token has no trusted Jupiter USD price; notional limit cannot be enforced")

    shield = await jupiter.shield([proposal.input_mint, proposal.output_mint])
    warnings = _flatten_warnings(shield)
    blocking = [warning for warning in warnings if _BLOCKING_SHIELD_WARNING.search(warning)]
    if blocking:
        raise ValueError(
            "Token security policy blocked this trade: " + "; ".join(blocking[:3])
        )
    quote = await jupiter.quote(
        proposal.input_mint,
        proposal.output_mint,
        proposal.amount_atomic,
        proposal.slippage_bps,
    )
    price_impact = float(quote.get("priceImpactPct") or 0) * 100
    if price_impact > settings.max_price_impact_pct:
        raise ValueError(
            f"Price impact {price_impact:.2f}% exceeds maximum {settings.max_price_impact_pct:.2f}%"
        )
    built = await jupiter.swap_transaction(quote, wallet_address)
    simulation = await simulate_transaction(built["swapTransaction"])
    if not simulation["ok"]:
        raise ValueError(f"Transaction simulation failed: {simulation['error']}")
    now = datetime.now(timezone.utc)
    plan_id = secrets.token_urlsafe(16)
    confirmation = f"CONFIRM {plan_id}"
    plan = TradePlan(
        plan_id=plan_id,
        status="pending_confirmation",
        created_at=now,
        expires_at=now + timedelta(seconds=settings.plan_ttl_seconds),
        wallet_address=wallet_address,
        proposal=proposal,
        quote=quote,
        input_token=input_token,
        output_token=output_token,
        input_value_usd=input_value_usd,
        warnings=warnings,
        simulation=simulation,
        confirmation_text=confirmation,
        owner_account_id=_plan_owner.get(),
    )
    await _store_plan(plan)
    await store_prepared_transaction(plan.plan_id, built["swapTransaction"])
    return plan


def _token_info(item: dict) -> TokenInfo:
    tags = set(item.get("tags") or [])
    return TokenInfo(
        mint=item["id"],
        symbol=item.get("symbol") or "UNKNOWN",
        name=item.get("name") or "Unknown token",
        decimals=int(item["decimals"]),
        usd_price=float(item["usdPrice"]) if item.get("usdPrice") is not None else None,
        verified="verified" in tags,
        organic_score=(
            float(item["organicScore"]) if item.get("organicScore") is not None else None
        ),
    )


async def simulate_swap(input_mint: str, output_mint: str, amount_atomic: int) -> dict:
    """Read-only: a real Jupiter quote and real token info, nothing else.

    Deliberately does NOT call jupiter.shield()/swap_transaction(),
    simulate_transaction(), or create/store a TradePlan -- there is no
    wallet interaction here at all, no signable transaction is ever built,
    and no CONFIRM token is ever issued. This is a different, shorter
    function than create_trade_plan(), not a flag on it -- there is no
    code path from here into the real trade-execution pipeline.
    """
    input_mint = normalize_mint(input_mint)
    output_mint = normalize_mint(output_mint)
    input_token = _token_info(await jupiter.token_by_mint(input_mint))
    output_token = _token_info(await jupiter.token_by_mint(output_mint))
    quote = await jupiter.quote(input_mint, output_mint, amount_atomic, slippage_bps=50)

    input_amount = amount_atomic / (10 ** input_token.decimals)
    input_value_usd = input_amount * input_token.usd_price if input_token.usd_price is not None else None

    out_amount_raw = quote.get("outAmount")
    output_amount = float(out_amount_raw) / (10 ** output_token.decimals) if out_amount_raw is not None else None
    output_value_usd = (
        output_amount * output_token.usd_price
        if output_amount is not None and output_token.usd_price is not None
        else None
    )
    price_impact_pct = float(quote.get("priceImpactPct") or 0) * 100

    return {
        "input_token": input_token,
        "output_token": output_token,
        "input_amount": input_amount,
        "input_value_usd": input_value_usd,
        "output_amount": output_amount,
        "output_value_usd": output_value_usd,
        "price_impact_pct": price_impact_pct,
        "quote": quote,
    }


def _flatten_warnings(payload: dict) -> list[str]:
    warnings: list[str] = []
    for mint, entries in (payload.get("warnings") or {}).items():
        for entry in entries or []:
            if isinstance(entry, dict):
                message = entry.get("message") or entry.get("type") or str(entry)
            else:
                message = str(entry)
            warnings.append(f"{mint}: {message}")
    return warnings


async def get_plan(plan_id: str) -> TradePlan:
    plan = await _load_plan(plan_id)
    if not plan:
        raise KeyError("Unknown trade plan")
    if plan.status == "pending_confirmation" and datetime.now(timezone.utc) > plan.expires_at:
        pool = await get_pg_pool()
        if pool is None:
            with _memory_plan_lock:
                if plan.status == "pending_confirmation":
                    plan.status = "expired"
        else:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE trade_plans SET status='expired', payload=jsonb_set(payload,'{status}','\"expired\"'::jsonb) "
                    "WHERE plan_id=$1 AND status='pending_confirmation' AND (payload->>'expires_at')::timestamptz <= now()",
                    plan_id,
                )
            plan = await _load_plan(plan_id)
    return plan


async def claim_plan_submission(plan: TradePlan, signature: str | None = None) -> None:
    """Atomically claim a pending plan before its transaction can be broadcast."""
    pool = await get_pg_pool()
    if pool is None:
        with _memory_plan_lock:
            current = _plans.get(plan.plan_id)
            if current is None or current.status != "pending_confirmation":
                status = current.status if current is not None else "missing"
                raise ValueError(f"Plan cannot execute in status {status}")
            if datetime.now(timezone.utc) >= current.expires_at:
                current.status = "expired"
                raise ValueError("Trade plan has expired")
            current.status = "submitting"
            current.submission_signature = signature
            current.submitted_at = datetime.now(timezone.utc)
            _plans[plan.plan_id] = current
            plan.status = "submitting"
            plan.submission_signature = signature
            plan.submitted_at = current.submitted_at
        return
    submitting = plan.model_copy(update={"status": "submitting", "submission_signature": signature, "submitted_at": datetime.now(timezone.utc)})
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE trade_plans SET status = $1, payload = $2 WHERE plan_id = $3 AND status = $4 "
            "AND (payload->>'expires_at')::timestamptz > now()",
            "submitting",
            submitting.model_dump_json(),
            plan.plan_id,
            "pending_confirmation",
        )
    if result != "UPDATE 1":
        raise ValueError("Trade plan is already being submitted or is no longer executable")
    plan.status = "submitting"
    plan.submission_signature = signature
    plan.submitted_at = submitting.submitted_at


async def unsettled_plans(limit: int = 100) -> list[TradePlan]:
    pool = await get_pg_pool()
    if pool is None:
        return [p for p in _plans.values() if p.status in {"submitting", "submitted", "submission_unknown"} and p.submission_signature][:limit]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload FROM trade_plans WHERE status IN ('submitting','submitted','submission_unknown') "
            "AND payload->>'submission_signature' IS NOT NULL "
            "ORDER BY (payload->>'reconciled_at')::timestamptz NULLS FIRST, created_at LIMIT $1", limit,
        )
    return [TradePlan.model_validate(json.loads(row["payload"])) for row in rows]


async def update_submission(plan: TradePlan, status: str) -> None:
    """CAS prevents a delayed broadcaster from overwriting a reconciled outcome."""
    if status not in {"submitted", "submission_unknown", "executed", "failed"}:
        raise ValueError("Invalid submission transition")
    pool = await get_pg_pool()
    now = datetime.now(timezone.utc)
    if pool is None:
        with _memory_plan_lock:
            current = _plans.get(plan.plan_id)
            if current and current.status in {"submitting", "submitted", "submission_unknown"}:
                current.status = status
                current.reconciled_at = now
        return
    updated = plan.model_copy(update={"status": status, "reconciled_at": now})
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE trade_plans SET status=$1, payload=$2 WHERE plan_id=$3 "
            "AND status IN ('submitting','submitted','submission_unknown') "
            "AND payload->>'submission_signature'=$4",
            status, updated.model_dump_json(), plan.plan_id, plan.submission_signature,
        )
    if result == "UPDATE 1":
        plan.status = status
        plan.reconciled_at = now


async def mark_plan_superseded(plan_id: str) -> None:
    pool = await get_pg_pool()
    if pool is None:
        with _memory_plan_lock:
            plan = _plans.get(plan_id)
            if plan and plan.status == "pending_confirmation":
                plan.status = "superseded"
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE trade_plans SET status = 'superseded', "
            "payload = jsonb_set(payload::jsonb, '{status}', '\"superseded\"'::jsonb) "
            "WHERE plan_id = $1 AND status = 'pending_confirmation'", plan_id,
        )
