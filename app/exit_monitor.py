"""Position-specific exit analysis: can this position get out, at what price, and how has that changed.

The proposition (2026-09-22): before entering a meme token, know who
controls the supply, whether your position can exit, and when those
conditions change. This module is the second part. It reads the wallet's
real balance of a token, asks Jupiter for an exact-size sell quote to
USDC at 25, 50 and 100 percent of the position, and shows four numbers
that are never conflated:

- marked value: quantity times the reference price (what a portfolio says);
- quoted proceeds: what the current route would return for that quantity;
- minimum output: the transaction's own floor at the quoted slippage;
- realised proceeds: what a confirmed sale returned (not here: no sale is
  ever prepared by this module; it is read-only, like plans.simulate_swap).

Partial exits are separate scenarios, each quoted on the full current
book; their proceeds do not add up. A monitored position is a scheduled
durable job (app/jobs.py) that re-reads the balance, records the quotes,
and raises an inbox alert when the full-exit quote has fallen by more
than `exit_alert_drop_pct` against the entry quote or the last alert,
with the two quotes side by side. Solana only, through Jupiter.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

from app import jobs, tasks
from app.db import apply_schema, get_pg_pool, memory_is_the_store
from app.jupiter import jupiter, normalize_mint
from app.plans import simulate_swap
from app.settings import settings
from app.solana_rpc import get_token_accounts

logger = logging.getLogger(__name__)

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
FRACTIONS = (0.25, 0.5, 1.0)
KIND = "exit_monitor"

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS exit_positions (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    wallet TEXT NOT NULL,
    mint TEXT NOT NULL,
    chain TEXT NOT NULL DEFAULT 'solana',
    symbol TEXT,
    decimals INTEGER NOT NULL DEFAULT 0,
    quantity_raw NUMERIC NOT NULL DEFAULT 0,
    entry JSONB,
    last_alert JSONB,
    job_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS exit_positions_user_idx ON exit_positions (user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS exit_quotes (
    id UUID PRIMARY KEY,
    position_id UUID NOT NULL REFERENCES exit_positions(id) ON DELETE CASCADE,
    taken_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    quantity_raw NUMERIC NOT NULL,
    quotes JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS exit_quotes_position_idx ON exit_quotes (position_id, taken_at DESC);
"""
_ready = False
_positions: dict[str, dict] = {}
_history: dict[str, list[dict]] = {}
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def reset_for_test() -> None:
    global _ready
    with _lock:
        _positions.clear()
        _history.clear()
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


# ----------------------------------------------------------------------------
# the position and its quotes
# ----------------------------------------------------------------------------

class BalanceUnavailable(RuntimeError):
    """The chain could not be read: not a zero, not a position (review,
    2026-09-22: a failed RPC call was reported as "holds none")."""


async def position_of(wallet: str, mint: str) -> dict | None:
    """The wallet's real balance of the mint from the chain, both token
    programs, summed across accounts. None when it holds none; raises
    BalanceUnavailable when a lookup failed and nothing proved a holding."""
    mint = normalize_mint(mint)
    total, decimals, failed = 0, None, []
    for program in (None, TOKEN_2022_PROGRAM):
        try:
            payload = await get_token_accounts(wallet) if program is None else await _token_accounts_2022(wallet)
        except Exception as exc:
            logger.info("exit_monitor: token accounts failed for %s", wallet[:8], exc_info=True)
            failed.append(f"{'token' if program is None else 'token-2022'} program: {type(exc).__name__}")
            continue
        for account in (payload or {}).get("value") or []:            # rpc() already unwraps the JSON-RPC result
            info = (((account.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            if info.get("mint") != mint:
                continue
            amount = info.get("tokenAmount") or {}
            total += int(amount.get("amount") or 0)
            decimals = int(amount.get("decimals") or 0) if decimals is None else decimals
    if failed and total <= 0:
        raise BalanceUnavailable("; ".join(failed))
    if total <= 0:
        return None
    return {"mint": mint, "quantity_raw": total, "decimals": decimals or 0, "quantity": total / (10 ** (decimals or 0)), "partial_read": bool(failed)}


async def _token_accounts_2022(wallet: str) -> dict:
    from app.solana_rpc import rpc

    return await rpc("getTokenAccountsByOwner", [wallet, {"programId": TOKEN_2022_PROGRAM}, {"encoding": "jsonParsed", "commitment": "confirmed"}])


async def quote_exit(mint: str, quantity_raw: int, fractions: tuple[float, ...] = FRACTIONS) -> list[dict]:
    """One exact-size sell quote to USDC per fraction. Each row stands alone:
    a failed route is a row that says so, never a missing row."""
    rows = []
    for fraction in fractions:
        amount = int(quantity_raw * fraction)
        if amount <= 0:
            rows.append({"fraction": fraction, "amount_raw": 0, "ok": False, "error": "nothing to sell"})
            continue
        try:
            sim = await simulate_swap(mint, USDC_MINT, amount)
        except Exception as exc:  # noqa: BLE001 - the quote's failure is the finding
            rows.append({"fraction": fraction, "amount_raw": amount, "ok": False, "error": _route_error(exc)})
            continue
        quote = sim.get("quote") or {}
        threshold = quote.get("otherAmountThreshold")
        rows.append({
            "fraction": fraction, "amount_raw": amount, "ok": True,
            "tokens": sim["input_amount"], "symbol": sim["input_token"].symbol,
            "marked_value_usd": sim["input_value_usd"], "reference_price_usd": sim["input_token"].usd_price,
            "quoted_usdc": sim["output_amount"], "minimum_usdc": float(threshold) / 1e6 if threshold is not None else None,
            "price_impact_pct": sim["price_impact_pct"], "slippage_bps": int(quote.get("slippageBps") or 50),
            "route": [(hop.get("swapInfo") or {}).get("label") for hop in quote.get("routePlan") or [] if isinstance(hop, dict)],
            "quoted_at": _now().isoformat(),
        })
    return rows


def _route_error(exc: Exception) -> str:
    """"no route" only when the quote provider SAID so; a timeout, a dropped
    connection, an auth error or an unknown failure is unavailable data
    (review, 2026-09-22: a read timeout raised the no-route alert)."""
    import httpx

    text = str(exc)
    name = type(exc).__name__
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        # The provider's answer is in the body, not the message: Jupiter says
        # {"errorCode": "COULD_NOT_FIND_ANY_ROUTE", ...} with HTTP 400.
        try:
            body = exc.response.json()
            text += " " + " ".join(str(body.get(k) or "") for k in ("errorCode", "error", "message", "detail")) if isinstance(body, dict) else ""
        except Exception:
            text += " " + (exc.response.text or "")[:300]
    if isinstance(exc, httpx.TimeoutException) or "timeout" in name.lower() or "timed out" in text.lower():
        return "quote unavailable (timeout)"
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, ConnectionError)) or "connect" in name.lower():
        return "quote unavailable (connection)"
    if "not found uniquely" in text:
        return "token not in Jupiter's registry"
    status = re.search(r"\b(4\d\d|5\d\d)\b", text)
    if status and status.group(1) == "429":
        return "quote provider rate-limited"
    if status and status.group(1) in ("401", "403"):
        return "quote unavailable (auth)"
    if status and status.group(1).startswith("5"):
        return "quote provider outage"
    if re.search(r"\b(?:no|could not find (?:any )?|couldn't find (?:any )?)routes?\b|\bNO_ROUTES?_FOUND\b|\bCOULD_NOT_FIND_ANY_ROUTE\b", text, re.I):
        return "no route"
    return f"quote failed: {name}"


def full_exit(rows: list[dict]) -> dict | None:
    return next((r for r in rows if r.get("fraction") == 1.0 and r.get("ok")), None)


def deterioration_pct(before: dict | None, now: dict | None) -> float | None:
    """How much the full-exit quoted proceeds fell, in percent (positive = worse)."""
    if not before or not now or not before.get("quoted_usdc") or now.get("quoted_usdc") is None:
        return None
    return (before["quoted_usdc"] - now["quoted_usdc"]) / before["quoted_usdc"] * 100


# ----------------------------------------------------------------------------
# the card
# ----------------------------------------------------------------------------

def _usd(v) -> str:
    return "—" if v is None else f"${v:,.2f}"


def _qty(v) -> str:
    """Token quantities with thousands separators and no scientific notation."""
    if v is None:
        return "—"
    v = float(v)
    if v >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.6f}".rstrip("0").rstrip(".") or "0"


def render_card(position: dict, rows: list[dict], entry: dict | None = None, history: list[dict] | None = None) -> str:
    symbol = position.get("symbol") or next((r.get("symbol") for r in rows if r.get("ok")), None) or position["mint"][:6]
    qty = position["quantity_raw"] / (10 ** position.get("decimals", 0))
    lines = [f"# Exit analysis — {symbol}",
             f"**Wallet**: `{position['wallet'][:6]}…{position['wallet'][-4:]}` · **Position**: {_qty(qty)} {symbol} · **Quoted**: {_now().strftime('%Y-%m-%d %H:%M UTC')} · Jupiter, to USDC",
             "", "| Exit | Tokens | Marked value | Quoted proceeds | Minimum out | Price impact | Route |", "|---|---:|---:|---:|---:|---:|---|"]
    for r in rows:
        label = f"{int(r['fraction'] * 100)}%"
        if not r.get("ok"):
            lines.append(f"| {label} | {_qty(r.get('amount_raw', 0) / (10 ** position.get('decimals', 0)))} | — | **{r.get('error', 'no quote')}** | — | — | — |")
            continue
        lines.append(f"| {label} | {_qty(r['tokens'])} | {_usd(r['marked_value_usd'])} | **{_usd(r['quoted_usdc'])}** | {_usd(r['minimum_usdc'])} "
                     f"| {r['price_impact_pct']:.2f}% | {' → '.join(x for x in r['route'] if x) or '—'} |")
    full = full_exit(rows)
    if full and full.get("marked_value_usd"):
        gap = (full["marked_value_usd"] - full["quoted_usdc"]) / full["marked_value_usd"] * 100
        if gap > 0.05:
            lines += ["", f"Selling everything now would return **{_usd(full['quoted_usdc'])}** against a marked value of {_usd(full['marked_value_usd'])}: "
                          f"**{gap:.1f}%** is the cost of getting out at this size ({full['price_impact_pct']:.2f}% price impact at {full['slippage_bps']} bps slippage)."]
        else:
            # The quote is at or above the marked value: there is no cost to
            # report, and "-0.4% is the cost" is not a sentence (live, 2026-09-22).
            lines += ["", f"Selling everything now would return **{_usd(full['quoted_usdc'])}**, at or above its marked value of {_usd(full['marked_value_usd'])}: "
                          f"no measurable cost of getting out at this size ({full['price_impact_pct']:.2f}% price impact at {full['slippage_bps']} bps slippage)."]
    # The change since entry is shown once there is a later quote to compare;
    # at the moment of watching the entry IS this quote (live, 2026-09-22).
    if entry and full and history and len(history) > 1:
        entry_full = full_exit(entry.get("rows") or [])
        drop = deterioration_pct(entry_full, full)
        if drop is not None:
            when = (entry.get("at") or "")[:16].replace("T", " ")
            lines.append(f"Since you started watching ({when} UTC): a full exit was quoted at {_usd(entry_full['quoted_usdc'])}, now {_usd(full['quoted_usdc'])} "
                         f"(**{-drop:+.1f}%**).")
    failed = [r for r in rows if not r.get("ok")]
    if failed:
        lines += ["", "Not quoted: " + "; ".join(f"{int(r['fraction'] * 100)}% ({r.get('error')})" for r in failed) + ". A missing quote is not a zero; it is unknown until the route answers."]
    if history:
        lines += ["", "## Full-exit quote over time", "| Taken | Position | Quoted proceeds | Price impact |", "|---|---:|---:|---:|"]
        for h in history[:8]:
            f = full_exit(h.get("quotes") or [])
            lines.append(f"| {(h.get('taken_at') or '')[:16].replace('T', ' ')} | {_qty(h.get('quantity_raw', 0) / (10 ** position.get('decimals', 0)))} | "
                         f"{_usd(f['quoted_usdc']) if f else '—'} | {f['price_impact_pct']:.2f}% |" if f else
                         f"| {(h.get('taken_at') or '')[:16].replace('T', ' ')} | {_qty(h.get('quantity_raw', 0) / (10 ** position.get('decimals', 0)))} | — | — |")
    lines += ["", "Quoted proceeds are an estimate until a sale executes; the minimum is what the transaction would refuse to go below. "
                  "Each exit size is quoted on the whole current book, so partial exits do not add up. Nothing here prepares or submits a trade."]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# persistence
# ----------------------------------------------------------------------------

def _from_db(r) -> dict:
    row = dict(r)
    row["id"] = str(row["id"]).replace("-", "")
    row["user_id"] = str(row["user_id"]) if row.get("user_id") else None
    row["quantity_raw"] = int(row["quantity_raw"] or 0)
    for key in ("entry", "last_alert"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    for key in ("created_at", "updated_at"):
        if isinstance(row.get(key), datetime):
            row[key] = row[key].isoformat()
    return row


async def watch(user_id: str, wallet: str, mint: str, symbol: str | None, position: dict, entry_rows: list[dict]) -> dict:
    """Start watching: persist the position with its entry quotes and
    schedule the monitor job."""
    if len([p for p in await list_for(user_id) if p["status"] == "active"]) >= settings.exit_positions_per_user:
        raise ValueError(f"You already watch {settings.exit_positions_per_user} positions; stop one first.")
    position_id = uuid.uuid4().hex
    # The entry is a baseline only when the full-exit quote succeeded;
    # otherwise the monitor is pending until its first valid quote (review,
    # 2026-09-22: a failed entry quote disabled the alert for good).
    baseline = full_exit(entry_rows) is not None
    entry = {"at": _now().isoformat(), "rows": entry_rows, "baseline": baseline, "quantity_raw": position["quantity_raw"]}
    # The job first: a position whose job could not be created is not
    # created either (review, 2026-09-22: a registration that failed halfway
    # said "already watching" and never ran).
    job = await jobs.create(KIND, {"position_id": position_id}, user_id=user_id,
                            next_run_at=_now() + timedelta(minutes=settings.exit_monitor_interval_minutes))
    row = {"id": position_id, "user_id": user_id, "wallet": wallet, "mint": position["mint"], "chain": "solana", "symbol": symbol,
           "decimals": position["decimals"], "quantity_raw": position["quantity_raw"], "entry": entry,
           "last_alert": None, "job_id": job["id"], "status": "active", "created_at": _now().isoformat(), "updated_at": _now().isoformat()}
    try:
        pool = await _pool()
        if pool is not None:
            await pool.execute(
                "INSERT INTO exit_positions (id, user_id, wallet, mint, chain, symbol, decimals, quantity_raw, entry, job_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)",
                uuid.UUID(row["id"]), user_id, wallet, row["mint"], "solana", symbol, row["decimals"], row["quantity_raw"], json.dumps(row["entry"]), job["id"])
        else:
            with _lock:
                _positions[row["id"]] = row
        await record(row["id"], position["quantity_raw"], entry_rows)
    except Exception:
        # All or nothing: whatever landed is removed, the job cancelled
        # (review, 2026-09-22: a failed history write left an active
        # position pointing at a cancelled job).
        try:
            await _delete(row["id"])
        except Exception:
            logger.warning("exit_monitor: could not remove the half-registered position %s", row["id"], exc_info=True)
        await jobs.cancel(job["id"], user_id)
        raise
    return row


async def _delete(position_id: str) -> None:
    pool = await _pool()
    if pool is not None:
        await pool.execute("DELETE FROM exit_positions WHERE id = $1", uuid.UUID(position_id))     # exit_quotes cascade
        return
    with _lock:
        _positions.pop(position_id, None)
        _history.pop(position_id, None)


async def is_live(row: dict) -> bool:
    """An active position whose monitor job exists and will run again."""
    if row.get("status") != "active" or not row.get("job_id"):
        return False
    job = await jobs.get(row["job_id"])
    return bool(job) and job["status"] in ("scheduled", "queued", "running", "waiting_input", "waiting_approval")


async def _update(position_id: str, **fields) -> None:
    pool = await _pool()
    if pool is not None:
        sets, args = [], []
        for i, (k, v) in enumerate(fields.items(), start=1):
            sets.append(f"{k} = ${i}::jsonb" if k in ("entry", "last_alert") else f"{k} = ${i}")
            args.append(json.dumps(v) if k in ("entry", "last_alert") else v)
        await pool.execute(f"UPDATE exit_positions SET {', '.join(sets)}, updated_at = NOW() WHERE id = ${len(args) + 1}", *args, uuid.UUID(position_id))
        return
    with _lock:
        if position_id in _positions:
            _positions[position_id].update(fields)
            _positions[position_id]["updated_at"] = _now().isoformat()


async def get(position_id: str) -> dict | None:
    pool = await _pool()
    if pool is not None:
        r = await pool.fetchrow("SELECT * FROM exit_positions WHERE id = $1", uuid.UUID(position_id))
        return _from_db(r) if r else None
    with _lock:
        row = _positions.get(position_id)
        return json.loads(json.dumps(row)) if row else None


async def list_for(user_id: str) -> list[dict]:
    pool = await _pool()
    if pool is not None:
        return [_from_db(r) for r in await pool.fetch("SELECT * FROM exit_positions WHERE user_id = $1 ORDER BY created_at DESC", user_id)]
    with _lock:
        return sorted([json.loads(json.dumps(r)) for r in _positions.values() if r.get("user_id") == user_id], key=lambda r: r["created_at"], reverse=True)


async def record(position_id: str, quantity_raw: int, rows: list[dict]) -> None:
    entry = {"id": uuid.uuid4().hex, "taken_at": _now().isoformat(), "quantity_raw": quantity_raw, "quotes": rows}
    pool = await _pool()
    if pool is not None:
        await pool.execute("INSERT INTO exit_quotes (id, position_id, quantity_raw, quotes) VALUES ($1, $2, $3, $4::jsonb)",
                           uuid.UUID(entry["id"]), uuid.UUID(position_id), quantity_raw, json.dumps(rows))
        return
    with _lock:
        _history.setdefault(position_id, []).insert(0, entry)
        del _history[position_id][200:]


async def history(position_id: str, limit: int = 50) -> list[dict]:
    pool = await _pool()
    if pool is not None:
        rows = await pool.fetch("SELECT taken_at, quantity_raw, quotes FROM exit_quotes WHERE position_id = $1 ORDER BY taken_at DESC LIMIT $2", uuid.UUID(position_id), limit)
        return [{"taken_at": r["taken_at"].isoformat(), "quantity_raw": int(r["quantity_raw"]), "quotes": json.loads(r["quotes"]) if isinstance(r["quotes"], str) else r["quotes"]} for r in rows]
    with _lock:
        return [json.loads(json.dumps(h)) for h in _history.get(position_id, [])[:limit]]


async def stop(position_id: str, user_id: str) -> bool:
    row = await get(position_id)
    if row is None or row.get("user_id") != user_id or row["status"] != "active":
        return False
    await _update(position_id, status="closed")
    if row.get("job_id"):
        await jobs.cancel(row["job_id"], user_id)
    return True


async def scrub_user(user_id: str) -> int:
    """Account deletion: the database rows cascade with the user; the memory
    store and the monitor jobs are cleared here."""
    n = 0
    with _lock:
        for pid in [k for k, v in _positions.items() if v.get("user_id") == user_id]:
            _positions.pop(pid, None)
            _history.pop(pid, None)
            n += 1
    return n


def public(row: dict) -> dict:
    return {k: row.get(k) for k in ("id", "wallet", "mint", "chain", "symbol", "decimals", "quantity_raw", "entry", "last_alert", "status", "created_at", "updated_at")}


# ----------------------------------------------------------------------------
# the monitor job and the alert
# ----------------------------------------------------------------------------

async def monitor(job: dict, ctx: "jobs.JobContext") -> dict:
    """One tick of a watched position: re-read the balance, quote, record,
    alert on deterioration, and come back in exit_monitor_interval_minutes."""
    position_id = job["spec"]["position_id"]
    row = await get(position_id)
    if row is None or row["status"] != "active":
        return {"answer": "position no longer watched"}          # no next_run_at: the job settles
    await ctx.plan(["Balance", "Quotes", "Compare"])
    next_at = (_now() + timedelta(minutes=settings.exit_monitor_interval_minutes)).isoformat()
    await ctx.step("0")
    try:
        current = await position_of(row["wallet"], row["mint"])
    except BalanceUnavailable as exc:
        # Not a zero: the last known quantity stands, the gap is recorded.
        await ctx.event("error", "Balance unavailable", str(exc))
        await record(position_id, row["quantity_raw"], [{"fraction": 1.0, "amount_raw": row["quantity_raw"], "ok": False, "error": f"balance unavailable: {exc}"}])
        await ctx.finish_step("0")
        return {"answer": "balance unavailable; last known quantity kept", "next_run_at": next_at}
    await ctx.finish_step("0")
    if current is None:
        await ctx.step("1")
        await _update(position_id, quantity_raw=0)
        await record(position_id, 0, [])
        await ctx.finish_step("1")
        return {"answer": "the wallet no longer holds the token", "next_run_at": next_at}
    await ctx.step("1")
    rows = await quote_exit(row["mint"], current["quantity_raw"])
    await record(position_id, current["quantity_raw"], rows)
    await _update(position_id, quantity_raw=current["quantity_raw"], decimals=current["decimals"])
    await ctx.finish_step("1")
    await ctx.step("2")
    row = await _rebaseline_if_needed(row, current, rows)
    alert = await _maybe_alert(row, rows)
    await ctx.finish_step("2")
    return {"answer": "recorded", "quotes": rows, "alerted": bool(alert), "next_run_at": next_at}


QUANTITY_CHANGE_PCT = 1.0


async def _rebaseline_if_needed(row: dict, current: dict, rows: list[dict]) -> dict:
    """A baseline compares like with like. No baseline yet (the entry quote
    failed): the first valid full-exit quote becomes it. The position size
    changed by more than QUANTITY_CHANGE_PCT since the baseline: the quote
    for the new size becomes the baseline and no alert fires for the size
    change (review, 2026-09-22: halving the position read as a 50% fall)."""
    entry = row.get("entry") or {}
    full = full_exit(rows)
    if full is None:
        return row
    base_qty = int(entry.get("quantity_raw") or row.get("quantity_raw") or 0)
    changed = base_qty and abs(current["quantity_raw"] - base_qty) / base_qty * 100 > QUANTITY_CHANGE_PCT
    if entry.get("baseline") and not changed:
        return row
    new_entry = {"at": _now().isoformat(), "rows": rows, "baseline": True, "quantity_raw": current["quantity_raw"],
                 "reason": "first valid quote" if not entry.get("baseline") else f"position changed from {base_qty} to {current['quantity_raw']}"}
    await _update(row["id"], entry=new_entry, last_alert=None)
    return {**row, "entry": new_entry, "last_alert": None}


async def _maybe_alert(row: dict, rows: list[dict]) -> dict | None:
    """The deterioration alert: the full-exit quote fell by more than the
    threshold against the entry quote or the last alert's quote, no more
    than once per cooldown. Both quotes travel with the alert."""
    entry = row.get("entry") or {}
    if not entry.get("baseline"):
        return None                                                    # pending: nothing valid to compare against yet
    baseline = (row.get("last_alert") or {}).get("quote") or full_exit(entry.get("rows") or [])
    if baseline is None:
        return None
    last_at = (row.get("last_alert") or {}).get("at")
    if last_at and (_now() - datetime.fromisoformat(last_at)).total_seconds() < settings.exit_alert_cooldown_hours * 3600:
        return None
    symbol = row.get("symbol") or baseline.get("symbol") or row["mint"][:6]
    baseline_at = last_at or entry.get("at") or ""
    now_full = full_exit(rows)
    full_row = next((r for r in rows if r.get("fraction") == 1.0), None)
    if now_full is None:
        # The route is gone: a full exit that was quoted cannot be quoted now.
        # That is the exit-risk alert in its plainest form.
        reason = (full_row or {}).get("error") or "no quote"
        if reason != "no route":
            return None                                                # unavailable data is not a market fact
        title = f"Exit for {symbol}: no route"
        body = (f"A full exit of your {symbol} position was quoted at {_usd(baseline['quoted_usdc'])} ({baseline_at[:16].replace('T', ' ')} UTC) and "
                f"cannot be quoted now: {reason}. Smaller sizes: " +
                (", ".join(f"{int(r['fraction'] * 100)}% {_usd(r['quoted_usdc'])}" for r in rows if r.get('ok')) or "none quoted") +
                ". This compares Jupiter quotes for your exact size; it is not a sell instruction.")
        alert = {"at": _now().isoformat(), "quote": None, "baseline": baseline, "drop_pct": None, "kind": "no_route", "reason": reason}
    else:
        drop = deterioration_pct(baseline, now_full)
        if drop is None or drop < settings.exit_alert_drop_pct:
            return None
        title = f"Exit for {symbol} deteriorated {drop:.0f}%"
        body = (f"A full exit of your {symbol} position is now quoted at {_usd(now_full['quoted_usdc'])}, down {drop:.1f}% from {_usd(baseline['quoted_usdc'])} "
                f"({baseline_at[:16].replace('T', ' ')} UTC). Price impact {now_full['price_impact_pct']:.2f}% now vs {baseline.get('price_impact_pct', 0):.2f}% then; "
                f"route {' → '.join(x for x in now_full.get('route') or [] if x) or 'unknown'}. Marked value {_usd(now_full.get('marked_value_usd'))}. "
                "This compares two Jupiter quotes for your exact size; it is not a price alert and not a sell instruction.")
        alert = {"at": _now().isoformat(), "quote": now_full, "baseline": baseline, "drop_pct": round(drop, 2), "kind": "deterioration"}
    # One occurrence per baseline: a retry after a failed checkpoint notifies
    # with the same key and the inbox keeps one (review, 2026-09-22).
    occurrence = f"{row['id']}:{baseline.get('quoted_at') or baseline_at}:{alert['kind']}"
    if row.get("user_id"):
        await tasks.notify(row["user_id"], title, body, kind="exit_alert", task_id=row["id"], occurrence=occurrence)
    await _update(row["id"], last_alert=alert)
    return alert


jobs.register(KIND, monitor, version="2026-09-22.1", settings_keys=("exit_monitor_interval_minutes", "exit_alert_drop_pct", "exit_alert_cooldown_hours"))
