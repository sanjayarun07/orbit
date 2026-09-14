"""Deterministic crypto market brief backed by live quantitative sources."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import Condition
import time
from typing import Any

import httpx

from app.tool_results import compact_tool_result
from app.provider_registry import get_provider_router

_CHAINS = ("solana", "bsc", "robinhood", "ethereum", "base", "arbitrum", "sui")
_DEX_URL = "https://api.llama.fi/overview/dexs/{chain}?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
_TOTAL_DEX_URL = "https://api.llama.fi/overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
_PRICE_URL = "https://coins.llama.fi/prices/current/coingecko:bitcoin,coingecko:ethereum"
_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
_cache_condition = Condition()
_cache: dict[str, tuple[float, str]] = {}
_building: set[str] = set()


def _requested_chains(query: str) -> tuple[str, ...]:
    lowered = query.lower()
    return tuple(chain for chain in _CHAINS if chain in lowered)


def _cache_key(query: str) -> str:
    return ",".join(_requested_chains(query)) or "global"


def _get_json(url: str) -> Any:
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        response = client.get(url, headers={"User-Agent": "Orbit-Web3-Copilot/0.2"})
        response.raise_for_status()
        return response.json()


def _money(value: float | int | None) -> str:
    if value is None:
        return "—"
    number = float(value)
    if abs(number) >= 1_000_000_000:
        return f"${number / 1_000_000_000:.2f}B"
    if abs(number) >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    if abs(number) >= 1_000:
        return f"${number / 1_000:.1f}K"
    return f"${number:,.2f}"


def _pct(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.1f}%"


def _boost_details(boosts: list[dict]) -> list[dict]:
    unique: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for boost in boosts:
        key = (str(boost.get("chainId") or ""), str(boost.get("tokenAddress") or "").lower())
        if not all(key) or key in seen:
            continue
        seen.add(key)
        unique.append(boost)
        if len(unique) == 5:
            break

    def fetch(boost: dict) -> dict | None:
        address = boost["tokenAddress"]
        chain = boost["chainId"]
        data = _get_json(f"https://api.dexscreener.com/latest/dex/tokens/{address}")
        pairs = [pair for pair in data.get("pairs") or [] if pair.get("chainId") == chain]
        if not pairs:
            return None
        pair = max(pairs, key=lambda item: float((item.get("liquidity") or {}).get("usd") or 0))
        return {
            "symbol": (pair.get("baseToken") or {}).get("symbol") or "Unknown",
            "chain": chain,
            "volume": (pair.get("volume") or {}).get("h24"),
            "liquidity": (pair.get("liquidity") or {}).get("usd"),
            "change": (pair.get("priceChange") or {}).get("h24"),
            "url": boost.get("url") or pair.get("url"),
        }

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(fetch, boost) for boost in unique]
        for future in as_completed(futures):
            try:
                item = future.result()
                if item:
                    results.append(item)
            except Exception:
                continue
    results.sort(key=lambda item: float(item.get("volume") or 0), reverse=True)
    return results[:4]


def _build_crypto_market_brief(query: str) -> str:
    requested_chains = _requested_chains(query)
    # When the request names a specific chain (e.g. "trending tokens on
    # solana"), scope every fetch to that chain instead of pulling the full
    # multi-chain snapshot -- a request naming one chain shouldn't come back
    # with an unfiltered all-chain table that buries the chain actually asked
    # about among six others.
    fetch_chains = requested_chains or _CHAINS
    urls = {"total": _TOTAL_DEX_URL, "prices": _PRICE_URL, "boosts": _BOOSTS_URL}
    urls.update({f"chain:{chain}": _DEX_URL.format(chain=chain) for chain in fetch_chains})
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_get_json, url): key for key, url in urls.items()}
        chain_label = ", ".join(chain.upper() for chain in requested_chains)
        scope = f" Focus specifically on {chain_label} -- exclude other chains." if requested_chains else ""
        narrative_request = (
            "Show trending crypto narratives and metas using current measurable DEX activity, "
            f"volume and liquidity. Exclude generic news and crime stories.{scope} "
            f"Tailor the brief to this user request: {query}"
        )
        narrative_future = executor.submit(
            get_provider_router().try_route, narrative_request, "token_discovery", requested_chains
        )
        for future in as_completed(futures):
            try:
                results[futures[future]] = future.result()
            except Exception:
                continue
        try:
            narrative_result = narrative_future.result()
            narratives = (
                narrative_result.output
                if narrative_result is not None
                else "Narrative search is temporarily unavailable."
            )
        except Exception:
            narratives = "Narrative search is temporarily unavailable."

    lines = [
        f"# {chain_label} crypto market brief" if requested_chains else "# Crypto market brief",
        f"**Data freshness**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    coins = (results.get("prices") or {}).get("coins") or {}
    total = results.get("total") or {}
    if coins or total:
        lines.extend(["", "## Market pulse"])
        for coin_id, label in (("coingecko:bitcoin", "Bitcoin (BTC)"), ("coingecko:ethereum", "Ethereum (ETH)")):
            coin = coins.get(coin_id) or {}
            if coin.get("price") is not None:
                lines.append(f"- **{label}**: {_money(coin['price'])} ([DefiLlama price feed]({_PRICE_URL}))")
        if total.get("total24h") is not None:
            lines.append(
                f"- **All-chain DEX volume**: {_money(total['total24h'])} in 24h "
                f"({_pct(total.get('change_1d'))} day/day; {_pct(total.get('change_7d'))} week/week) "
                f"([DefiLlama]({_TOTAL_DEX_URL}))"
            )

    chain_rows = []
    for chain in fetch_chains:
        data = results.get(f"chain:{chain}") or {}
        if data.get("total24h") is not None:
            chain_rows.append((chain, data))
    chain_rows.sort(key=lambda item: float(item[1].get("total24h") or 0), reverse=True)
    if chain_rows:
        table_title = f"## {chain_label} DEX volume" if requested_chains else "## Where the volume is"
        lines.extend(["", table_title, "| Chain | DEX volume (24h) | 1d change | 7d change |", "|---|---:|---:|---:|"])
        for chain, data in chain_rows:
            source = _DEX_URL.format(chain=chain)
            lines.append(
                f"| [{chain.upper()}]({source}) | {_money(data.get('total24h'))} | "
                f"{_pct(data.get('change_1d'))} | {_pct(data.get('change_7d'))} |"
            )

    lines.extend(["", "## Narratives and protocols", narratives])

    boosts = results.get("boosts") or []
    if requested_chains:
        boosts = [boost for boost in boosts if str(boost.get("chainId") or "").lower() in requested_chains]
    promoted = _boost_details(boosts)
    if promoted:
        lines.extend([
            "",
            f"## Promoted {chain_label} tokens" if requested_chains else "## Promoted-token attention",
            "These are currently boosted on DEX Screener. Boosts measure paid attention—not quality or organic demand.",
        ])
        for item in promoted:
            lines.append(
                f"- [{item['symbol']}]({item['url']}) on **{item['chain']}**: "
                f"{_money(item.get('volume'))} 24h volume, {_money(item.get('liquidity'))} liquidity, "
                f"{_pct(item.get('change'))} price change."
            )

    lines.extend([
        "",
        "## Risks",
        "New and promoted tokens can have shallow liquidity, concentrated ownership, mutable contracts, and extreme price swings. Treat attention as a discovery signal, not a recommendation, and verify liquidity and contract metadata before trading.",
        "",
        "## If you want, I can:",
        "- Show new token launches on a chain with a new-pairs view.",
        (f"- Pull the same brief for another chain (Solana, Base, Robinhood, Arbitrum, ...) instead of {chain_label}."
         if requested_chains else
         "- Pull live trending tokens and gainers for Solana, Base, Robinhood, Arbitrum, or another supported chain."),
        "- Show tokenized-stock leaders and their current activity.",
        "",
        "**Note**: Launchpad tokens and DEX Screener-boosted coins are often extremely volatile and low-liquidity. Verify on-chain liquidity, holder concentration, and contract metadata before trading. I can run those checks for any specific token you choose.",
    ])
    return compact_tool_result("\n".join(lines))


def crypto_market_brief(query: str) -> str:
    """Return a cached live market brief and coalesce concurrent refreshes."""
    key = _cache_key(query)
    with _cache_condition:
        cached = _cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < 60:
            return cached[1]
        if key in _building:
            _cache_condition.wait_for(lambda: key not in _building, timeout=30)
            cached = _cache.get(key)
            if cached is not None:
                return cached[1]
        _building.add(key)
    try:
        value = _build_crypto_market_brief(query)
        with _cache_condition:
            _cache[key] = (time.monotonic(), value)
            while len(_cache) > 16:
                oldest = min(_cache, key=lambda item: _cache[item][0])
                _cache.pop(oldest, None)
        return value
    finally:
        with _cache_condition:
            _building.discard(key)
            _cache_condition.notify_all()
