"""Whether a token is listed on a centralised exchange, from the exchange
itself.

Review of 2026-09-20: "Binance" in the product scope means tokens tradable
in the Binance app, which no on-chain source establishes. Binance publishes
its spot symbols on a public, keyless endpoint; that is the authority. The
answer names the pairs and their status, or says the token is not listed.
Cached for ten minutes; the list changes a few times a day.
"""
from __future__ import annotations

import logging
import re
import threading
import time

import httpx

from app.provider_router import ProviderRouter, ProviderTool
from app.tool_catalog import TOOL_SPECS

logger = logging.getLogger(__name__)

_BINANCE = "https://api.binance.com/api/v3/exchangeInfo"
_TTL = 600.0
_cache: tuple[float, list[dict]] | None = None
_lock = threading.Lock()
LISTING_ASK = re.compile(r"\b(?:listed|listing|available|tradeable|tradable|trade)\b.{0,30}\b(?:on\s+)?binance\b|\bbinance\b.{0,30}\b(?:listed|listing|pairs?|spot|available)\b", re.I)
_QUOTES = ("USDT", "USDC", "FDUSD", "BTC", "ETH", "BNB", "TRY", "EUR", "BRL", "JPY")


def _symbols() -> list[dict]:
    global _cache
    now = time.monotonic()
    with _lock:
        if _cache and _cache[0] > now:
            return _cache[1]
    with httpx.Client(timeout=30) as client:
        response = client.get(_BINANCE, params={"permissions": "SPOT"})
        response.raise_for_status()
        rows = [s for s in response.json().get("symbols", []) if isinstance(s, dict)]
    with _lock:
        _cache = (now + _TTL, rows)
    return rows


def binance_pairs(symbol: str) -> list[dict]:
    base = (symbol or "").strip().lstrip("$").upper()
    return [s for s in _symbols() if s.get("baseAsset") == base or s.get("baseAsset") == f"1000{base}"]


def binance_listing(request: str) -> str:
    """The router tool: every Binance spot pair for the ticker named."""
    match = re.search(r"\$?\b([A-Z][A-Z0-9]{1,10})\b", request or "")
    words = [w for w in re.findall(r"\$?\b([A-Z][A-Z0-9]{1,10})\b", request or "") if w not in ("BINANCE", "USDT", "USDC", "SPOT", "CEX")]
    if not words:
        raise ValueError("Name the token, e.g. 'is BONK listed on Binance'")
    symbol = words[-1]
    pairs = binance_pairs(symbol)
    lines = ["# Binance listing", f"**Provider**: Binance public API · **Checked**: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} · **Token**: {symbol}", ""]
    if not pairs:
        lines += [f"**{symbol} is not listed on Binance spot.** No spot pair has {symbol} (or 1000{symbol}) as its base asset. "
                  "Futures and Binance Alpha are separate lists and are not checked here."]
    else:
        trading = [p for p in pairs if p.get("status") == "TRADING"]
        lines += [f"**{symbol} is listed on Binance spot** with {len(trading)} trading pair(s)" + (f" and {len(pairs) - len(trading)} halted" if len(pairs) > len(trading) else "") + ".", "",
                  "| Pair | Status | Quote |", "|---|---|---|"]
        for p in sorted(pairs, key=lambda p: (_QUOTES.index(p["quoteAsset"]) if p.get("quoteAsset") in _QUOTES else 99)):
            lines.append(f"| {p.get('symbol')} | {p.get('status')} | {p.get('quoteAsset')} |")
    lines += ["", "Source: [Binance exchangeInfo](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-endpoints)"]
    return "\n".join(lines)


class ExchangeListingsProvider:
    name = "binance"

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "binance_spot_listing", self.name, ("listing_events", "market_data"), binance_listing,
            matches=lambda request: bool(LISTING_ASK.search(request or "")),
            keywords=("binance", "listed on binance", "binance spot", "binance pair"),
            chains=(), cache_ttl_seconds=600, priority=12, spec=TOOL_SPECS.get("binance_spot_listing"),
            description="Whether a token is listed on Binance spot and which pairs trade, from Binance's own symbol list",
        ))
