"""The holder snapshot ledger: what a meme token's structure looked like, when.

Mobula answers "what is the holder structure NOW". It has no yesterday. So
today Orbit can say whether a token is clean now, not whether it was clean
when a buyer had to decide, and any "buy when top-10 < 20% and LP locked"
idea could only be tested with today's state on past prices -- lookahead
that makes a backtest look good and mean nothing (ai-hedge-fund discussion,
2026-09-20; the user chose to start recording early because history only
exists from the day it begins).

This module records, on a schedule and in the background, one row per
tracked token per moment: top-10 / top-50 concentration, the largest
positions with Mobula's labels, the share held by dev / sniper / bundler /
insider labelled wallets, LP burned / locked / unlocked for the main pool,
the contract switches, and price, liquidity and market cap at that moment.

What is tracked: every token a user deep-dived (14 days, extended on each
ask), and every new or bonding launch Pulse lists on the meme chains (3
days). Cadence: every `holder_snapshot_fresh_minutes` while a token is under
a day old, then every `holder_snapshot_interval_minutes`. Each snapshot is
three Mobula calls through the shared budgeted door (app/mobula_client.py),
so the worker never competes with a user's turn for more than its slice.

What it enables, in order of arrival: "top-10 went from 41% to 23% in 12
hours" in a deep-dive today (the `history` dimension); alerts as diffs
between rows; reflection with structural evidence; and, once the ledger has
depth, honest backtests of state-based meme signals that read only rows
taken before the decision moment.

Storage: two tables when Postgres is configured, bounded in-memory dicts
otherwise (tests, keyless deployments).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

from app import mobula_client
from app.db import apply_schema, get_pg_pool
from app.mobula_security import _CHAIN_NAMES
from app.settings import settings
from app.signals import Subject

logger = logging.getLogger(__name__)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tracked_tokens (
    subject_key TEXT PRIMARY KEY,
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    symbol TEXT,
    source TEXT NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_asked TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    launched_at TIMESTAMPTZ,
    until TIMESTAMPTZ NOT NULL,
    last_snapshot TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS holder_snapshots (
    id UUID PRIMARY KEY,
    subject_key TEXT NOT NULL,
    taken_at TIMESTAMPTZ NOT NULL,
    top10_pct REAL,
    top50_pct REAL,
    holders_count INTEGER,
    dev_pct REAL,
    sniper_pct REAL,
    bundler_pct REAL,
    insider_pct REAL,
    lp_burned_pct REAL,
    lp_locked_pct REAL,
    lp_unlocked_pct REAL,
    price_usd DOUBLE PRECISION,
    liquidity_usd DOUBLE PRECISION,
    market_cap_usd DOUBLE PRECISION,
    top_holders JSONB NOT NULL,
    flags JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS holder_snapshots_subject_idx ON holder_snapshots (subject_key, taken_at DESC);
"""
_schema_ready = False
_tracked: dict[str, dict] = {}
_rows: dict[str, list[dict]] = {}
_MEMORY_ROWS_PER_SUBJECT = 500
_lock = threading.Lock()

TOP_HOLDERS_KEPT = 20
FRESH_HOURS = 24.0
DEEP_DIVE_DAYS = 14
LAUNCH_DAYS = 3
# Label words that mark a wallet as one of the risk cohorts Pulse reports.
_COHORTS = {"dev": ("dev", "deployer", "creator", "team"), "sniper": ("sniper",), "bundler": ("bundler", "bundle"), "insider": ("insider",)}


def enabled() -> bool:
    return bool(settings.mobula_api_key) and bool(settings.holder_snapshots_enabled)


def reset_for_test() -> None:
    global _schema_ready
    with _lock:
        _tracked.clear()
        _rows.clear()
    _schema_ready = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def _pool():
    global _schema_ready
    try:
        pool = await get_pg_pool()
    except Exception:
        return None
    if pool is not None and not _schema_ready:
        try:
            await apply_schema(pool, _TABLE_SQL)
            _schema_ready = True
        except Exception:
            logger.warning("holder_snapshots: schema not ready; using memory store", exc_info=True)
            return None
    return pool


# ---------------------------------------------------------------------------
# tracking
# ---------------------------------------------------------------------------

async def track(subject: Subject, source: str, *, days: float, launched_at: datetime | None = None) -> None:
    """Start (or extend) recording `subject`. `days` runs from now; an
    existing entry keeps the earlier of its launch times and the later of
    its expiries, so a deep-dive on a fresh launch never shortens it."""
    if subject.kind != "token" or not subject.chain:
        return
    now = _now()
    until = now + timedelta(days=days)
    pool = await _pool()
    if pool is not None:
        await pool.execute(
            "INSERT INTO tracked_tokens (subject_key, chain, address, symbol, source, first_seen, last_asked, launched_at, until) "
            "VALUES ($1, $2, $3, $4, $5, $6, $6, $7, $8) "
            "ON CONFLICT (subject_key) DO UPDATE SET last_asked = EXCLUDED.last_asked, symbol = COALESCE(EXCLUDED.symbol, tracked_tokens.symbol), "
            "launched_at = COALESCE(tracked_tokens.launched_at, EXCLUDED.launched_at), until = GREATEST(tracked_tokens.until, EXCLUDED.until)",
            subject.key, subject.chain.lower(), subject.id, subject.symbol, source, now, launched_at, until)
        return
    with _lock:
        row = _tracked.get(subject.key)
        if row is None:
            _tracked[subject.key] = {"subject_key": subject.key, "chain": subject.chain.lower(), "address": subject.id, "symbol": subject.symbol,
                                     "source": source, "first_seen": now, "last_asked": now, "launched_at": launched_at, "until": until, "last_snapshot": None}
        else:
            row["last_asked"] = now
            row["symbol"] = subject.symbol or row.get("symbol")
            row["launched_at"] = row.get("launched_at") or launched_at
            row["until"] = max(row["until"], until)


async def is_tracked(subject_key: str) -> bool:
    pool = await _pool()
    if pool is not None:
        return bool(await pool.fetchval("SELECT 1 FROM tracked_tokens WHERE subject_key = $1", subject_key))
    with _lock:
        return subject_key in _tracked


async def tracked(now: datetime | None = None) -> list[dict]:
    """Every token still inside its tracking window."""
    now = now or _now()
    pool = await _pool()
    if pool is not None:
        rows = await pool.fetch("SELECT * FROM tracked_tokens WHERE until > $1 ORDER BY last_asked DESC", now)
        return [dict(r) for r in rows]
    with _lock:
        return [dict(r) for r in _tracked.values() if r["until"] > now]


def cadence_minutes(row: dict, now: datetime | None = None) -> float:
    """Dense while the token is under a day old, then the base interval."""
    now = now or _now()
    launched = _dt(row.get("launched_at")) or _dt(row.get("first_seen"))
    if launched and (now - launched) < timedelta(hours=FRESH_HOURS):
        return float(settings.holder_snapshot_fresh_minutes)
    return float(settings.holder_snapshot_interval_minutes)


def due(rows: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or _now()
    out = []
    for row in rows:
        last = _dt(row.get("last_snapshot"))
        if last is None or (now - last) >= timedelta(minutes=cadence_minutes(row, now)):
            out.append(row)
    # Fresh launches first: their history is the one that cannot be recovered.
    out.sort(key=lambda r: cadence_minutes(r, now))
    return out


async def _mark_taken(subject_key: str, when: datetime) -> None:
    pool = await _pool()
    if pool is not None:
        await pool.execute("UPDATE tracked_tokens SET last_snapshot = $2 WHERE subject_key = $1", subject_key, when)
        return
    with _lock:
        if subject_key in _tracked:
            _tracked[subject_key]["last_snapshot"] = when


# ---------------------------------------------------------------------------
# taking a snapshot (synchronous: three Mobula calls)
# ---------------------------------------------------------------------------

def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_POOL_WORDS = ("liquidity", "pool", "amm", "bonding", "curve", "raydium", "pump", "meteora", "orca")


def _is_pool(row: dict) -> bool:
    return any(any(w in label for w in _POOL_WORDS) for label in _labels(row))


def _positive(value) -> float | None:
    """A percentage that is None or zero is 'not computed' on a fresh token."""
    number = _num(value)
    return number if number is not None and number > 0 else None


def _labels(row: dict) -> list[str]:
    out = []
    for label in row.get("labels") or []:
        text = label.get("name") if isinstance(label, dict) else label
        if text:
            out.append(str(text).lower())
    return out


def _mobula_chain(chain: str) -> str:
    return _CHAIN_NAMES.get(chain.lower(), chain)


def take(chain: str, address: str) -> dict:
    """One row of the ledger for (chain, address), from live Mobula data.
    Every part is optional: a token Mobula has not indexed for holders still
    gets its price and pool state recorded, and the row says what is missing
    (`flags.missing`) rather than inventing zeros."""
    mchain = _mobula_chain(chain)
    missing: list[str] = []      # Mobula answered and had nothing
    failed: list[str] = []       # the call did not happen or did not succeed

    holders: list[dict] = []
    try:
        holders = [r for r in (mobula_client.get(2, "/token/holder-positions", {"address": address, "blockchain": mchain, "limit": 50}, background=True) or []) if isinstance(r, dict)]
    except Exception:
        failed.append("holders")
        logger.info("holder_snapshots: holder positions failed for %s", address, exc_info=True)
    if not holders:
        missing.append("holders")
    holders.sort(key=lambda r: _num(r.get("percentageOfTotalSupply"), 0.0), reverse=True)
    cohorts = {name: 0.0 for name in _COHORTS}
    for row in holders:
        labels = _labels(row)
        for name, words in _COHORTS.items():
            if any(any(w in label for w in words) for label in labels):
                cohorts[name] += _num(row.get("percentageOfTotalSupply"), 0.0)
    top_holders = [{"address": str(r.get("walletAddress") or ""), "pct": round(_num(r.get("percentageOfTotalSupply"), 0.0), 4),
                    "labels": _labels(r)[:4]} for r in holders[:TOP_HOLDERS_KEPT]]

    security: dict = {}
    try:
        data = mobula_client.get(2, "/token/security", {"blockchain": mchain, "address": address}, background=True)
        security = data if isinstance(data, dict) else {}
    except Exception:
        failed.append("security")
        logger.info("holder_snapshots: security failed for %s", address, exc_info=True)
    if not security:
        missing.append("security")
    pools = [p for p in (security.get("liquidityAnalysis") or []) if isinstance(p, dict)]
    main_pool = pools[0] if pools else {}
    if not pools:
        missing.append("liquidity")

    market: dict = {}
    try:
        data = mobula_client.get(1, "/market/data", {"asset": address, "blockchain": mchain}, background=True)
        market = (data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data) or {}
        if not isinstance(market, dict):
            market = {}
    except Exception:
        failed.append("market")
        logger.info("holder_snapshots: market data failed for %s", address, exc_info=True)
    if not market:
        missing.append("market")

    # Security's concentration figures read 0 on a token minutes old; when
    # they say nothing, sum the largest positions ourselves, leaving out the
    # pool and curve wallets that are not holders in the sense that matters.
    wallets = [r for r in holders if not _is_pool(r)]
    top10 = _positive(security.get("top10HoldingsPercentage"))
    if top10 is None and wallets:
        top10 = sum(_num(r.get("percentageOfTotalSupply"), 0.0) for r in wallets[:10])
    top50 = _positive(security.get("top50HoldingsPercentage"))
    if top50 is None and len(wallets) >= 50:
        top50 = sum(_num(r.get("percentageOfTotalSupply"), 0.0) for r in wallets[:50])

    return {
        "id": uuid.uuid4().hex,
        "taken_at": _now(),
        "top10_pct": top10, "top50_pct": top50,
        "holders_count": None,
        "dev_pct": cohorts["dev"] if holders else None, "sniper_pct": cohorts["sniper"] if holders else None,
        "bundler_pct": cohorts["bundler"] if holders else None, "insider_pct": cohorts["insider"] if holders else None,
        "lp_burned_pct": _num(main_pool.get("burnedPercentage")), "lp_locked_pct": _num(main_pool.get("lockedPercentage")),
        "lp_unlocked_pct": _num(main_pool.get("unlockedPercentage")),
        "price_usd": _num(market.get("price")), "liquidity_usd": _num(market.get("liquidity")), "market_cap_usd": _num(market.get("market_cap")),
        "top_holders": top_holders,
        "flags": {"mintable": security.get("isMintable"), "freezable": security.get("isFreezable"), "honeypot": security.get("isHoneypot"),
                  "renounced": security.get("renounced"), "pools": len(pools), "missing": missing, "failed": failed},
    }


async def store(subject_key: str, row: dict) -> None:
    pool = await _pool()
    if pool is not None:
        await pool.execute(
            "INSERT INTO holder_snapshots (id, subject_key, taken_at, top10_pct, top50_pct, holders_count, dev_pct, sniper_pct, bundler_pct, insider_pct, "
            "lp_burned_pct, lp_locked_pct, lp_unlocked_pct, price_usd, liquidity_usd, market_cap_usd, top_holders, flags) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17::jsonb, $18::jsonb)",
            uuid.UUID(row["id"]), subject_key, row["taken_at"], row["top10_pct"], row["top50_pct"], row["holders_count"], row["dev_pct"], row["sniper_pct"],
            row["bundler_pct"], row["insider_pct"], row["lp_burned_pct"], row["lp_locked_pct"], row["lp_unlocked_pct"], row["price_usd"],
            row["liquidity_usd"], row["market_cap_usd"], json.dumps(row["top_holders"]), json.dumps(row["flags"]))
    else:
        with _lock:
            rows = _rows.setdefault(subject_key, [])
            rows.append(dict(row, subject_key=subject_key))
            del rows[:-_MEMORY_ROWS_PER_SUBJECT]
    await _mark_taken(subject_key, row["taken_at"])


_background: set[asyncio.Task] = set()


def schedule(subject: Subject, source: str, days: float, *, take_now: bool) -> None:
    """Track `subject` and, when asked, take its first row -- off the turn's
    critical path (review, 2026-09-22: a deep-dive waited on three Mobula
    calls after its answer was already written). Never raises."""
    async def run():
        try:
            await track(subject, source, days=days)
            if take_now:
                await snapshot(subject)
        except Exception:
            logger.warning("holder_snapshots: background tracking failed for %s", subject.key, exc_info=True)
    try:
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


async def drain_background(timeout: float = 15.0) -> int:
    pending = [t for t in _background if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    return len(pending)


async def snapshot(subject: Subject) -> dict | None:
    """Take and store one row for `subject` now. None when disabled."""
    if not enabled() or subject.kind != "token" or not subject.chain:
        return None
    row = await asyncio.to_thread(take, subject.chain, subject.id)
    await store(subject.key, row)
    return row


# ---------------------------------------------------------------------------
# reading the ledger
# ---------------------------------------------------------------------------

def _from_db(r) -> dict:
    row = dict(r)
    for key in ("top_holders", "flags"):
        if isinstance(row.get(key), str):
            row[key] = json.loads(row[key])
    row["id"] = str(row["id"]).replace("-", "")
    return row


async def history(subject_key: str, limit: int = 200, since: datetime | None = None) -> list[dict]:
    """The newest `limit` rows for one subject, returned oldest first. The cut
    is taken from the newest end: an ascending LIMIT returned the oldest 200
    forever, so the history card froze after about three days of rows and
    the dark-token check compared against stale state (review, 2026-09-22)."""
    pool = await _pool()
    if pool is not None:
        if since is not None:
            rows = await pool.fetch("SELECT *, top_holders::text AS top_holders, flags::text AS flags FROM holder_snapshots WHERE subject_key = $1 AND taken_at >= $2 "
                                    "ORDER BY taken_at DESC LIMIT $3", subject_key, since, limit)
        else:
            rows = await pool.fetch("SELECT *, top_holders::text AS top_holders, flags::text AS flags FROM holder_snapshots WHERE subject_key = $1 "
                                    "ORDER BY taken_at DESC LIMIT $2", subject_key, limit)
        return [_from_db(r) for r in reversed(rows)]
    with _lock:
        rows = [dict(r) for r in _rows.get(subject_key, []) if since is None or r["taken_at"] >= since]
    return rows[-limit:] if limit else rows


async def as_of(subject_key: str, moment: datetime) -> dict | None:
    """The last row taken at or before `moment` -- the point-in-time read a
    backtest is allowed to use. None when the ledger has nothing that old."""
    pool = await _pool()
    if pool is not None:
        r = await pool.fetchrow("SELECT *, top_holders::text AS top_holders, flags::text AS flags FROM holder_snapshots WHERE subject_key = $1 AND taken_at <= $2 "
                                "ORDER BY taken_at DESC LIMIT 1", subject_key, moment)
        return _from_db(r) if r else None
    with _lock:
        rows = [r for r in _rows.get(subject_key, []) if r["taken_at"] <= moment]
    return dict(rows[-1]) if rows else None


_FIELDS = (
    ("top10_pct", "Top-10 share", "pct"), ("top50_pct", "Top-50 share", "pct"), ("dev_pct", "Dev-labelled wallets", "pct"),
    ("sniper_pct", "Sniper-labelled wallets", "pct"), ("bundler_pct", "Bundler-labelled wallets", "pct"), ("insider_pct", "Insider-labelled wallets", "pct"),
    ("lp_locked_pct", "LP locked", "pct"), ("lp_burned_pct", "LP burned", "pct"), ("lp_unlocked_pct", "LP unlocked", "pct"),
    ("price_usd", "Price", "usd"), ("liquidity_usd", "Liquidity", "usd"), ("market_cap_usd", "Market cap", "usd"),
)


def _fmt(value, kind: str) -> str:
    if value is None:
        return "—"
    if kind == "pct":
        return f"{value:.2f}%"
    if abs(value) >= 1e6:
        return f"${value / 1e6:,.2f}M"
    if abs(value) >= 1:
        return f"${value:,.2f}"
    return f"${value:.6g}"


def diff(older: dict, newer: dict, *, min_pct_points: float = 0.5, min_ratio: float = 0.05) -> list[str]:
    """Human lines for what changed between two rows: a percentage field that
    moved by at least `min_pct_points` points, a dollar field by at least
    `min_ratio` of itself, and any top-20 wallet that entered or left. This is
    what an alert will read; below the thresholds nothing is said."""
    lines: list[str] = []
    for key, label, kind in _FIELDS:
        a, b = older.get(key), newer.get(key)
        if a is None or b is None:
            continue
        if kind == "pct" and abs(b - a) >= min_pct_points:
            lines.append(f"{label} {_fmt(a, kind)} → {_fmt(b, kind)}")
        elif kind == "usd" and a and abs(b - a) / abs(a) >= min_ratio:
            lines.append(f"{label} {_fmt(a, kind)} → {_fmt(b, kind)} ({(b - a) / a * 100:+.1f}%)")
    before = {h["address"]: h for h in older.get("top_holders") or []}
    after = {h["address"]: h for h in newer.get("top_holders") or []}
    # A row with no holder list (a Pulse row, or holders Mobula did not
    # return) says nothing about who left or entered.
    left = [a for a in before if a not in after] if before and after else []
    entered = [a for a in after if a not in before] if before and after else []
    if left:
        lines.append(f"{len(left)} wallet(s) left the top {TOP_HOLDERS_KEPT}: " + ", ".join(f"`{a[:4]}…{a[-4:]}`" for a in left[:3]) + (" …" if len(left) > 3 else ""))
    if entered:
        lines.append(f"{len(entered)} wallet(s) entered the top {TOP_HOLDERS_KEPT}: " + ", ".join(f"`{a[:4]}…{a[-4:]}`" for a in entered[:3]) + (" …" if len(entered) > 3 else ""))
    for key, label in (("mintable", "Mintable"), ("honeypot", "Honeypot"), ("renounced", "Ownership renounced")):
        a, b = (older.get("flags") or {}).get(key), (newer.get("flags") or {}).get(key)
        if a is not None and b is not None and a != b:
            lines.append(f"{label}: {a} → {b}")
    return lines


def _age(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600.0
    if hours < 1:
        return f"{delta.total_seconds() / 60:.0f} min"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.0f} days"


async def history_card(subject: Subject) -> str | None:
    """The `history` dimension for a deep-dive: how the structure moved since
    Orbit first recorded this token. None with fewer than two rows -- a single
    row is a present state the other dimensions already show, not history."""
    rows = await history(subject.key)
    if len(rows) < 2:
        return None
    first, last = rows[0], rows[-1]
    span = _age(_dt(last["taken_at"]) - _dt(first["taken_at"]))
    lines = [f"# Holder history — {subject.label()}",
             f"**Provider**: Orbit snapshot ledger (Mobula data) · **Rows**: {len(rows)} · **Span**: {span} · "
             f"**First**: {_dt(first['taken_at']).strftime('%Y-%m-%d %H:%M UTC')} · **Latest**: {_dt(last['taken_at']).strftime('%Y-%m-%d %H:%M UTC')}", ""]
    changes = diff(first, last)
    if changes:
        lines += [f"Since first recorded ({span} ago):"] + [f"- {c}" for c in changes]
    else:
        lines.append(f"No material change in concentration, labelled cohorts, LP state, price or liquidity over {span}.")
    # The last step, when it differs from the whole-span read.
    if len(rows) >= 3:
        recent = diff(rows[-2], last)
        if recent and recent != changes:
            lines += ["", f"Latest step ({_age(_dt(last['taken_at']) - _dt(rows[-2]['taken_at']))}):"] + [f"- {c}" for c in recent]
    missing = (last.get("flags") or {}).get("missing") or []
    if missing:
        lines += ["", f"Latest row is missing: {', '.join(missing)} (Mobula returned nothing for those parts)."]
    lines += ["", "Rows are Orbit's own periodic snapshots; nothing before the first row is known. Labels are Mobula's classifications, evidence not proof."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------

def launch_address(item: dict) -> str | None:
    """The launched token's address in a Pulse row. `pair.baseToken` is a
    POINTER ("token0" / "token1") to the side that is the token, not an
    object (probed live 2026-09-21: a BNB pair read token1-first picked
    Binance-peg DOGE as the launch). Then the side whose symbol is the row's
    own tokenSymbol; only then whichever side is not a quote asset."""
    from app import mobula_meme

    pair = item.get("pair") or {}
    if not isinstance(pair, dict):
        return None
    sides = {name: pair.get(name) for name in ("token0", "token1") if isinstance(pair.get(name), dict) and pair[name].get("address")}
    base = pair.get("baseToken")
    if isinstance(base, str) and base in sides:
        return str(sides[base]["address"])
    if isinstance(base, dict) and base.get("address"):
        return str(base["address"])
    symbol = str(item.get("tokenSymbol") or item.get("symbol") or "").strip().upper()
    if symbol:
        for token in sides.values():
            if str(token.get("symbol") or "").strip().upper() == symbol:
                return str(token["address"])
    for name in ("token1", "token0"):
        token = sides.get(name)
        if token and str(token.get("symbol") or "").upper() not in mobula_meme._QUOTES:
            return str(token["address"])
    return None


def launch_symbol(item: dict, address: str) -> str | None:
    """The symbol of the side `launch_address` chose -- never a symbol from
    one side with the address of the other (live: a GRND/HOMO pair was
    tracked as HOMO at GRND's address)."""
    pair = item.get("pair") if isinstance(item.get("pair"), dict) else {}
    for name in ("token0", "token1"):
        token = pair.get(name)
        if isinstance(token, dict) and str(token.get("address") or "") == address and token.get("symbol"):
            return str(token["symbol"])
    symbol = item.get("tokenSymbol") or item.get("symbol")
    return str(symbol) if symbol else None


# A launch row's holder count above this is not the launch's (a pointer to the
# wrong side, or a program-level figure); market cap is only kept once a few
# wallets hold the token, the same rule the launch card applies.
_LAUNCH_MAX_HOLDERS = 100_000
_LAUNCH_MIN_HOLDERS_FOR_MCAP = 5


def _launch_holders(item: dict) -> int | None:
    count = int(_num(item.get("holders_count"), 0) or 0)
    return count if 0 < count <= _LAUNCH_MAX_HOLDERS else None


def row_from_pulse(item: dict) -> dict:
    """A ledger row straight from a Pulse item -- Pulse already carries the
    cohort shares, top-10, holder count and price for a launch, so the first
    rows of a new token cost no extra calls. LP state is not in Pulse; the
    scheduled snapshot fills it in later."""
    pair = item.get("pair") if isinstance(item.get("pair"), dict) else {}
    return {
        "id": uuid.uuid4().hex, "taken_at": _now(),
        "top10_pct": _positive(item.get("top10HoldingsPercentage")), "top50_pct": _positive(item.get("top50HoldingsPercentage")),
        "holders_count": _launch_holders(item),
        "dev_pct": _num(item.get("devHoldingsPercentage")), "sniper_pct": _num(item.get("snipersHoldingsPercentage")),
        "bundler_pct": _num(item.get("bundlersHoldingsPercentage")), "insider_pct": _num(item.get("insidersHoldingsPercentage")),
        "lp_burned_pct": None, "lp_locked_pct": None, "lp_unlocked_pct": None,
        # Pulse's pair liquidity is one side of the pool; the market endpoint
        # reports both. Not the same number, so it is not recorded here.
        "price_usd": _num(item.get("price") or item.get("latest_price")), "liquidity_usd": None,
        "market_cap_usd": _num(item.get("market_cap")) if (_launch_holders(item) or 0) >= _LAUNCH_MIN_HOLDERS_FOR_MCAP else None,
        "top_holders": [],
        "flags": {"source": "pulse", "deployer": item.get("deployer"), "bonded": item.get("bonded"), "bonding_pct": _num(item.get("bondingPercentage")),
                  "launchpad": item.get("source") or item.get("sourceFactory"), "missing": ["liquidity", "top_holders"]},
    }


async def discover_launches() -> int:
    """Put every new or bonding launch Pulse lists on the meme chains under
    tracking for LAUNCH_DAYS, and record the Pulse row itself as the token's
    first ledger row. Returns how many launches were seen."""
    from app import mobula_meme

    seen = 0
    for chain in [c.strip().lower() for c in settings.holder_snapshot_pulse_chains.split(",") if c.strip()]:
        chain_id = mobula_meme.PULSE_CHAINS.get(chain)
        if not chain_id:
            continue
        try:
            data = await asyncio.to_thread(lambda: mobula_client.get(2, "/pulse", {"chainId": chain_id, "limit": 20}, background=True))
        except Exception:
            logger.info("holder_snapshots: pulse failed for %s", chain, exc_info=True)
            continue
        if not isinstance(data, dict):
            continue
        for key in ("new", "bonding"):
            items = ((data.get(key) or {}).get("data") if isinstance(data.get(key), dict) else data.get(key)) or []
            for item in [i for i in items if isinstance(i, dict)]:
                address = launch_address(item)
                if not address:
                    continue
                # A launch nobody holds yet is noise: most die within the
                # hour, and each one tracked costs three calls per snapshot.
                if _num(item.get("holders_count"), 0.0) < settings.holder_snapshot_min_holders:
                    continue
                subject = Subject(kind="token", id=address, chain="bnb" if chain == "bsc" else chain, symbol=launch_symbol(item, address))
                launched = _dt(item.get("created_at") or item.get("createdAt"))
                first_sight = not await is_tracked(subject.key)
                await track(subject, "pulse", days=LAUNCH_DAYS, launched_at=launched)
                if first_sight:
                    # The Pulse row is the token's first ledger row, once; the
                    # scheduled snapshots take it from here.
                    await store(subject.key, row_from_pulse(item))
                seen += 1
    return seen


async def tick(now: datetime | None = None) -> int:
    """One pass: snapshot every due token, at most `holder_snapshot_max_per_tick`."""
    now = now or _now()
    rows = due(await tracked(now), now)[: max(1, settings.holder_snapshot_max_per_tick)]
    taken = 0
    for row in rows:
        # The ledger is background work: it runs only while Mobula has room
        # to spare and stops the moment a user's turn might need the budget.
        if not mobula_client.background_ok():
            logger.info("holder_snapshots: Mobula budget reserved for users; %d due token(s) wait for the next tick", len(rows) - taken)
            break
        try:
            snap = await asyncio.to_thread(take, row["chain"], row["address"])
            if blank(snap):
                # Nothing came back at all (budget refused, or Mobula down):
                # not a fact about the token, so no row and no "taken" mark.
                continue
            await store(row["subject_key"], snap)
            taken += 1
            if dark(snap):
                previous = await history(row["subject_key"], limit=50)
                if len(previous) >= 2 and dark(previous[-2]):
                    await untrack(row["subject_key"])
                    logger.info("holder_snapshots: %s went dark twice; tracking stopped", row["subject_key"])
        except Exception:
            logger.warning("holder_snapshots: snapshot failed for %s", row["subject_key"], exc_info=True)
    return taken


def blank(row: dict) -> bool:
    """Every call failed or was refused: the snapshot did not happen. An
    answered-but-empty row is different -- that is a fact about the token."""
    failed = set((row.get("flags") or {}).get("failed") or [])
    return {"holders", "security", "market"} <= failed


def dark(row: dict) -> bool:
    """Nothing known: no holders and no market. Two dark rows in a row mean a
    launch that died (or a token Mobula does not index); either way, stop
    spending calls on it. A Pulse-sourced row is never dark: it has no
    holder list by construction."""
    missing = set((row.get("flags") or {}).get("missing") or [])
    return (row.get("flags") or {}).get("source") != "pulse" and {"holders", "market"} <= missing


async def untrack(subject_key: str) -> None:
    now = _now()
    pool = await _pool()
    if pool is not None:
        await pool.execute("UPDATE tracked_tokens SET until = $2 WHERE subject_key = $1", subject_key, now)
        return
    with _lock:
        if subject_key in _tracked:
            _tracked[subject_key]["until"] = now


_LEADER_KEY = "holder_snapshots:leader"
_LEADER_TTL = 180
_leader_id = uuid.uuid4().hex


async def is_leader() -> bool:
    """Only one process records the ledger. With Redis configured, the lease
    is a key set NX with a TTL and renewed by its holder every tick, so two
    servers sharing one Mobula key (or a reloader's overlapping processes)
    never both run; without Redis a single process is assumed."""
    from app.db import get_redis

    try:
        client = await get_redis()
    except Exception:
        client = None
    if client is None:
        return True
    try:
        if await client.set(_LEADER_KEY, _leader_id, nx=True, ex=_LEADER_TTL):
            return True
        holder = await client.get(_LEADER_KEY)
        if holder == _leader_id:
            await client.expire(_LEADER_KEY, _LEADER_TTL)
            return True
        return False
    except Exception:
        logger.info("holder_snapshots: leader check failed; running", exc_info=True)
        return True


async def worker() -> None:
    """Background loop: discover launches every hour, snapshot due tokens every tick."""
    if not enabled():
        return
    last_discovery: datetime | None = None
    while True:
        try:
            if not await is_leader():
                await asyncio.sleep(max(15, settings.holder_snapshot_tick_seconds))
                continue
            now = _now()
            if last_discovery is None or (now - last_discovery) >= timedelta(hours=1):
                await discover_launches()
                last_discovery = now
            taken = await tick(now)
            if taken:
                logger.info("holder_snapshots: recorded %d snapshot(s)", taken)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("holder_snapshots: tick failed", exc_info=True)
        await asyncio.sleep(max(15, settings.holder_snapshot_tick_seconds))
