"""Polymarket odds about an asset: money-backed probabilities as evidence.

From the last30days review (user decision, 2026-09-22). Polymarket's Gamma
API is public and keyless; a market's Yes price is the crowd's probability
with real money behind it. Markets exist for majors and events (Bitcoin
above X, ETF approvals, listings), rarely for memes, so this is a deep-dive
dimension and desk evidence when it finds something and silent otherwise.

Kept: open markets whose question names the asset (symbol or name), sorted
by money at stake, dust markets dropped. Shown: the question, the Yes
probability, the volume, and when it resolves. The developer network's
resolver returned no address for the Gamma host and reset the connection
when pinned by IP (2026-09-22), so this shipped verified by tests only; the
deployment's network decides whether it answers.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

import httpx

from app.metrics import increment
from app.provider_router import NoData, ProviderRouter, ProviderTool
from app.settings import settings
from app.tool_catalog import TOOL_SPECS

logger = logging.getLogger(__name__)

MIN_VOLUME_USD = 1_000.0
LIMIT = 8
_TTL = 600.0
_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = threading.Lock()

ODDS_ASK = re.compile(r"\bpolymarket\b|\bprediction\s+markets?\b|\bodds\s+(?:of|on|for|that)\b|\bwhat\s+are\s+the\s+odds\b|\bprobability\s+(?:of|that)\b", re.I)
_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP", "DOGE": "Dogecoin", "BNB": "BNB", "ADA": "Cardano", "AVAX": "Avalanche",
          "LINK": "Chainlink", "DOT": "Polkadot", "TRX": "Tron", "TON": "Toncoin", "SUI": "Sui", "HYPE": "Hyperliquid", "LTC": "Litecoin", "ZEC": "Zcash"}


def enabled() -> bool:
    return bool(settings.polymarket_enabled)


def reset_for_test() -> None:
    with _lock:
        _cache.clear()


def matches(request: str) -> bool:
    from app.social_sentiment import extract_symbol

    return bool(ODDS_ASK.search(request or "")) and extract_symbol(request) is not None


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value) if isinstance(value, str) else []
        return parsed if isinstance(parsed, list) else []
    except ValueError:
        return []


def _date(value) -> str | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _about(text: str, symbol: str, name: str | None) -> bool:
    t = text.lower()
    if re.search(rf"(?<![a-z]){re.escape(symbol.lower())}(?![a-z])", t):
        return True
    return bool(name and re.search(rf"(?<![a-z]){re.escape(name.lower())}(?![a-z])", t))


def _search(query: str) -> dict:
    increment("polymarket_requests")
    with httpx.Client(timeout=12.0, headers={"User-Agent": "Orbit/1.0 (crypto research)"}) as client:
        response = client.get(f"{settings.polymarket_gamma_url.rstrip('/')}/public-search", params={"q": query, "limit_per_type": 20, "events_status": "active"})
        response.raise_for_status()
        return response.json()


def parse_events(payload: dict, symbol: str, name: str | None) -> list[dict]:
    """Open markets about the asset from a search response, by money at stake."""
    out: list[dict] = []
    for event in (payload.get("events") or []) if isinstance(payload, dict) else []:
        if not isinstance(event, dict) or event.get("closed") or event.get("active") is False:
            continue
        title = str(event.get("title") or "")
        for market in event.get("markets") or []:
            if not isinstance(market, dict) or market.get("closed") or market.get("active") is False:
                continue
            question = str(market.get("question") or title)
            if not (_about(question, symbol, name) or _about(title, symbol, name)):
                continue
            outcomes = [str(o) for o in _json_list(market.get("outcomes"))]
            prices = [_num(p) for p in _json_list(market.get("outcomePrices"))]
            if not prices:
                continue
            yes_index = next((i for i, o in enumerate(outcomes) if o.lower() == "yes"), 0)
            yes = prices[yes_index] if yes_index < len(prices) else prices[0]
            volume = _num(market.get("volume")) or _num(event.get("volume"))
            if volume < MIN_VOLUME_USD:
                continue
            out.append({"question": question[:160], "event": title[:160], "yes": round(max(0.0, min(1.0, yes)), 4), "outcomes": outcomes[:4],
                        "prices": prices[:4], "volume": volume, "volume_24h": _num(market.get("volume24hr")),
                        "ends": _date(market.get("endDate") or event.get("endDate")),
                        "url": f"https://polymarket.com/event/{event.get('slug')}" if event.get("slug") else None})
    out.sort(key=lambda m: -m["volume"])
    return out[:LIMIT]


def markets_for(symbol: str, name: str | None = None) -> list[dict]:
    sym = symbol.strip().lstrip("$").upper()
    name = name or _NAMES.get(sym)
    key = f"{sym}:{name or ''}"
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    payload = _search(name or sym)
    markets = parse_events(payload, sym, name)
    if name and not markets:
        markets = parse_events(_search(sym), sym, name)
    with _lock:
        _cache[key] = (now + _TTL, markets)
    return markets


def _usd(value: float) -> str:
    if value >= 1e6:
        return f"${value / 1e6:,.2f}M"
    if value >= 1e3:
        return f"${value / 1e3:,.1f}K"
    return f"${value:,.0f}"


def render_card(symbol: str, markets: list[dict]) -> str | None:
    if not markets:
        return None
    lines = ["# Prediction markets", f"**Source**: Polymarket (Gamma API) · **Asset**: ${symbol} · **Checked**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", "",
             "| Market | Yes | Volume | Resolves |", "|---|---:|---:|---|"]
    for m in markets:
        q = m["question"].replace("|", "/")
        label = f"[{q}]({m['url']})" if m.get("url") else q
        lines.append(f"| {label} | {m['yes'] * 100:.0f}% | {_usd(m['volume'])} | {m.get('ends') or '—'} |")
    lines += ["", "Yes is the last traded price of the Yes share: the crowd's money-backed probability, not a forecast. Thin markets move on small bets; weigh the volume."]
    return "\n".join(lines)


def polymarket_card(request: str) -> str:
    from app.social_sentiment import extract_symbol

    symbol = extract_symbol(request)
    if not symbol:
        raise ValueError("Name the asset, e.g. 'polymarket odds on BTC'")
    markets = markets_for(symbol)
    card = render_card(symbol, markets)
    if card is None:
        raise NoData(f"Polymarket has no open market naming {symbol}")
    return card


class PolymarketProvider:
    name = "polymarket"

    def enabled(self) -> bool:
        return enabled()

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "polymarket_odds", self.name, ("market_sentiment", "market_data"), polymarket_card,
            enabled=enabled, matches=matches,
            keywords=("polymarket", "prediction market", "odds"),
            chains=(), cache_ttl_seconds=600, priority=14, spec=TOOL_SPECS.get("polymarket_odds"),
            description="Open Polymarket markets naming an asset with the Yes probability, volume and resolution date",
        ))
