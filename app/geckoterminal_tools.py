"""GeckoTerminal (CoinGecko DEX API) adapters: real per-chain trending and
newly-created pools.

This is the correct data source for "trending tokens on <chain>" and "new
pairs on <chain>": unlike DEX Screener's public API (whose free profiles/boosts
feeds are curated, sparse, and often miss whole chains like Base), GeckoTerminal
exposes comprehensive trending_pools/new_pools per network for every chain this
app supports. Public and keyless (free tier ~30 req/min).
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

import httpx

from app.tool_results import compact_tool_result

_BASE = "https://api.geckoterminal.com/api/v2"

# App chain-name -> GeckoTerminal network id. Launchpads resolve to their chain.
_NETWORK = {
    "ethereum": "eth", "eth": "eth",
    "solana": "solana", "base": "base", "arbitrum": "arbitrum",
    "bsc": "bsc", "bnb": "bsc",
    "polygon": "polygon_pos", "matic": "polygon_pos",
    "avalanche": "avax", "avax": "avax",
    "sui": "sui-network",
    "robinhood": "robinhood", "hyperliquid": "hyperliquid",
    "pump.fun": "solana", "pumpfun": "solana", "pump fun": "solana",
    "letsbonk": "solana", "bonk.fun": "solana", "bonkfun": "solana", "moonshot": "solana",
}
_NEW = re.compile(r"\b(?:new|newest|just\s+launched|recently\s+launched|fresh|latest)\b", re.IGNORECASE)
_TRENDING = re.compile(r"\b(?:trending|hot|hottest|top|popular|biggest|moving)\b", re.IGNORECASE)


def _network(request: str) -> tuple[str, str] | None:
    lowered = request.lower()
    for alias, network in _NETWORK.items():
        if alias in lowered:
            # Return the display label (the alias, title-cased) and the gt id.
            label = alias.title() if alias not in {"eth", "bnb", "avax", "bsc"} else {"eth": "Ethereum", "bnb": "BNB Chain", "avax": "Avalanche", "bsc": "BNB Chain"}[alias]
            return label, network
    return None


def _get(path: str) -> Any:
    with httpx.Client(timeout=20, headers={"Accept": "application/json", "User-Agent": "Orbit-Web3-Copilot/0.3"}) as client:
        response = client.get(f"{_BASE}{path}")
        response.raise_for_status()
        return response.json()


def _money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if abs(number) >= 1_000_000_000:
        return f"${number / 1_000_000_000:.2f}B"
    if abs(number) >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    if abs(number) >= 1_000:
        return f"${number / 1_000:.1f}K"
    if abs(number) >= 1:
        return f"${number:,.2f}"
    if number == 0:
        return "$0"
    import math
    decimals = 3 - math.floor(math.log10(abs(number)))                # four significant figures, plain decimals: $0.0000063, not $0.0000
    return f"${number:.{min(decimals, 12)}f}"


def _age(created: str | None) -> str:
    if not created:
        return "—"
    try:
        then = datetime.fromisoformat(created.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - then
        hours = delta.total_seconds() / 3600
        return f"{hours:.0f}h" if hours < 48 else f"{delta.days}d"
    except (ValueError, TypeError):
        return "—"


def geckoterminal_pools(request: str) -> str:
    """Trending or newly-created pools on a chain/launchpad, with live price,
    volume, liquidity, and pool age. Picks new vs trending from the wording.
    """
    resolved = _network(request)
    if resolved is None:
        raise ValueError("Name a chain or launchpad (Solana, Base, pump.fun, ...) to list its pools")
    label, network = resolved
    want_new = bool(_NEW.search(request)) and not _TRENDING.search(request)
    kind = "new_pools" if want_new else "trending_pools"
    payload = _get(f"/networks/{network}/{kind}?page=1")
    pools = payload.get("data") if isinstance(payload, dict) else None
    pools = pools or []

    heading = f"{'Newly-created' if want_new else 'Trending'} pools on {label}"
    lines = [
        f"# {heading}",
        f"**Data freshness**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · GeckoTerminal",
        "",
        "| Pool | Price | 24h volume | Liquidity | 24h price change | Age |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for pool in pools[:12]:
        attr = pool.get("attributes") or {}
        name = attr.get("name") or "?"
        price = attr.get("base_token_price_usd")
        volume = (attr.get("volume_usd") or {}).get("h24")
        liquidity = attr.get("reserve_in_usd")
        change = (attr.get("price_change_percentage") or {}).get("h24")
        age = _age(attr.get("pool_created_at"))
        address = attr.get("address") or ""
        url = f"https://www.geckoterminal.com/{network}/pools/{address}" if address else "#"
        lines.append(
            f"| [{name}]({url}) | {_money(price)} | {_money(volume)} | {_money(liquidity)} | "
            f"{f'{float(change):+.1f}%' if change not in (None, '') else '—'} | {age} |"
        )
    if not pools:
        lines.append("| No pools returned for this network right now | — | — | — | — | — |")
    lines.extend([
        "",
        f"Source: [GeckoTerminal {kind.replace('_', ' ')}]({_BASE}/networks/{network}/{kind})",
        "",
        "\"24h price change\" is the pool's base-token price move over 24 hours, not a change in volume. "
        "New/trending pools are often low-liquidity and volatile. Verify liquidity, holder "
        "concentration, and contract metadata before trading.",
    ])
    return compact_tool_result("\n".join(lines))
