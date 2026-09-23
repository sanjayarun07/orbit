"""Tequity: the company's internal equities and perps feed, over one websocket.

Channels (confirmed live 2026-09-23 over wss://; plain ws:// is rejected):
  equities:movers:aster        every ~3 s, a full snapshot of 500 Aster pairs
  equities:movers:hyperliquid  every ~5 s, 358 Hyperliquid pairs
  equities:trending            every ~4 s, 50 pairs across DEXes, stocks flagged
  equities:news                on publish (nothing seen in 150 s)
  equities:stats[:4h|24h|7d|30d|1y]   on change (nothing seen in 150 s)
A record is thin and clean: symbol, base/quote asset, last price, 24h
change, quote volume, tags, is_stock, logo. No book, no funding, no per-row
time; the server timestamp on each snapshot is the freshness we report.

One worker per process keeps the latest snapshot of every channel; a tool
reads the snapshot, or when none has arrived yet takes one from a short
one-shot subscription. News and stats are wired but their tools stay out
of reach until a snapshot has actually arrived.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from app import evidence
from app.provider_router import ProviderRouter, ProviderTool
from app.settings import settings
from app.tool_catalog import TOOL_SPECS
from app.tool_results import compact_tool_result

logger = logging.getLogger(__name__)

MOVERS = {"aster": "equities:movers:aster", "hyperliquid": "equities:movers:hyperliquid"}
TRENDING = "equities:trending"
NEWS = "equities:news"
STATS = ("equities:stats", "equities:stats:4h", "equities:stats:24h", "equities:stats:7d", "equities:stats:30d", "equities:stats:1y")
CHANNELS = (*MOVERS.values(), TRENDING, NEWS, *STATS)

_snapshots: dict[str, dict] = {}          # channel -> {"data", "received_at", "server_ts"}
_lock = asyncio.Lock()
STALE_SECONDS = 120
ONE_SHOT_TIMEOUT_S = 8.0


def enabled() -> bool:
    return bool(settings.tequity_enabled and settings.tequity_ws_url)


def reset_for_test() -> None:
    _snapshots.clear()


def _store(channel: str, data: dict, server_ts: float | None = None) -> None:
    _snapshots[channel] = {"data": data, "received_at": time.time(), "server_ts": server_ts}


def _ts_of(msg: dict) -> float | None:
    data = (msg.get("event") or {}).get("data") or {}
    raw = data.get("timestamp") if isinstance(data, dict) else None
    try:
        return float(raw) / 1000.0 if raw and float(raw) > 1e11 else (float(raw) if raw else None)
    except (TypeError, ValueError):
        return None


async def _listen(channels: tuple[str, ...], *, until: float | None = None, stop_when: set[str] | None = None) -> None:
    """Subscribe and store every snapshot until `until` (monotonic deadline)
    or until every channel in `stop_when` has been seen."""
    import websockets

    seen: set[str] = set()
    async with websockets.connect(settings.tequity_ws_url, max_size=50_000_000, open_timeout=15) as ws:
        await ws.send(json.dumps({"action": "subscribe", "channels": list(channels)}))
        while True:
            if until is not None and time.monotonic() > until:
                return
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            channel = (msg.get("event") or {}).get("channel")
            if not channel or channel not in channels:
                continue
            data = msg["event"].get("data")
            if isinstance(data, dict):
                _store(channel, data, _ts_of(msg))
                seen.add(channel)
            if stop_when and stop_when <= seen:
                return


async def worker() -> None:
    """Keep the snapshots fresh for this process; reconnect with backoff."""
    if not enabled():
        return
    backoff = 2.0
    while True:
        try:
            await _listen(CHANNELS)
            backoff = 2.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("tequity: stream dropped (%s); reconnecting in %.0fs", str(exc)[:120], backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def snapshot(channel: str) -> dict | None:
    """The latest snapshot for a channel: from the worker when fresh, else
    from a short one-shot subscription. None when nothing arrives."""
    if not enabled():
        return None
    hit = _snapshots.get(channel)
    if hit and time.time() - hit["received_at"] <= STALE_SECONDS:
        return hit
    async with _lock:
        hit = _snapshots.get(channel)
        if hit and time.time() - hit["received_at"] <= STALE_SECONDS:
            return hit
        try:
            await _listen((channel,), until=time.monotonic() + ONE_SHOT_TIMEOUT_S, stop_when={channel})
        except Exception as exc:  # noqa: BLE001
            logger.warning("tequity: one-shot %s failed: %s", channel, str(exc)[:120])
        hit = _snapshots.get(channel)
        # Still stale after the refresh: the feed is down. Old prices are not
        # current data (review of 330bc651: a day-old snapshot rendered as now).
        return hit if hit and time.time() - hit["received_at"] <= STALE_SECONDS else None


def last_seen(channel: str) -> float | None:
    """Seconds since the last snapshot of a channel arrived, or None."""
    hit = _snapshots.get(channel)
    return (time.time() - hit["received_at"]) if hit else None


def _unavailable(tool: str, channel: str, what: str) -> RuntimeError:
    age = last_seen(channel)
    since = f"; the last snapshot arrived {age / 60:.0f} min ago" if age is not None else ""
    evidence.unavailable(tool, {"kind": "venue", "id": channel, "chain": None, "symbol": None}, f"feed unavailable{since}",
                         [{"provider": "tequity", "endpoint": channel, "as_of": None}])
    return RuntimeError(f"Tequity feed has no current {what} snapshot{since}")


_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """The server's loop, so tools called from worker threads run their
    database reads on it: the Postgres pool is bound to that loop, and a
    read from a fresh loop fell back to the empty memory store (live,
    2026-09-23: "no ticks" while the ledger held them)."""
    global _loop
    _loop = loop


def _sync(coro):
    """Tools are called from worker threads without a loop; run the coroutine
    on the server's loop when one is registered, else in a fresh loop."""
    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False
    if not running and _loop is not None and _loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=60)
    if not running:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# ----------------------------------------------------------------------------
# reading a request
# ----------------------------------------------------------------------------

_DEX = re.compile(r"\b(aster|hyperliquid|hl)\b", re.I)
_BOTH = re.compile(r"\b(?:across|both|either|all)\b.{0,30}\b(?:venues?|dexes|exchanges|aster|hyperliquid)\b|\baster\b.{0,20}\b(?:and|vs\.?|versus)\b.{0,20}\bhyperliquid\b|\bhyperliquid\b.{0,20}\b(?:and|vs\.?|versus)\b.{0,20}\baster\b", re.I)
_NO_STOCKS = re.compile(r"\b(?:excluding|exclude|without|no|not|ignore|skip|minus)\s+(?:the\s+)?(?:tokeni[sz]ed\s+)?(?:stocks?|equit(?:y|ies)|shares?)\b|\bcrypto\s+only\b|\bonly\s+crypto\b", re.I)
_MOVERS_WORDS = re.compile(r"\b(?:movers?|gainers?|losers?|top\s+(?:stocks?|tokens?|pairs?|perps?)|biggest\s+(?:moves?|winners?|losers?)|"
                           r"most\s+(?:active|traded)|pumping|dumping|up\s+the\s+most|down\s+the\s+most|leaders?|laggards?)\b", re.I)
_STOCK_WORDS = re.compile(r"\b(?:stocks?|equit(?:y|ies)|tokeni[sz]ed\s+(?:stocks?|equities|shares)|shares?|xstocks?)\b", re.I)
_LOSERS = re.compile(r"\b(?:losers?|dumping|down\s+the\s+most|laggards?|worst)\b", re.I)
_TRENDING_WORDS = re.compile(r"\b(?:trending|hot|what'?s\s+moving|momentum)\b", re.I)


def _dex_of(request: str) -> str | None:
    m = _DEX.search(request or "")
    if not m:
        return None
    word = m.group(1).lower()
    return "hyperliquid" if word in ("hyperliquid", "hl") else "aster"


def venues_of(request: str) -> list[str]:
    """Every venue an ask names, in order; both when it says across, both,
    or names the two (live run 2026-09-23: "across Aster and Hyperliquid"
    answered with Aster alone)."""
    text = request or ""
    found = []
    for m in _DEX.finditer(text):
        v = "hyperliquid" if m.group(1).lower() in ("hyperliquid", "hl") else "aster"
        if v not in found:
            found.append(v)
    if _BOTH.search(text) and len(found) < 2:
        found = ["aster", "hyperliquid"]
    return found


def stock_filter(request: str) -> bool | None:
    """True for stocks only, False for crypto only ("excluding stocks"), None for all."""
    text = request or ""
    if _NO_STOCKS.search(text):
        return False
    return True if _STOCK_WORDS.search(text) else None


def movers_matches(request: str) -> bool:
    text = request or ""
    dex = _dex_of(text) or (venues_of(text) or [None])[0]
    if not dex or _VOLUME_WORDS.search(text):
        return False
    if re.search(r"\b(?:positions?|balances?|margin|liquidation|funding|open\s+interest|my\b)", text, re.I):
        return False                                   # a wallet's perps are the hyperliquid_* tools' business
    return bool(_MOVERS_WORDS.search(text) or _STOCK_WORDS.search(text))


def trending_matches(request: str) -> bool:
    text = request or ""
    if not _TRENDING_WORDS.search(text):
        return False
    return bool(_dex_of(text) or _STOCK_WORDS.search(text) or re.search(r"\bperps?\b", text, re.I))


def news_matches(request: str) -> bool:
    return bool(re.search(r"\b(?:news|headlines?|breaking)\b", request or "", re.I) and _STOCK_WORDS.search(request or "")) and NEWS in _snapshots


_PERIOD = re.compile(
    r"\b(?:since\s+this\s+morning|this\s+morning|today|this\s+week|past\s+week|last\s+week|last\s+24\s*h(?:ours)?|past\s+24\s*h(?:ours)?|"
    r"(?:over|in|during|for)?\s*the\s+(?:last|past)\s+(?P<n>\d+)\s*(?P<u>hours?|h|days?|d|weeks?|w)|(?:last|past)\s+(?P<n2>\d+)\s*(?P<u2>hours?|h|days?|d|weeks?|w)|"
    r"since\s+(?P<since>[A-Za-z]{3,9}\.?\s+\d{1,2}(?:,?\s+\d{4})?|\d{4}-\d{2}-\d{2}|yesterday))\b", re.I)
_VOLUME_WORDS = re.compile(r"\b(?:most\s+traded|volume\s+leaders?|highest\s+volume|by\s+(?:traded\s+|quote\s+)?volume|most\s+volume|biggest\s+volume|volume\s+rank\w*|rank\w*\s+by\s+volume|top\s+volume)\b", re.I)
_HISTORY_WORDS = re.compile(r"\b(?:how\s+(?:has|did|is)|moved|moving|move|change[ds]?|performance|performed|done|doing|trend|history)\b", re.I)


def period_start(request: str, now: datetime | None = None) -> datetime | None:
    """The start of the period a request names, or None."""
    now = now or datetime.now(timezone.utc)
    m = _PERIOD.search(request or "")
    if not m:
        return None
    text = m.group(0).lower()
    if "morning" in text or text == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    n, u = (m.group("n") or m.group("n2")), (m.group("u") or m.group("u2") or "")
    if n:                                                                  # a quantity and its unit first: "last 24 days" is days (review of 07190a22)
        unit = u.lower()[:1]
        return now - (timedelta(hours=int(n)) if unit == "h" else timedelta(days=int(n)) if unit == "d" else timedelta(weeks=int(n)))
    if "week" in text:
        return now - timedelta(days=7)
    if re.fullmatch(r"(?:last|past)\s+24\s*h(?:ours)?", text):
        return now - timedelta(hours=24)
    if m.group("since"):
        if m.group("since").lower() == "yesterday":
            return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        from app.snapshot_compare import dates_in
        found = dates_in(m.group("since"), now)
        if not found:
            return None
        when = found[0]
        if when > now:
            when = when.replace(year=when.year - 1)                        # a period never starts in the future
        return when
    return None


def base_in(request: str) -> str | None:
    """The base asset a request names ($tsla, TSLA, 'AAPL on hyperliquid'), or None."""
    text = _DEX.sub(" ", request or "")
    m = re.search(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b", text)
    if m:
        return m.group(1).upper()
    skip = {"USDC", "USDT", "USD", "HL", "AI", "OK", "ETF", "LP", "PDA", "AMM"}
    for m in re.finditer(r"\b([A-Z][A-Z0-9]{1,9})\b", text):
        if m.group(1) not in skip:
            return m.group(1)
    return None


def resolve_pair(venue: str, base: str) -> str | None:
    """The venue's symbol for a base asset, from the latest movers snapshot."""
    snap = _snapshots.get(MOVERS.get(venue, ""))
    rows = ((snap or {}).get("data") or {}).get("data", {}).get("tokens") or []
    for r in rows:
        if isinstance(r, dict) and str(r.get("base_asset") or "").upper() == base.upper():
            return str(r.get("symbol"))
    return None


def history_matches(request: str) -> bool:
    text = request or ""
    return bool(_dex_of(text) and period_start(text) and base_in(text) and _HISTORY_WORDS.search(text)
                and not re.search(r"\b(?:positions?|balances?|margin|my)\b", text, re.I))


def period_movers_matches(request: str) -> bool:
    text = request or ""
    return bool(_dex_of(text) and period_start(text) and (_MOVERS_WORDS.search(text) or _STOCK_WORDS.search(text)) and not base_in(text)
                and not _VOLUME_WORDS.search(text)
                and not re.search(r"\b(?:positions?|balances?|margin|my)\b", text, re.I))


def volume_leaders_matches(request: str) -> bool:
    text = request or ""
    return bool(venues_of(text) and _VOLUME_WORDS.search(text) and not re.search(r"\b(?:positions?|balances?|margin|my)\b", text, re.I))


def _fmt_when(when) -> str:
    return when.strftime("%Y-%m-%d %H:%M UTC") if when else "—"


def render_history(venue: str, base: str, symbol: str, ticks: list[dict], start: datetime, live: dict | None) -> str:
    lines = [f"# {base} on {venue.title()} since {_fmt_when(start)}", f"**Provider**: Tequity tick ledger (5-minute snapshots of the venue's feed) · **Pair**: {symbol}", ""]
    if len(ticks) < 2:
        lines += [f"The ledger has {'one tick' if ticks else 'no ticks'} for {symbol} in that period, so no change can be read from stored prices. "
                  f"Ticks are recorded every 5 minutes while the feed is up; the ledger keeps {settings.tequity_retention_days} days."]
    else:
        first, last = ticks[0], ticks[-1]
        prices = [t["last_price"] for t in ticks if t.get("last_price") is not None]
        change = (last["last_price"] - first["last_price"]) / first["last_price"] * 100 if first.get("last_price") else None
        lines += ["| | Value |", "|---|---:|",
                  f"| First stored price ({_fmt_when(first['taken_at'])}) | {_price(first['last_price'])} |",
                  f"| Latest stored price ({_fmt_when(last['taken_at'])}) | {_price(last['last_price'])} |",
                  f"| Change between those ticks | {_pct(change)} |",
                  f"| High / low across {len(ticks)} ticks | {_price(max(prices))} / {_price(min(prices))} |",
                  f"| Latest stored 24h quote volume | {_money(last.get('quote_volume'))} |"]
        if first["taken_at"] > start + timedelta(minutes=10):
            lines += ["", f"The ledger's first tick for this pair is {_fmt_when(first['taken_at'])}, later than the period asked for; the change is measured from there."]
    if live:
        lines += ["", f"Feed now: {_price(live.get('last_price'))}, {_pct(live.get('price_change_percent'))} over the venue's own 24h window, {_money(live.get('quote_volume'))} quote volume."]
    lines += ["", "Stored prices are the feed's last price at each tick, not trades; a change between ticks is a price move, not volume. Not a recommendation."]
    return "\n".join(lines)


def render_period_movers(venue: str, out: dict, start: datetime, *, stocks_only: bool, losers: bool, limit: int = 15) -> str:
    rows = out["losers"] if losers else out["gainers"]
    what = ("Tokenized stocks" if stocks_only else "Pairs") + (" down the most" if losers else " up the most")
    lines = [f"# {what} on {venue.title()} since {_fmt_when(start)}",
             f"**Provider**: Tequity tick ledger · **From tick**: {_fmt_when(out.get('from'))} · **To tick**: {_fmt_when(out.get('to'))} · {out.get('pairs', 0)} pairs present at both ends", ""]
    if not rows:
        lines.append("No pair has stored prices at both ends of that period yet. Ticks are recorded every 5 minutes while the feed is up.")
    else:
        lines += ["| # | Pair | Type | Price then | Price now | Change between ticks | Latest 24h quote volume |", "|---:|---|---|---:|---:|---:|---:|"]
        for i, r in enumerate(rows[:limit], start=1):
            lines.append(f"| {i} | {r.get('base_asset') or r['symbol']}/{r.get('quote_asset') or '?'} | {'stock' if r.get('is_stock') else 'crypto'} | {_price(r['price_then'])} | {_price(r['last_price'])} | {_pct(r['change_between_pct'])} | {_money(r.get('quote_volume'))} |")
    if out.get("from") and out["from"] > start + timedelta(minutes=10):
        lines += ["", f"The ledger's earliest tick in range is {_fmt_when(out['from'])}, later than the period asked for; changes are measured from there."]
    lines += ["", "Changes are between two stored ticks of the feed's last price, not the venue's own 24h figure. Not a recommendation."]
    return "\n".join(lines)


def render_volume_leaders(venue: str, rows: list[dict], days: float, *, stocks_only: bool | None, hours: float | None = None) -> str:
    span = f"last {hours:g} hour{'s' if hours != 1 else ''}" if hours is not None and hours < 48 else f"last {days:g} day{'s' if days != 1 else ''}"
    lines = [f"# {'Tokenized stocks' if stocks_only is True else 'Crypto pairs' if stocks_only is False else 'Pairs'} by traded volume on {venue.title()}, {span}",
             "**Provider**: Tequity tick ledger · mean of the feed's 24h quote volume across stored ticks", "",
             "| # | Pair | Type | Mean 24h quote volume | Ticks | Price high / low |", "|---:|---|---|---:|---:|---:|"]
    for i, r in enumerate(rows, start=1):
        lines.append(f"| {i} | {r['symbol']} | {'stock' if r.get('is_stock') else 'crypto'} | {_money(r.get('mean_volume'))} | {r.get('ticks')} | {_price(r.get('high'))} / {_price(r.get('low'))} |")
    if not rows:
        lines.append("| — | no stored ticks in that window | | | | |")
    lines += ["", "A mean of rolling 24h figures over the window, so it over-weights nothing but is not a sum of trades. Not a recommendation."]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# cards
# ----------------------------------------------------------------------------

def _stamp(snap: dict) -> str:
    ts = snap.get("server_ts")
    when = datetime.fromtimestamp(ts, timezone.utc) if ts else datetime.fromtimestamp(snap["received_at"], timezone.utc)
    return when.strftime("%Y-%m-%d %H:%M:%S UTC")


def _price(v) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    if n >= 1:
        return f"${n:,.2f}"
    if n == 0:
        return "$0"
    import math
    return f"${n:.{min(3 - math.floor(math.log10(abs(n))), 12)}f}"


def _money(v) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    if abs(n) >= 1e9:
        return f"${n / 1e9:.2f}B"
    if abs(n) >= 1e6:
        return f"${n / 1e6:.1f}M"
    if abs(n) >= 1e3:
        return f"${n / 1e3:.1f}K"
    return f"${n:,.0f}"


def _pct(v) -> str:
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def render_movers(dex: str, snap: dict, *, stocks_only: bool | None, losers: bool, limit: int = 15) -> str:
    payload = snap["data"].get("data") or {}
    rows = [r for r in payload.get("tokens") or [] if isinstance(r, dict)]
    if stocks_only is True:
        rows = [r for r in rows if r.get("is_stock")]
    elif stocks_only is False:
        rows = [r for r in rows if not r.get("is_stock")]
    rows = [r for r in rows if r.get("price_change_percent") is not None]
    rows.sort(key=lambda r: float(r.get("price_change_percent") or 0), reverse=not losers)
    top = rows[:limit]
    what = ("Tokenized stocks" if stocks_only is True else "Crypto pairs" if stocks_only is False else "Pairs") + (" down the most" if losers else " up the most")
    lines = [f"# {what} on {dex.title()} ({payload.get('range') or '1d'})",
             f"**Provider**: Tequity (internal feed) · **Snapshot**: {_stamp(snap)} · {len(rows)} {'stocks' if stocks_only is True else 'crypto pairs' if stocks_only is False else 'pairs'} of {payload.get('count') or len(payload.get('tokens') or [])} listed",
             "", "| # | Pair | Type | Last price | 24h price change | 24h quote volume |", "|---:|---|---|---:|---:|---:|"]
    for i, r in enumerate(top, start=1):
        kind = "stock" if r.get("is_stock") else ("perp" if dex == "hyperliquid" else "token")
        lines.append(f"| {i} | {r.get('base_asset') or r.get('symbol')}/{r.get('quote_asset') or '?'} | {kind} | {_price(r.get('last_price'))} | {_pct(r.get('price_change_percent'))} | {_money(r.get('quote_volume'))} |")
    if not top:
        lines.append("| — | no rows in this snapshot | | | | |")
    lines += ["", "\"24h price change\" is the pair's last-price move over 24 hours, not volume growth. Quote volume is in the quote asset (USDT or USDC). "
                  "One snapshot of the venue's own list; not a recommendation."]
    return "\n".join(lines)


def render_trending(snap: dict, *, stocks_only: bool | None, dex: str | None, limit: int = 15) -> str:
    rows = [r for r in snap["data"].get("trending") or [] if isinstance(r, dict)]
    if dex:
        rows = [r for r in rows if str(r.get("dex") or "").lower() == dex]
    if stocks_only is True:
        rows = [r for r in rows if r.get("is_stock")]
    elif stocks_only is False:
        rows = [r for r in rows if not r.get("is_stock")]
    top = rows[:limit]
    lines = [f"# Trending {'tokenized stocks' if stocks_only is True else 'crypto pairs' if stocks_only is False else 'pairs'}{' on ' + dex.title() if dex else ' across Aster and Hyperliquid'}",
             f"**Provider**: Tequity (internal feed) · **Snapshot**: {_stamp(snap)} · {len(rows)} in the feed's trending list",
             "", "| # | Pair | Venue | Type | Last price | 24h price change | 24h quote volume |", "|---:|---|---|---|---:|---:|---:|"]
    for i, r in enumerate(top, start=1):
        lines.append(f"| {i} | {r.get('base_asset') or r.get('symbol')}/{r.get('quote_asset') or '?'} | {r.get('dex') or '?'} | {'stock' if r.get('is_stock') else 'crypto'} | "
                     f"{_price(r.get('last_price'))} | {_pct(r.get('price_change_percent'))} | {_money(r.get('quote_volume'))} |")
    if not top:
        lines.append("| — | nothing trending matches | | | | | |")
    lines += ["", "The feed's own trending order (volume-led). \"24h price change\" is a price move, not volume growth. Not a recommendation."]
    return "\n".join(lines)


def render_news(snap: dict, limit: int = 10) -> str:
    items = [n for n in snap["data"].get("news") or [] if isinstance(n, dict)]
    lines = [f"# Equities news (internal feed)", f"**Provider**: Tequity · **Snapshot**: {_stamp(snap)} · {len(items)} articles", ""]
    for n in items[:limit]:
        title = n.get("title") or n.get("headline") or "—"
        url = n.get("url") or n.get("link")
        when = n.get("published_at") or n.get("time") or n.get("date") or ""
        lines.append(f"- {'[' + title + '](' + url + ')' if url else title}{' · ' + str(when)[:16] if when else ''}{' · ' + n['source'] if n.get('source') else ''}")
    if not items:
        lines.append("No articles in the latest snapshot.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# tools
# ----------------------------------------------------------------------------

def movers(request: str) -> str:
    venues = venues_of(request) or ["hyperliquid"]
    stocks_only = stock_filter(request)
    losers = bool(_LOSERS.search(request or ""))
    cards, missing = [], []
    for dex in venues:
        channel = MOVERS[dex]
        snap = _sync(snapshot(channel))
        if not snap:
            missing.append(dex)
            continue
        rows = (snap["data"].get("data") or {}).get("tokens") or []
        evidence.complete("tequity_movers", {"kind": "venue", "id": dex, "chain": None, "symbol": None},
                          {"pairs": len(rows), "stocks": sum(1 for r in rows if isinstance(r, dict) and r.get("is_stock")), "range": (snap["data"].get("data") or {}).get("range")},
                          [{"provider": "tequity", "endpoint": channel, "as_of": _stamp(snap)}])
        cards.append(render_movers(dex, snap, stocks_only=stocks_only, losers=losers, limit=10 if len(venues) > 1 else 15))
    if not cards:
        raise _unavailable("tequity_movers", MOVERS[venues[0]], f"{venues[0]} movers")
    if missing:
        cards.append(f"_{', '.join(v.title() for v in missing)}: no current snapshot from the feed; that venue is not shown, not empty._")
    if len(venues) > 1:
        cards.append("Each venue is its own list from its own snapshot; the two are not merged or ranked against each other.")
    return compact_tool_result("\n\n".join(cards))


def trending(request: str) -> str:
    snap = _sync(snapshot(TRENDING))
    if not snap:
        raise _unavailable("tequity_trending", TRENDING, "trending")
    rows = snap["data"].get("trending") or []
    evidence.complete("tequity_trending", {"kind": "venue", "id": "aster+hyperliquid", "chain": None, "symbol": None},
                      {"pairs": len(rows), "stocks": sum(1 for r in rows if isinstance(r, dict) and r.get("is_stock"))},
                      [{"provider": "tequity", "endpoint": TRENDING, "as_of": _stamp(snap)}])
    venues = venues_of(request)
    return compact_tool_result(render_trending(snap, stocks_only=stock_filter(request), dex=venues[0] if len(venues) == 1 else None))


def news(request: str) -> str:
    snap = _sync(snapshot(NEWS))
    if not snap:
        raise RuntimeError("Tequity feed has published no news yet")
    return compact_tool_result(render_news(snap))


def history(request: str) -> str:
    from app import tequity_ledger

    venue = _dex_of(request) or "hyperliquid"
    base = base_in(request)
    start = period_start(request)
    if not base or not start:
        raise ValueError("Name the asset and the period, e.g. 'how has TSLA moved on hyperliquid this week'")
    current = _sync(snapshot(MOVERS[venue]))                              # None when the feed is stale: then no "Feed now" line
    symbol = resolve_pair(venue, base) or f"{base}USDC"
    ticks = _sync(tequity_ledger.history(venue, symbol, start))
    live = next((r for r in (((current or {}).get("data") or {}).get("data", {}).get("tokens") or []) if isinstance(r, dict) and r.get("symbol") == symbol), None)
    evidence.complete("tequity_history", {"kind": "pair", "id": f"{venue}:{symbol}", "chain": None, "symbol": base},
                      {"ticks": len(ticks), "from": ticks[0]["taken_at"].isoformat() if ticks else None, "to": ticks[-1]["taken_at"].isoformat() if ticks else None},
                      [{"provider": "tequity_ledger", "endpoint": "tequity_ticks", "as_of": _fmt_when(ticks[-1]["taken_at"]) if ticks else None}], attempted=1)
    return compact_tool_result(render_history(venue, base, symbol, ticks, start, live))


def period_movers(request: str) -> str:
    from app import tequity_ledger

    venue = _dex_of(request) or "hyperliquid"
    start = period_start(request)
    if not start:
        raise ValueError("Name the period, e.g. 'movers on hyperliquid this week'")
    stocks_only = bool(_STOCK_WORDS.search(request or ""))
    out = _sync(tequity_ledger.movers_between(venue, start, datetime.now(timezone.utc), stocks_only=stocks_only))
    evidence.complete("tequity_period_movers", {"kind": "venue", "id": venue, "chain": None, "symbol": None},
                      {"pairs": out.get("pairs", 0), "from": out["from"].isoformat() if out.get("from") else None, "to": out["to"].isoformat() if out.get("to") else None},
                      [{"provider": "tequity_ledger", "endpoint": "tequity_ticks", "as_of": _fmt_when(out.get("to"))}])
    return compact_tool_result(render_period_movers(venue, out, start, stocks_only=stocks_only, losers=bool(_LOSERS.search(request or ""))))


def volume_leaders(request: str) -> str:
    from app import tequity_ledger

    venues = venues_of(request) or ["hyperliquid"]
    start = period_start(request)
    # The window as asked, down to a quarter hour ("two hours" was six, live 2026-09-23)
    days = max(0.01, (datetime.now(timezone.utc) - start).total_seconds() / 86400) if start else 7.0
    stocks_only = stock_filter(request)
    cards = []
    for venue in venues:
        rows = _sync(tequity_ledger.volume_leaders(venue, days, stocks_only=bool(stocks_only)))
        if stocks_only is False:
            rows = [r for r in rows if not r.get("is_stock")]
        evidence.complete("tequity_volume_leaders", {"kind": "venue", "id": venue, "chain": None, "symbol": None}, {"pairs": len(rows), "days": days},
                          [{"provider": "tequity_ledger", "endpoint": "tequity_ticks", "as_of": _fmt_when(datetime.now(timezone.utc))}])
        cards.append(render_volume_leaders(venue, rows, round(days, 2), stocks_only=stocks_only, hours=(datetime.now(timezone.utc) - start).total_seconds() / 3600 if start else None))
    return compact_tool_result("\n\n".join(cards))


class TequityProvider:
    name = "tequity"

    def enabled(self) -> bool:
        return enabled()

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "tequity_movers", self.name, ("token_discovery", "market_data"), movers,
            enabled=enabled, matches=movers_matches,
            keywords=("aster", "hyperliquid", "movers", "gainers", "losers", "tokenized stocks"),
            cache_ttl_seconds=10, priority=13, spec=TOOL_SPECS.get("tequity_movers"),
            description="Top movers on Aster or Hyperliquid from the company's live feed: pairs and tokenized stocks by 24h price change with volume",
        ))
        router.register(ProviderTool(
            "tequity_trending", self.name, ("token_discovery", "market_data"), trending,
            enabled=enabled, matches=trending_matches,
            keywords=("trending", "aster", "hyperliquid", "tokenized stocks", "perps"),
            cache_ttl_seconds=10, priority=13, spec=TOOL_SPECS.get("tequity_trending"),
            description="What is trending across Aster and Hyperliquid right now, crypto and tokenized stocks, from the company's live feed",
        ))
        router.register(ProviderTool(
            "tequity_history", self.name, ("market_data",), history,
            enabled=enabled, matches=history_matches,
            keywords=("since this morning", "this week", "moved", "aster", "hyperliquid"),
            cache_ttl_seconds=60, priority=13, spec=TOOL_SPECS.get("tequity_history"),
            description="How one pair on Aster or Hyperliquid moved over a period, from the company's stored 5-minute ticks",
        ))
        router.register(ProviderTool(
            "tequity_period_movers", self.name, ("token_discovery", "market_data"), period_movers,
            enabled=enabled, matches=period_movers_matches,
            keywords=("movers this week", "since this morning", "aster", "hyperliquid", "tokenized stocks"),
            cache_ttl_seconds=60, priority=14, spec=TOOL_SPECS.get("tequity_period_movers"),
            description="Gainers and losers on Aster or Hyperliquid over a period you name, measured between stored ticks rather than the venue's 24h figure",
        ))
        router.register(ProviderTool(
            "tequity_volume_leaders", self.name, ("token_discovery", "market_data"), volume_leaders,
            enabled=enabled, matches=volume_leaders_matches,
            keywords=("most traded", "volume leaders", "aster", "hyperliquid"),
            cache_ttl_seconds=120, priority=13, spec=TOOL_SPECS.get("tequity_volume_leaders"),
            description="Pairs by traded volume on Aster or Hyperliquid over the last days, from stored ticks",
        ))
        router.register(ProviderTool(
            "tequity_news", self.name, ("news",), news,
            enabled=enabled, matches=news_matches,
            keywords=("equities news", "stock headlines"),
            cache_ttl_seconds=30, priority=8, spec=TOOL_SPECS.get("tequity_news"),
            description="Breaking equities headlines from the company's live feed (only once the feed has published any)",
        ))
