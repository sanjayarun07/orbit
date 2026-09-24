"""A listed coin's own facts: supply, market cap, rank, all-time high.

Live, 2026-09-21: "What is the current total supply and circulating supply
of the $ZEC token?" was answered with a question -- which chain, the Solana
wrapper or the BNB one? Zcash is a native coin ranked ninth on CoinGecko;
its supply is a fact about the asset, not about any wrapped copy. Every
market tool Orbit had keyed off a contract address, so nothing could
answer the listed asset by name.

This tool answers from CoinGecko's coin page: price, market cap and rank,
fully diluted value, circulating / total / max supply, 24h-7d-30d change,
all-time high and low with dates, categories and the homepage. It matches a
listed-asset question (supply, market cap, FDV, rank, ATH, tokenomics) that
names a ticker CoinGecko lists with one clear leader, and the resolver
rewrites such a question with "(CoinGecko id <id>)" so no chain is ever
asked for. A native coin (no contract anywhere) resolves here too whatever
the question, since no on-chain tool can see it.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from datetime import datetime, timezone

from app import symbol_registry
from app.provider_router import NoData, ProviderRouter, ProviderTool
from app.token_pages import _cg
from app.tool_catalog import TOOL_SPECS

logger = logging.getLogger(__name__)

LISTED_ASK = re.compile(
    r"\b(?:supply|circulating|max\s+supply|total\s+supply|market\s*cap(?:italization)?|mcap|fdv|fully\s+diluted|"
    r"rank(?:ing|ed)?|ath\b|all[\s-]time\s+high|atl\b|all[\s-]time\s+low|halving|inflation|emission|tokenomics|"
    r"how\s+many\s+(?:tokens|coins)|listed\s+since|when\s+(?:did|was)\s+\S+\s+(?:launch|list))",
    re.IGNORECASE,
)
_COIN_ID = re.compile(r"\(CoinGecko id ([a-z0-9-]+)\)")
_TICKER = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b|\b([A-Z]{2,10})\b")
_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
_STOP = {"THE", "AND", "FOR", "OF", "WHAT", "WHATS", "IS", "ARE", "ATH", "ATL", "FDV", "MCAP", "USD", "USDT", "USDC", "TOKEN", "COIN", "SUPPLY", "RANK", "CURRENT", "TOTAL", "MAX"}

_TTL = 300.0
_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def coin_id_in(request: str) -> str | None:
    match = _COIN_ID.search(request or "")
    return match.group(1) if match else None


def ticker_in(request: str) -> str | None:
    from app.tequity import fuzzy_venue
    for match in _TICKER.finditer(request or ""):
        sym = (match.group(1) or match.group(2) or "").upper()
        if sym and sym not in _STOP and not (not match.group(1) and fuzzy_venue(sym.lower()) == "hyperliquid" and sym.lower() in ("hl",)):
            # "HL" is the venue's alias, not a coin ("Holy Liquid, rank 5389", expanded UI review 2026-09-24); $HL would still be a ticker
            return sym
    return None


_PRICE_ASK = re.compile(r"\b(?:price|quote|worth|trading\s+at|how\s+much|24h|change|market|chart|performance)\b", re.I)
_DERIVATIVES = re.compile(r"\b(?:funding|open\s+interest|\boi\b|perps?|perpetuals?|liquidations?|hyperliquid|hyperloquid|aster|leverage)\b", re.I)


def matches(request: str) -> bool:
    """A listed fact (supply, market cap, rank, ATH...) about a ticker with no
    address; or a price/market ask carrying the resolver's coin-id marker.
    Never a derivatives ask -- funding and open interest belong to the perps
    tools even when the marker is present."""
    text = request or ""
    if _DERIVATIVES.search(text):
        return False
    if coin_id_in(text):
        return bool(LISTED_ASK.search(text) or _PRICE_ASK.search(text))
    return bool(LISTED_ASK.search(text)) and ticker_in(text) is not None and not _ADDRESS.search(text)


def _num(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _usd(value: float | None, digits: int = 2) -> str:
    """Dollars readable at every scale: $1.72T, $25.33B, $1,496.00, $16.08,
    $0.00000333 (never 3.33e-06, live 2026-09-21)."""
    if value is None:
        return "—"
    if abs(value) >= 1e12:
        return f"${value / 1e12:,.2f}T"
    if abs(value) >= 1e9:
        return f"${value / 1e9:,.2f}B"
    if abs(value) >= 1e6:
        return f"${value / 1e6:,.2f}M"
    if abs(value) >= 10:
        return f"${value:,.2f}"
    if abs(value) >= 1:
        text = f"{value:,.{min(digits, 4)}f}".rstrip("0")
        return "$" + (text + "0" if text.endswith(".") or len(text.split(".")[-1]) < 2 else text)
    if value == 0:
        return "$0"
    decimals = 2 - math.floor(math.log10(abs(value)))
    return "$" + f"{value:.{min(decimals, 12)}f}".rstrip("0").rstrip(".")


def _amount(value: float | None) -> str:
    if value is None:
        return "not reported"
    if abs(value) >= 1e12:
        return f"{value / 1e12:,.3f}T"
    if abs(value) >= 1e9:
        return f"{value / 1e9:,.3f}B"
    if abs(value) >= 1e6:
        return f"{value / 1e6:,.3f}M"
    return f"{value:,.0f}"


def _pct(value: float | None) -> str:
    return f"{value:+.2f}%" if value is not None else "—"


def _date(value) -> str:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return "—"


def coin_snapshot(coin_id: str) -> str:
    """The card for one CoinGecko coin id."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(coin_id)
        if hit and hit[0] > now:
            return hit[1]
    coin = _cg(f"/coins/{coin_id}", {"localization": "false", "tickers": "false", "community_data": "false", "developer_data": "false", "sparkline": "false"})
    if not isinstance(coin, dict) or not coin.get("id"):
        raise NoData(f"CoinGecko has no coin {coin_id}")
    market = coin.get("market_data") or {}
    symbol = str(coin.get("symbol") or "").upper()
    name = coin.get("name") or coin_id
    circulating, total, maximum = _num(market.get("circulating_supply")), _num(market.get("total_supply")), _num(market.get("max_supply"))
    price = _num((market.get("current_price") or {}).get("usd"))
    mcap = _num((market.get("market_cap") or {}).get("usd"))
    fdv = _num((market.get("fully_diluted_valuation") or {}).get("usd"))
    ath = _num((market.get("ath") or {}).get("usd"))
    atl = _num((market.get("atl") or {}).get("usd"))
    platforms = {k: v for k, v in (coin.get("platforms") or {}).items() if k and v}
    lines = [
        f"# {name} ({symbol}) — listed asset",
        f"**Provider**: CoinGecko · **Rank**: #{coin.get('market_cap_rank') or '—'} · **Checked**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Supply",
        f"- **Circulating**: {_amount(circulating)} {symbol}" + (f" ({circulating / maximum * 100:.1f}% of max)" if circulating and maximum else ""),
        f"- **Total**: {_amount(total)} {symbol}",
        f"- **Max**: {_amount(maximum) if maximum else 'no cap reported'}" + (f" {symbol}" if maximum else ""),
        "",
        "## Market",
        f"- **Price**: {_usd(price, 4)} · **24h**: {_pct(_num(market.get('price_change_percentage_24h')))} · "
        f"**7d**: {_pct(_num(market.get('price_change_percentage_7d')))} · **30d**: {_pct(_num(market.get('price_change_percentage_30d')))}",
        f"- **Market cap**: {_usd(mcap)} · **Fully diluted**: {_usd(fdv)} · **24h volume**: {_usd(_num((market.get('total_volume') or {}).get('usd')))}",
        f"- **All-time high**: {_usd(ath, 4)} on {_date((market.get('ath_date') or {}).get('usd'))} "
        f"({_pct(_num((market.get('ath_change_percentage') or {}).get('usd')))} from it) · **All-time low**: {_usd(atl, 4)} on {_date((market.get('atl_date') or {}).get('usd'))}",
    ]
    categories = [c for c in (coin.get("categories") or []) if c][:5]
    if categories:
        lines += ["", f"**Categories**: {', '.join(categories)}"]
    if platforms:
        shown = ", ".join(f"{k}: `{str(v)[:10]}…`" for k, v in list(platforms.items())[:4])
        lines += [f"**Contracts**: {shown}" + (f" (+{len(platforms) - 4} more)" if len(platforms) > 4 else "")]
    else:
        lines += ["**Contracts**: none — a native coin on its own chain; wrapped copies on other chains are separate tokens."]
    home = next((u for u in ((coin.get("links") or {}).get("homepage") or []) if u), None)
    lines += ["", f"Source: [CoinGecko](https://www.coingecko.com/en/coins/{coin_id})" + (f" · [Project site]({home})" if home else ""),
              "Supply figures are CoinGecko's reported values; a max supply of 'no cap' means none is reported, not that none exists."]
    card = "\n".join(lines)
    with _lock:
        _cache[coin_id] = (now + _TTL, card)
    return card


def listed_coin_snapshot(request: str) -> str:
    """Router handler: the coin named by id in the request, else the clear
    CoinGecko leader for the ticker it names."""
    coin_id = coin_id_in(request)
    if coin_id is None:
        ticker = ticker_in(request)
        if ticker is None:
            raise ValueError("Name the coin, e.g. 'circulating supply of ZEC'")
        lead = symbol_registry.leader(symbol_registry.listed(ticker))
        if lead is None:
            raise NoData(f"CoinGecko lists no clear leader for the ticker {ticker}")
        coin_id = lead["id"]
    return coin_snapshot(coin_id)


def reset_for_test() -> None:
    with _lock:
        _cache.clear()


class ListedAssetProvider:
    name = "coingecko"

    def enabled(self) -> bool:
        return True

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "coingecko_coin_snapshot", self.name, ("market_data", "token_discovery"), listed_coin_snapshot,
            matches=matches,
            keywords=("circulating supply", "total supply", "max supply", "market cap", "fdv", "rank", "all-time high", "tokenomics"),
            chains=(), cache_ttl_seconds=300, priority=15, spec=TOOL_SPECS.get("coingecko_coin_snapshot"),
            description="A listed coin's supply, market cap, rank, FDV and all-time high from CoinGecko's coin page, by ticker or CoinGecko id",
        ))
