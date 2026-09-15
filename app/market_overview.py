"""Compact crypto "market overview" card -- a Minara-style dashboard composed
from live sources we already use (CoinGecko global + simple price + markets +
trending, DeFiLlama chain TVL/DEX volume, Alternative.me Fear & Greed). One tight
card: core quotes, market pulse, top movers, trending. Deterministic, cached, and
degrades section-by-section so one dead source never blanks the whole card.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
import time
from typing import Any

import httpx

from app.settings import settings
from app.tool_results import compact_tool_result

_CG = settings.coingecko_base_url.rstrip("/")
_GLOBAL_URL = f"{_CG}/global"
_SIMPLE_URL = f"{_CG}/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true"
_MARKETS_URL = f"{_CG}/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=1&price_change_percentage=24h"
_TRENDING_URL = f"{_CG}/search/trending"
_FNG_URL = "https://api.alternative.me/fng/?limit=1"
_CHAINS_TVL_URL = "https://api.llama.fi/v2/chains"
_DEX_TOTAL_URL = "https://api.llama.fi/overview/dexs?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"

_cache: dict[str, tuple[float, str]] = {}
_cache_lock = Lock()
_CACHE_TTL = 60.0


def _cg_headers() -> dict[str, str]:
    headers = {"User-Agent": "Orbit-Web3-Copilot/0.2"}
    if settings.coingecko_api_key:
        # CoinGecko accepts the demo key header on the public base and the pro
        # header on the pro base; sending the matching one, harmless otherwise.
        key_header = "x-cg-pro-api-key" if "pro-api" in _CG else "x-cg-demo-api-key"
        headers[key_header] = settings.coingecko_api_key
    return headers


def _get_json(url: str) -> Any:
    headers = _cg_headers() if url.startswith(_CG) else {"User-Agent": "Orbit-Web3-Copilot/0.2"}
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


def _money(value: float | int | None) -> str:
    if value is None:
        return "—"
    number = float(value)
    if abs(number) >= 1_000_000_000_000:
        return f"${number / 1_000_000_000_000:.2f}T"
    if abs(number) >= 1_000_000_000:
        return f"${number / 1_000_000_000:.2f}B"
    if abs(number) >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    if abs(number) >= 1_000:
        return f"${number / 1_000:.1f}K"
    return f"${number:,.4f}" if abs(number) < 1 else f"${number:,.2f}"


def _pct(value: float | int | None) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.2f}%"


def _core_quotes(simple: dict, global_data: dict) -> list[str]:
    ids = (("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL"))
    rows = ["## Core quotes", "| Asset | Price | 24h |", "|---|---:|---:|"]
    any_row = False
    for coin_id, label in ids:
        coin = (simple or {}).get(coin_id) or {}
        if coin.get("usd") is not None:
            any_row = True
            rows.append(f"| {label} | {_money(coin['usd'])} | {_pct(coin.get('usd_24h_change'))} |")
    total = (((global_data or {}).get("data") or {}).get("total_market_cap") or {}).get("usd")
    mcap_change = ((global_data or {}).get("data") or {}).get("market_cap_change_percentage_24h_usd")
    if total is not None:
        any_row = True
        rows.append(f"| Total market cap | {_money(total)} | {_pct(mcap_change)} |")
    return rows if any_row else []


def _market_pulse(global_data: dict, fng: dict, tvl_chains: list, dex_total: dict) -> list[str]:
    lines: list[str] = []
    dominance = ((global_data or {}).get("data") or {}).get("market_cap_percentage") or {}
    fng_row = ((fng or {}).get("data") or [{}])[0] if isinstance((fng or {}).get("data"), list) else {}
    if fng_row.get("value"):
        lines.append(f"- **Fear & Greed**: {fng_row.get('value')} · {fng_row.get('value_classification', '—')}")
    if dominance.get("btc") is not None:
        eth = f" · **ETH**: {dominance['eth']:.1f}%" if dominance.get("eth") is not None else ""
        lines.append(f"- **BTC dominance**: {dominance['btc']:.1f}%{eth}")
    if (dex_total or {}).get("total24h") is not None:
        lines.append(f"- **All-chain DEX volume (24h)**: {_money(dex_total['total24h'])} ({_pct(dex_total.get('change_1d'))} d/d)")
    if isinstance(tvl_chains, list) and tvl_chains:
        top = sorted((c for c in tvl_chains if isinstance(c, dict) and c.get("tvl")), key=lambda c: c["tvl"], reverse=True)[:4]
        if top:
            lines.append("- **Top chains by DeFi TVL**: " + ", ".join(f"{c.get('name')} {_money(c.get('tvl'))}" for c in top))
    return (["## Market pulse", *lines] if lines else [])


def _top_movers(markets: list) -> list[str]:
    if not isinstance(markets, list) or not markets:
        return []
    ranked = [c for c in markets if isinstance(c, dict) and c.get("price_change_percentage_24h") is not None]
    if not ranked:
        return []
    ranked.sort(key=lambda c: c["price_change_percentage_24h"], reverse=True)
    gainers = ranked[:5]
    losers = ranked[-5:][::-1]

    def fmt(coins: list) -> str:
        return ", ".join(f"{(c.get('symbol') or '?').upper()} {_pct(c.get('price_change_percentage_24h'))}" for c in coins)

    return [
        "## Top movers (24h, among top-250 by market cap)",
        f"- **Gainers**: {fmt(gainers)}",
        f"- **Losers**: {fmt(losers)}",
    ]


def _trending(trending: dict) -> list[str]:
    coins = ((trending or {}).get("coins") or [])
    names = []
    for entry in coins[:7]:
        item = entry.get("item") or {}
        symbol = item.get("symbol")
        if symbol:
            names.append(symbol.upper())
    return ["## Trending (CoinGecko search)", "- " + ", ".join(names)] if names else []


def _build(query: str) -> str:
    urls = {
        "global": _GLOBAL_URL, "simple": _SIMPLE_URL, "markets": _MARKETS_URL,
        "trending": _TRENDING_URL, "fng": _FNG_URL, "tvl": _CHAINS_TVL_URL, "dex": _DEX_TOTAL_URL,
    }
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(urls)) as executor:
        futures = {executor.submit(_get_json, url): key for key, url in urls.items()}
        for future, key in list(futures.items()):
            try:
                results[key] = future.result()
            except Exception:
                results[key] = None

    lines = [
        "# Crypto Market Overview",
        f"**As of** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    for section in (
        _core_quotes(results.get("simple") or {}, results.get("global") or {}),
        _market_pulse(results.get("global") or {}, results.get("fng") or {}, results.get("tvl") or [], results.get("dex") or {}),
        _top_movers(results.get("markets") or []),
        _trending(results.get("trending") or {}),
    ):
        if section:
            lines.extend(["", *section])

    if len(lines) <= 2:  # only the header rendered -- every source failed
        return compact_tool_result(
            "# Crypto Market Overview\n\nMarket data sources are temporarily unavailable. Please try again shortly."
        )
    lines.extend([
        "",
        "Sources: CoinGecko · DeFiLlama · Alternative.me. Data is a market snapshot, not advice; "
        "verify before trading.",
    ])
    return compact_tool_result("\n".join(lines))


def crypto_market_overview(query: str = "") -> str:
    """Compact live crypto market-overview card, cached for 60s across callers."""
    with _cache_lock:
        cached = _cache.get("overview")
        if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL:
            return cached[1]
    output = _build(query)
    with _cache_lock:
        _cache["overview"] = (time.monotonic(), output)
    return output
