"""The Tequity tick ledger: the feed's snapshots, kept.

The feed only ever says "now" (app/tequity.py). Anything that needs a
"then" -- how a pair moved since this morning, which tokenized stocks moved
most this week, an alert when a pair jumps between two ticks, volume
leaders over seven days -- needs snapshots stored, so one process records
them (user decision, 2026-09-23: "we must store snapshots first").

One narrow row per pair per tick, not one JSON blob per snapshot: 858 pairs
every five minutes is about 250k rows a day, indexed by (venue, symbol,
taken_at), which the time-series reads below want; a 60 KB blob every three
seconds would be 1.7 GB a day of nothing anyone queries. Retention prunes
rows older than `tequity_retention_days`. Only the lease holder records
(Redis key set NX, renewed each tick), so two servers or a reloader's
overlapping processes never double-write.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from app import tequity
from app.db import apply_schema, get_pg_pool
from app.settings import settings

logger = logging.getLogger(__name__)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tequity_ticks (
    id UUID PRIMARY KEY,
    channel TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    base_asset TEXT,
    quote_asset TEXT,
    is_stock BOOLEAN NOT NULL DEFAULT FALSE,
    rank INTEGER,
    last_price DOUBLE PRECISION,
    change_pct DOUBLE PRECISION,
    quote_volume DOUBLE PRECISION,
    taken_at TIMESTAMPTZ NOT NULL,
    server_ts TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS tequity_ticks_pair_idx ON tequity_ticks (venue, symbol, taken_at DESC);
CREATE INDEX IF NOT EXISTS tequity_ticks_channel_time_idx ON tequity_ticks (channel, taken_at DESC);
"""

_schema_ready = False
_rows: list[dict] = []                       # memory mode (tests, no DATABASE_URL)
_lock = threading.Lock()
_MEMORY_ROWS = 20_000
_LEADER_KEY = "tequity_ledger:leader"


def _leader_ttl() -> int:
    """Longer than the recording interval, or the lease lapses between ticks
    and a second process can take it (review of 330bc651, 2026-09-23)."""
    return max(180, 2 * int(settings.tequity_record_interval_seconds or 0) + 60)
_leader_id = uuid.uuid4().hex
_last_pruned: datetime | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> bool:
    return tequity.enabled() and bool(settings.tequity_record_interval_seconds)


def reset_for_test() -> None:
    global _schema_ready, _last_pruned
    with _lock:
        _rows.clear()
    _schema_ready = False
    _last_pruned = None


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
            logger.warning("tequity_ledger: schema not ready; using memory store", exc_info=True)
            return None
    return pool


# ----------------------------------------------------------------------------
# recording
# ----------------------------------------------------------------------------

def _num(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def rows_from(channel: str, snap: dict, taken_at: datetime | None = None) -> list[dict]:
    """The tick rows a snapshot yields: one per pair, ranked for trending."""
    taken_at = taken_at or _now()
    server_ts = datetime.fromtimestamp(snap["server_ts"], timezone.utc) if snap.get("server_ts") else None
    data = snap.get("data") or {}
    if channel == tequity.TRENDING:
        items = [(i + 1, r) for i, r in enumerate(data.get("trending") or []) if isinstance(r, dict)]
        venue_of = lambda r: str(r.get("dex") or "?").lower()
    else:
        payload = data.get("data") or {}
        items = [(None, r) for r in payload.get("tokens") or [] if isinstance(r, dict)]
        fixed = str(payload.get("dex") or channel.rsplit(":", 1)[-1]).lower()
        venue_of = lambda r: fixed
    out = []
    for rank, r in items:
        symbol = str(r.get("symbol") or "").strip()
        if not symbol:
            continue
        out.append({"id": str(uuid.uuid4()), "channel": channel, "venue": venue_of(r), "symbol": symbol, "base_asset": r.get("base_asset"),
                    "quote_asset": r.get("quote_asset"), "is_stock": bool(r.get("is_stock")), "rank": rank, "last_price": _num(r.get("last_price")),
                    "change_pct": _num(r.get("price_change_percent")), "quote_volume": _num(r.get("quote_volume")), "taken_at": taken_at, "server_ts": server_ts})
    return out


async def store(rows: list[dict]) -> int:
    if not rows:
        return 0
    pool = await _pool()
    if pool is not None:
        await pool.executemany(
            "INSERT INTO tequity_ticks (id, channel, venue, symbol, base_asset, quote_asset, is_stock, rank, last_price, change_pct, quote_volume, taken_at, server_ts) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
            [(uuid.UUID(r["id"]), r["channel"], r["venue"], r["symbol"], r["base_asset"], r["quote_asset"], r["is_stock"], r["rank"], r["last_price"],
              r["change_pct"], r["quote_volume"], r["taken_at"], r["server_ts"]) for r in rows])
        return len(rows)
    with _lock:
        _rows.extend(rows)
        del _rows[:-_MEMORY_ROWS]
    return len(rows)


async def record(now: datetime | None = None) -> int:
    """One tick: every channel with a fresh snapshot becomes rows, stamped
    with the same taken_at so a tick can be read back as a whole."""
    now = now or _now()
    stored = 0
    guard = timedelta(seconds=max(30, int(settings.tequity_record_interval_seconds or 300)) / 2)
    for channel in (*tequity.MOVERS.values(), tequity.TRENDING):
        snap = tequity._snapshots.get(channel)
        if not snap or time.time() - snap["received_at"] > tequity.STALE_SECONDS:
            continue
        latest = await latest_tick_time(channel)
        if latest is not None and now - latest < guard:
            continue                                                       # this sample bucket is already recorded (a second recorder, a restart)
        stored += await store(rows_from(channel, snap, now))
    return stored


async def latest_tick_time(channel: str) -> datetime | None:
    pool = await _pool()
    if pool is not None:
        return await pool.fetchval("SELECT max(taken_at) FROM tequity_ticks WHERE channel = $1", channel)
    with _lock:
        times = [r["taken_at"] for r in _rows if r["channel"] == channel]
    return max(times) if times else None


async def prune(now: datetime | None = None) -> int:
    cutoff = (now or _now()) - timedelta(days=settings.tequity_retention_days)
    pool = await _pool()
    if pool is not None:
        result = await pool.execute("DELETE FROM tequity_ticks WHERE taken_at < $1", cutoff)
        try:
            return int(str(result).rsplit(" ", 1)[-1])
        except ValueError:
            return 0
    with _lock:
        before = len(_rows)
        _rows[:] = [r for r in _rows if r["taken_at"] >= cutoff]
        return before - len(_rows)


async def is_leader() -> bool:
    from app.db import get_redis

    try:
        client = await get_redis()
    except Exception:
        client = None
    if client is None:
        return True
    try:
        if await client.set(_LEADER_KEY, _leader_id, nx=True, ex=_leader_ttl()):
            return True
        # Owner-checked renewal in one step, so a lease that lapsed and was
        # taken by another process is never re-extended by the old holder.
        renewed = await client.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
            1, _LEADER_KEY, _leader_id, _leader_ttl())
        return bool(renewed)
    except Exception:
        # Redis is configured but unreachable: nobody can prove they lead, so
        # nobody records this tick (review of 330bc651: every process did).
        logger.info("tequity_ledger: leader check failed; skipping this tick", exc_info=True)
        return False


async def worker() -> None:
    """Record a tick every `tequity_record_interval_seconds` while holding the lease; prune daily."""
    global _last_pruned
    if not enabled():
        return
    interval = max(30, int(settings.tequity_record_interval_seconds))
    await asyncio.sleep(min(20, interval))                 # let the stream worker take its first snapshots
    while True:
        try:
            if await is_leader():
                stored = await record()
                if stored:
                    logger.info("tequity_ledger: recorded %d rows", stored)
                if _last_pruned is None or _now() - _last_pruned >= timedelta(hours=24):
                    removed = await prune()
                    _last_pruned = _now()
                    if removed:
                        logger.info("tequity_ledger: pruned %d rows older than %d days", removed, settings.tequity_retention_days)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("tequity_ledger: tick failed", exc_info=True)
        await asyncio.sleep(interval)


# ----------------------------------------------------------------------------
# reading
# ----------------------------------------------------------------------------

def _from_db(r) -> dict:
    d = dict(r)
    d["id"] = str(d["id"])
    return d


async def history(venue: str, symbol: str, since: datetime, until: datetime | None = None, limit: int = 5000) -> list[dict]:
    """One pair's ticks, oldest first."""
    until = until or _now()
    pool = await _pool()
    if pool is not None:
        rows = await pool.fetch("SELECT * FROM tequity_ticks WHERE venue = $1 AND symbol = $2 AND channel <> $5 AND taken_at BETWEEN $3 AND $4 ORDER BY taken_at ASC LIMIT $6",
                                venue, symbol, since, until, tequity.TRENDING, limit)
        return [_from_db(r) for r in rows]
    with _lock:
        return sorted([dict(r) for r in _rows if r["venue"] == venue and r["symbol"] == symbol and r["channel"] != tequity.TRENDING and since <= r["taken_at"] <= until],
                      key=lambda r: r["taken_at"])[:limit]


async def tick_at(channel: str, moment: datetime) -> list[dict]:
    """Every row of the last tick at or before `moment` for a channel."""
    pool = await _pool()
    if pool is not None:
        when = await pool.fetchval("SELECT max(taken_at) FROM tequity_ticks WHERE channel = $1 AND taken_at <= $2", channel, moment)
        if when is None:
            return []
        return [_from_db(r) for r in await pool.fetch("SELECT * FROM tequity_ticks WHERE channel = $1 AND taken_at = $2", channel, when)]
    with _lock:
        times = [r["taken_at"] for r in _rows if r["channel"] == channel and r["taken_at"] <= moment]
        if not times:
            return []
        when = max(times)
        return [dict(r) for r in _rows if r["channel"] == channel and r["taken_at"] == when]


async def first_tick_from(channel: str, moment: datetime) -> list[dict]:
    """Every row of the first tick at or after `moment` for a channel."""
    pool = await _pool()
    if pool is not None:
        when = await pool.fetchval("SELECT min(taken_at) FROM tequity_ticks WHERE channel = $1 AND taken_at >= $2", channel, moment)
        if when is None:
            return []
        return [_from_db(r) for r in await pool.fetch("SELECT * FROM tequity_ticks WHERE channel = $1 AND taken_at = $2", channel, when)]
    with _lock:
        times = [r["taken_at"] for r in _rows if r["channel"] == channel and r["taken_at"] >= moment]
        if not times:
            return []
        when = min(times)
        return [dict(r) for r in _rows if r["channel"] == channel and r["taken_at"] == when]


async def movers_between(venue: str, start: datetime, end: datetime, *, stocks_only: bool = False, limit: int = 15) -> dict:
    """Price change per pair between the last tick at or before `start` (or
    the first one after it, when the ledger begins inside the period) and
    the last tick at or before `end`, from stored prices (not the feed's own
    24h figure): the "since this morning" and "this week" reads."""
    channel = tequity.MOVERS.get(venue)
    if not channel:
        return {"venue": venue, "rows": [], "from": None, "to": None}
    a, b = await tick_at(channel, start), await tick_at(channel, end)
    if not a:
        a = await first_tick_from(channel, start)
    if a and b and a[0]["taken_at"] == b[0]["taken_at"]:
        a = []                                                            # one tick is not a change
    first = {r["symbol"]: r for r in a}
    out = []
    for r in b:
        p0 = (first.get(r["symbol"]) or {}).get("last_price")
        p1 = r.get("last_price")
        if not p0 or p1 is None or (stocks_only and not r.get("is_stock")):
            continue
        out.append({**r, "price_then": p0, "change_between_pct": (p1 - p0) / p0 * 100})
    out.sort(key=lambda r: r["change_between_pct"], reverse=True)
    return {"venue": venue, "from": a[0]["taken_at"] if a else None, "to": b[0]["taken_at"] if b else None,
            "gainers": out[:limit], "losers": list(reversed(out))[:limit], "pairs": len(out)}


async def volume_leaders(venue: str, days: float, *, stocks_only: bool = False, limit: int = 15) -> list[dict]:
    """Pairs by mean 24h quote volume across the ticks of the last `days`."""
    since = _now() - timedelta(days=days)
    channel = tequity.MOVERS.get(venue)
    pool = await _pool()
    if pool is not None and channel:
        rows = await pool.fetch(
            "SELECT symbol, bool_or(is_stock) AS is_stock, avg(quote_volume) AS mean_volume, count(*) AS ticks, max(last_price) AS high, min(last_price) AS low "
            "FROM tequity_ticks WHERE channel = $1 AND taken_at >= $2 GROUP BY symbol ORDER BY mean_volume DESC NULLS LAST LIMIT $3",
            channel, since, limit * 3)
        out = [dict(r) for r in rows]
    else:
        with _lock:
            by: dict[str, list[dict]] = {}
            for r in _rows:
                if r["channel"] == channel and r["taken_at"] >= since:
                    by.setdefault(r["symbol"], []).append(r)
        out = [{"symbol": s, "is_stock": any(x["is_stock"] for x in xs), "mean_volume": sum(x["quote_volume"] or 0 for x in xs) / len(xs), "ticks": len(xs),
                "high": max((x["last_price"] or 0) for x in xs), "low": min((x["last_price"] or 0) for x in xs)} for s, xs in by.items()]
        out.sort(key=lambda r: r["mean_volume"], reverse=True)
    if stocks_only:
        out = [r for r in out if r.get("is_stock")]
    return out[:limit]


async def status() -> dict:
    """What the ledger holds: rows, span and last tick per channel."""
    pool = await _pool()
    if pool is not None:
        rows = await pool.fetch("SELECT channel, count(*) AS rows, min(taken_at) AS first, max(taken_at) AS last, count(DISTINCT taken_at) AS ticks FROM tequity_ticks GROUP BY channel")
        return {r["channel"]: {"rows": r["rows"], "ticks": r["ticks"], "first": r["first"].isoformat() if r["first"] else None, "last": r["last"].isoformat() if r["last"] else None} for r in rows}
    with _lock:
        out: dict[str, dict] = {}
        for r in _rows:
            c = out.setdefault(r["channel"], {"rows": 0, "ticks": set(), "first": None, "last": None})
            c["rows"] += 1; c["ticks"].add(r["taken_at"])
            c["first"] = min(c["first"] or r["taken_at"], r["taken_at"]); c["last"] = max(c["last"] or r["taken_at"], r["taken_at"])
        return {k: {"rows": v["rows"], "ticks": len(v["ticks"]), "first": v["first"].isoformat(), "last": v["last"].isoformat()} for k, v in out.items()}
