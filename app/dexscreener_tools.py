"""Read-only DEX Screener provider adapters with compact, cited output."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

import httpx

from app.settings import settings
from app.tool_results import compact_tool_result


_BASE = "https://api.dexscreener.com"
_CHAINS = ("solana", "base", "ethereum", "arbitrum", "bsc", "polygon", "avalanche", "sui")
_ADDRESS = re.compile(
    r"0x(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})(?![0-9a-fA-F])|"
    r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])"
)


def _get(path: str, params: dict | None = None) -> Any:
    with httpx.Client(timeout=settings.provider_request_timeout_seconds, follow_redirects=True) as client:
        response = client.get(
            f"{_BASE}{path}", params=params, headers={"User-Agent": "Orbit-Web3-Copilot/0.3"}
        )
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
    return f"${number:,.4f}" if abs(number) < 1 else f"${number:,.2f}"


def _chain(request: str) -> str | None:
    lowered = request.lower()
    return next((chain for chain in _CHAINS if re.search(rf"\b{chain}\b", lowered)), None)


def dexscreener_trending_metas(_request: str) -> str:
    """Return current crypto narratives ranked by DEX Screener market activity."""
    data = _get("/metas/trending/v1")
    rows = sorted(data if isinstance(data, list) else [], key=lambda item: float(item.get("volume") or 0), reverse=True)[:10]
    lines = [
        "# Trending crypto narratives",
        f"**Data freshness**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "| Narrative | Volume | Liquidity | Market cap | 24h change | Tokens |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in rows:
        change = (item.get("marketCapChange") or {}).get("h24")
        lines.append(
            f"| {item.get('name') or item.get('slug') or 'Unknown'} | {_money(item.get('volume'))} | "
            f"{_money(item.get('liquidity'))} | {_money(item.get('marketCap'))} | "
            f"{float(change):+.1f}% | {item.get('tokenCount') or 0} |" if change is not None else
            f"| {item.get('name') or item.get('slug') or 'Unknown'} | {_money(item.get('volume'))} | "
            f"{_money(item.get('liquidity'))} | {_money(item.get('marketCap'))} | — | {item.get('tokenCount') or 0} |"
        )
    lines.extend([
        "",
        f"Source: [DEX Screener trending metas]({_BASE}/metas/trending/v1)",
        "",
        "Narrative activity is a discovery signal, not a recommendation. Verify token-level liquidity and holder concentration before trading.",
    ])
    return compact_tool_result("\n".join(lines))


def dexscreener_latest_profiles(request: str) -> str:
    """Return recently published token profiles, optionally filtered by chain."""
    chain = _chain(request)
    data = _get("/token-profiles/latest/v1")
    rows = [item for item in (data if isinstance(data, list) else []) if not chain or item.get("chainId") == chain][:12]
    lines = [
        f"# Recent token profiles{f' on {chain.title()}' if chain else ''}",
        "These are recently published profiles, not a complete chronological list of newly created pairs.",
        "",
    ]
    for item in rows:
        address = item.get("tokenAddress") or "Unknown"
        url = item.get("url") or f"https://dexscreener.com/{item.get('chainId')}/{address}"
        description = " ".join(str(item.get("description") or "").split())[:180]
        lines.append(f"- [{address[:8]}…{address[-6:]}]({url}) · **{item.get('chainId') or 'unknown'}**{f' — {description}' if description else ''}")
    if not rows:
        lines.append(f"- No recent profiles for **{chain or 'the requested scope'}** were present in this API snapshot.")
    lines.extend([
        "",
        f"Source: [DEX Screener latest token profiles]({_BASE}/token-profiles/latest/v1)",
        "",
        "Recent profiles may be promoted, unaudited, or low-liquidity. Confirm the exact contract before taking action.",
    ])
    return compact_tool_result("\n".join(lines))


def dexscreener_pair_search(request: str) -> str:
    """Search DEX pairs by token name, symbol, contract, or natural-language query."""
    address = _ADDRESS.search(request)
    named = re.search(r"\b([A-Za-z][A-Za-z0-9._-]{1,15})\s+(?:token|coin)\b", request, re.I)
    subject = address.group(0) if address else named.group(1) if named else None
    if subject is None:
        words = [word for word in re.findall(r"[A-Za-z0-9._-]+", request) if word.lower() not in {
            "analyze", "analyse", "show", "check", "price", "of", "for", "on", "the", "token", "coin", "liquidity", "volume", "and", "latest", "current",
        } and word.lower() not in _CHAINS]
        subject = " ".join(words)
    if not subject:
        raise ValueError("Specify the token name, symbol or contract to search")
    data = _get("/latest/dex/search", {"q": subject})
    chain = _chain(request)
    pairs = data.get("pairs") or [] if isinstance(data, dict) else []
    if chain:
        pairs = [pair for pair in pairs if pair.get("chainId") == chain]
    pairs = [pair for pair in pairs if any(
        (subject == str((pair.get(side) or {}).get(field) or "")
         if address and not subject.startswith("0x") else
         subject.casefold() == str((pair.get(side) or {}).get(field) or "").casefold())
        for side in ("baseToken", "quoteToken") for field in ("symbol", "name", "address")
    )]
    pairs = sorted(pairs, key=lambda pair: float((pair.get("liquidity") or {}).get("usd") or 0), reverse=True)[:10]
    lines = [
        "# DEX pair results",
        f"**Data freshness**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "| Pair | Chain / DEX | Price | 24h volume | Liquidity | 24h change |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for pair in pairs:
        base = (pair.get("baseToken") or {}).get("symbol") or "?"
        quote = (pair.get("quoteToken") or {}).get("symbol") or "?"
        change = (pair.get("priceChange") or {}).get("h24")
        lines.append(
            f"| [{base}/{quote}]({pair.get('url') or '#'}) | {pair.get('chainId') or '?'} / {pair.get('dexId') or '?'} | "
            f"{_money(pair.get('priceUsd'))} | {_money((pair.get('volume') or {}).get('h24'))} | "
            f"{_money((pair.get('liquidity') or {}).get('usd'))} | {f'{float(change):+.1f}%' if change is not None else '—'} |"
        )
    if not pairs:
        lines.append("| No matching liquid pairs found | — | — | — | — | — |")
    lines.extend([
        "",
        "Search results can include duplicate symbols and unofficial contracts. Verify chain and contract address before trading.",
    ])
    return compact_tool_result("\n".join(lines))


def dexscreener_token_pairs(request: str) -> str:
    """Analyze all known DEX pairs for an exact token contract or mint."""
    address = _ADDRESS.search(request)
    chain = _chain(request)
    if not address or not chain:
        raise ValueError("Token pair analysis requires both an exact contract/mint and chain")
    data = _get(f"/token-pairs/v1/{chain}/{address.group(0)}")
    return dexscreener_pair_search_from_pairs(data if isinstance(data, list) else [])


def dexscreener_pair_search_from_pairs(pairs: list[dict]) -> str:
    pairs = sorted(pairs, key=lambda pair: float((pair.get("liquidity") or {}).get("usd") or 0), reverse=True)[:10]
    lines = ["# Token liquidity venues", "", "| Pair | Chain / DEX | Price | 24h volume | Liquidity |", "|---|---|---:|---:|---:|"]
    for pair in pairs:
        base = (pair.get("baseToken") or {}).get("symbol") or "?"
        quote = (pair.get("quoteToken") or {}).get("symbol") or "?"
        lines.append(
            f"| [{base}/{quote}]({pair.get('url') or '#'}) | {pair.get('chainId') or '?'} / {pair.get('dexId') or '?'} | "
            f"{_money(pair.get('priceUsd'))} | {_money((pair.get('volume') or {}).get('h24'))} | {_money((pair.get('liquidity') or {}).get('usd'))} |"
        )
    return compact_tool_result("\n".join(lines))
