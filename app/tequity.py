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
from datetime import datetime, timezone

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
        return _snapshots.get(channel)


def _sync(coro):
    """Tools are called from worker threads without a loop; run the coroutine there."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# ----------------------------------------------------------------------------
# reading a request
# ----------------------------------------------------------------------------

_DEX = re.compile(r"\b(aster|hyperliquid|hl)\b", re.I)
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


def movers_matches(request: str) -> bool:
    text = request or ""
    dex = _dex_of(text)
    if not dex:
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


def render_movers(dex: str, snap: dict, *, stocks_only: bool, losers: bool, limit: int = 15) -> str:
    payload = snap["data"].get("data") or {}
    rows = [r for r in payload.get("tokens") or [] if isinstance(r, dict)]
    if stocks_only:
        rows = [r for r in rows if r.get("is_stock")]
    rows = [r for r in rows if r.get("price_change_percent") is not None]
    rows.sort(key=lambda r: float(r.get("price_change_percent") or 0), reverse=not losers)
    top = rows[:limit]
    what = ("Tokenized stocks" if stocks_only else "Pairs") + (" down the most" if losers else " up the most")
    lines = [f"# {what} on {dex.title()} ({payload.get('range') or '1d'})",
             f"**Provider**: Tequity (internal feed) · **Snapshot**: {_stamp(snap)} · {len(rows)} {'stocks' if stocks_only else 'pairs'} of {payload.get('count') or len(payload.get('tokens') or [])} listed",
             "", "| # | Pair | Type | Last price | 24h price change | 24h quote volume |", "|---:|---|---|---:|---:|---:|"]
    for i, r in enumerate(top, start=1):
        kind = "stock" if r.get("is_stock") else ("perp" if dex == "hyperliquid" else "token")
        lines.append(f"| {i} | {r.get('base_asset') or r.get('symbol')}/{r.get('quote_asset') or '?'} | {kind} | {_price(r.get('last_price'))} | {_pct(r.get('price_change_percent'))} | {_money(r.get('quote_volume'))} |")
    if not top:
        lines.append("| — | no rows in this snapshot | | | | |")
    lines += ["", "\"24h price change\" is the pair's last-price move over 24 hours, not volume growth. Quote volume is in the quote asset (USDT or USDC). "
                  "One snapshot of the venue's own list; not a recommendation."]
    return "\n".join(lines)


def render_trending(snap: dict, *, stocks_only: bool, dex: str | None, limit: int = 15) -> str:
    rows = [r for r in snap["data"].get("trending") or [] if isinstance(r, dict)]
    if dex:
        rows = [r for r in rows if str(r.get("dex") or "").lower() == dex]
    if stocks_only:
        rows = [r for r in rows if r.get("is_stock")]
    top = rows[:limit]
    lines = [f"# Trending {'tokenized stocks' if stocks_only else 'pairs'}{' on ' + dex.title() if dex else ' across Aster and Hyperliquid'}",
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
    dex = _dex_of(request) or "hyperliquid"
    channel = MOVERS[dex]
    snap = _sync(snapshot(channel))
    if not snap:
        raise RuntimeError(f"Tequity feed has no {dex} movers snapshot right now")
    stocks_only = bool(_STOCK_WORDS.search(request or ""))
    losers = bool(_LOSERS.search(request or ""))
    rows = (snap["data"].get("data") or {}).get("tokens") or []
    evidence.complete("tequity_movers", {"kind": "venue", "id": dex, "chain": None, "symbol": None},
                      {"pairs": len(rows), "stocks": sum(1 for r in rows if isinstance(r, dict) and r.get("is_stock")), "range": (snap["data"].get("data") or {}).get("range")},
                      [{"provider": "tequity", "endpoint": channel, "as_of": _stamp(snap)}])
    return compact_tool_result(render_movers(dex, snap, stocks_only=stocks_only, losers=losers))


def trending(request: str) -> str:
    snap = _sync(snapshot(TRENDING))
    if not snap:
        raise RuntimeError("Tequity feed has no trending snapshot right now")
    rows = snap["data"].get("trending") or []
    evidence.complete("tequity_trending", {"kind": "venue", "id": "aster+hyperliquid", "chain": None, "symbol": None},
                      {"pairs": len(rows), "stocks": sum(1 for r in rows if isinstance(r, dict) and r.get("is_stock"))},
                      [{"provider": "tequity", "endpoint": TRENDING, "as_of": _stamp(snap)}])
    return compact_tool_result(render_trending(snap, stocks_only=bool(_STOCK_WORDS.search(request or "")), dex=_dex_of(request)))


def news(request: str) -> str:
    snap = _sync(snapshot(NEWS))
    if not snap:
        raise RuntimeError("Tequity feed has published no news yet")
    return compact_tool_result(render_news(snap))


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
            "tequity_news", self.name, ("news",), news,
            enabled=enabled, matches=news_matches,
            keywords=("equities news", "stock headlines"),
            cache_ttl_seconds=30, priority=8, spec=TOOL_SPECS.get("tequity_news"),
            description="Breaking equities headlines from the company's live feed (only once the feed has published any)",
        ))
