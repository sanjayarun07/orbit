"""Which crypto asset a ticker means, by the listing the market uses.

User (2026-09-18): "OPEN token should take only crypto assets in the
result; it is fetching Open Stablecoin Index." DEX Screener lists every
contract that ever had a pool, so a ticker's namesakes are mostly dust and
index wrappers. CoinGecko's listing with a market-cap rank is what a crypto
user means by "the OPEN token": OpenLedger is ranked 673, the next OPEN
1,963, the index 4,991. When one listed coin leads clearly, that is the
token; when two are close (Monad and MON Protocol), the question names
those two -- never the pool dust.
"""
from __future__ import annotations

import logging
import threading
import time

from app.token_pages import _cg, _PLATFORMS, _liquid_chain

logger = logging.getLogger(__name__)

_TTL = 3600.0
_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = threading.Lock()
# The leader must be at least this much better ranked than the runner-up
# (rank 673 vs 1,963 leads; 144 vs 1,360 leads; 567 vs unranked leads).
LEAD_RATIO = 2.0


def listed(symbol: str) -> list[dict]:
    """CoinGecko's coins with this exact symbol, best rank first:
    [{"id", "name", "symbol", "rank"}]; unranked last. Cached an hour."""
    key = (symbol or "").strip().lstrip("$").upper()
    if not key:
        return []
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    rows = _search(key)
    with _lock:
        _cache[key] = (now + _TTL, rows)
    return rows


def _search(key: str) -> list[dict]:
    """CoinGecko's coins whose symbol, or whose name, is exactly `key`
    ("SPX6900" is the name of the coin whose symbol is SPX, 2026-09-24)."""
    rows: list[dict] = []
    try:
        for coin in (_cg("/search", {"query": key}).get("coins") or []):
            symbol = str(coin.get("symbol") or "").upper()
            if (symbol == key or str(coin.get("name") or "").upper() == key) and coin.get("id"):
                rows.append({"id": coin["id"], "name": coin.get("name") or coin["id"], "symbol": symbol or key, "rank": coin.get("market_cap_rank")})
    except Exception:
        logger.info("coingecko symbol search failed for %s", key, exc_info=True)
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0))
    return rows


def leader(rows: list[dict]) -> dict | None:
    """The one listed coin a user means, or None when none leads clearly."""
    ranked = [r for r in rows if r.get("rank")]
    if not ranked:
        return None
    if len(ranked) == 1:
        return ranked[0]
    first, second = ranked[0], ranked[1]
    return first if second["rank"] >= first["rank"] * LEAD_RATIO else None


_contracts: dict[str, tuple[float, tuple[str, str] | None]] = {}


def contract(coin_id: str) -> tuple[str, str] | None:
    """(chain, address) for a listed coin on a chain the tools cover, the
    chain being where the contract trades most; None for a native coin or
    an unsupported chain. Cached an hour: the resolver now asks this for
    every ticker with a clear leader."""
    now = time.monotonic()
    with _lock:
        hit = _contracts.get(coin_id)
        if hit and hit[0] > now:
            return hit[1]
    result = _contract(coin_id)
    with _lock:
        _contracts[coin_id] = (now + _TTL, result)
    return result


def _contract(coin_id: str) -> tuple[str, str] | None:
    try:
        coin = _cg(f"/coins/{coin_id}", {"localization": "false", "tickers": "false", "community_data": "false", "developer_data": "false", "sparkline": "false"})
    except Exception:
        logger.info("coingecko coin %s failed", coin_id, exc_info=True)
        return None
    platforms = {_PLATFORMS[k]: v for k, v in (coin.get("platforms") or {}).items() if k in _PLATFORMS and v}
    if not platforms:
        return None
    chain = _liquid_chain(str(coin.get("symbol") or ""), platforms) or next(iter(platforms))
    return chain, platforms[chain]


def reset() -> None:
    with _lock:
        _cache.clear()
        _contracts.clear()
