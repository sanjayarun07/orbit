"""Read-only DEX Screener provider adapters with compact, cited output."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

import httpx

from app.settings import settings
from app.tool_results import compact_tool_result


_BASE = "https://api.dexscreener.com"
_CHAINS = ("solana", "base", "ethereum", "arbitrum", "bsc", "polygon", "avalanche", "sui", "robinhood")
_ADDRESS = re.compile(
    r"0x(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})(?![0-9a-fA-F])|"
    r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])"
)
# Launchpads are not chains, but a "trending tokens on <launchpad>" request is
# unambiguously scoped to the launchpad's chain (pump.fun / letsbonk / bonk.fun
# / moonshot are Solana). Resolve them so boosted-token results are chain-
# filtered instead of falling back to an all-chain list.
_LAUNCHPAD_CHAIN = {
    "pump.fun": "solana", "pumpfun": "solana", "pump fun": "solana",
    "letsbonk": "solana", "bonk.fun": "solana", "bonkfun": "solana",
    "moonshot": "solana",
}


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
    if abs(number) >= 1:
        return f"${number:,.2f}"
    if number == 0:
        return "$0"
    import math
    decimals = 3 - math.floor(math.log10(abs(number)))                # four significant figures, plain decimals: $0.0000063, not $0.0000
    return f"${number:.{min(decimals, 12)}f}"


def _chain(request: str) -> str | None:
    lowered = request.lower()
    for alias, chain in _LAUNCHPAD_CHAIN.items():
        if alias in lowered:
            return chain
    return next((chain for chain in _CHAINS if re.search(rf"\b{chain}\b", lowered)), None)


def dexscreener_boosted_tokens(request: str) -> str:
    """Return the tokens currently getting the most paid attention (DEX Screener
    boosts), enriched with live price/volume/liquidity, optionally scoped to a
    chain or launchpad.

    This is the token-level answer to "trending tokens on <chain/launchpad>" --
    an actual ranked list of tokens, as opposed to dexscreener_trending_metas
    (which ranks narratives/metas) or crypto_market_brief (a broad synthesis).
    Boosts measure paid promotion, not organic quality -- stated in the output.
    """
    chain = _chain(request)
    data = _get("/token-boosts/top/v1")
    boosts = data if isinstance(data, list) else []
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for boost in boosts:
        cid = str(boost.get("chainId") or "")
        addr = str(boost.get("tokenAddress") or "")
        if chain and cid != chain:
            continue
        key = (cid, addr.lower())
        if not all(key) or key in seen:
            continue
        seen.add(key)
        unique.append(boost)
        if len(unique) == 10:
            break

    def enrich(boost: dict) -> dict | None:
        address, cid = boost["tokenAddress"], boost["chainId"]
        try:
            pairs = _get(f"/token-pairs/v1/{cid}/{address}")
        except Exception:
            return None
        pairs = [p for p in (pairs if isinstance(pairs, list) else []) if p.get("chainId") == cid]
        if not pairs:
            return None
        pair = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
        return {
            "symbol": (pair.get("baseToken") or {}).get("symbol") or "?",
            "chain": cid,
            "address": address,
            "price": pair.get("priceUsd"),
            "volume": (pair.get("volume") or {}).get("h24"),
            "liquidity": (pair.get("liquidity") or {}).get("usd"),
            "change": (pair.get("priceChange") or {}).get("h24"),
            "url": boost.get("url") or pair.get("url") or f"https://dexscreener.com/{cid}/{address}",
        }

    from concurrent.futures import ThreadPoolExecutor, as_completed
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        for future in as_completed([executor.submit(enrich, b) for b in unique]):
            try:
                item = future.result()
            except Exception:
                item = None
            if item:
                rows.append(item)
    rows.sort(key=lambda item: float(item.get("volume") or 0), reverse=True)

    scope = f" on {chain.title()}" if chain else ""
    lines = [
        f"# Trending tokens{scope}",
        f"**Data freshness**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · currently boosted on DEX Screener",
        "",
        "| Token | Chain | Price | 24h volume | Liquidity | 24h price change |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for item in rows:
        change = item.get("change")
        lines.append(
            f"| [{item['symbol']}]({item['url']}) | {item['chain']} | {_money(item.get('price'))} | "
            f"{_money(item.get('volume'))} | {_money(item.get('liquidity'))} | "
            f"{f'{float(change):+.1f}%' if change is not None else '—'} |"
        )
    if not rows:
        available = sorted({str(b.get("chainId")) for b in boosts if b.get("chainId")})
        if chain and available:
            lines.append(
                f"| No boosted tokens for **{chain.title()}** right now | — | — | — | — | — |"
            )
            lines.append("")
            lines.append(
                f"The boosts feed currently has entries for: **{', '.join(available)}** — this is a "
                "curated paid-promotion list, not every chain is represented at all times. "
                "Ask for one of those chains, or give an exact contract to inspect."
            )
        else:
            lines.append(f"| No boosted tokens for **{chain or 'the requested scope'}** in this snapshot | — | — | — | — | — |")
    lines.extend([
        "",
        f"Source: [DEX Screener token boosts]({_BASE}/token-boosts/top/v1)",
        "",
        "Boosts measure paid attention, not organic demand or quality. These are often new, "
        "low-liquidity, and volatile -- verify liquidity and holder concentration before trading.",
    ])
    return compact_tool_result("\n".join(lines))


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
        available = sorted({item.get("chainId") for item in (data if isinstance(data, list) else []) if item.get("chainId")})
        if chain and available:
            # Don't dead-end: this is a curated/promoted feed, not a complete
            # per-chain new-pairs index, and it is often skewed to whichever
            # chains are hot. Say which chains DO have entries so the user can
            # redirect instead of assuming an outage.
            lines.append(
                f"- DEX Screener's latest-profiles feed has **no {chain.title()} tokens right now** — "
                f"it currently lists newly-published/promoted tokens on: **{', '.join(available)}**."
            )
            lines.append(
                "- This free feed is a curated promoted list, not a complete new-pairs index for every "
                "chain. Ask for one of the chains above, or give an exact contract to inspect."
            )
        else:
            lines.append(f"- No recent profiles for **{chain or 'the requested scope'}** were present in this API snapshot.")
    lines.extend([
        "",
        f"Source: [DEX Screener latest token profiles]({_BASE}/token-profiles/latest/v1)",
        "",
        "Recent profiles may be promoted, unaudited, or low-liquidity. Confirm the exact contract before taking action.",
    ])
    return compact_tool_result("\n".join(lines))


def _rank_pairs(pairs: list[dict], limit: int = 10) -> list[dict]:
    """Rank by what actually trades. Decoy listings fake liquidity ($350M
    "USELESS/USDC" with $3.99 of volume) and would otherwise head the table;
    when any pair shows real volume, deep-liquidity pairs with none are dropped."""
    def _vol(pair):
        return float((pair.get("volume") or {}).get("h24") or 0)

    def _liq(pair):
        return float((pair.get("liquidity") or {}).get("usd") or 0)

    if any(_vol(p) >= 1_000 for p in pairs):
        pairs = [p for p in pairs if not (_liq(p) >= 1_000_000 and _vol(p) < 1_000)]
    return sorted(pairs, key=lambda pair: (_vol(pair), _liq(pair)), reverse=True)[:limit]


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
    pairs = _rank_pairs(pairs)
    lines = [
        "# DEX pair results",
        f"**Data freshness**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "| Pair | Chain / DEX | Priced token | Price | 24h volume | Liquidity | 24h price change |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for pair in pairs:
        base = (pair.get("baseToken") or {}).get("symbol") or "?"
        quote = (pair.get("quoteToken") or {}).get("symbol") or "?"
        change = (pair.get("priceChange") or {}).get("h24")
        lines.append(
            f"| [{base}/{quote}]({pair.get('url') or '#'}) | {pair.get('chainId') or '?'} / {pair.get('dexId') or '?'} | {base} | "
            f"{_money(pair.get('priceUsd'))} | {_money((pair.get('volume') or {}).get('h24'))} | "
            f"{_money((pair.get('liquidity') or {}).get('usd'))} | {f'{float(change):+.1f}%' if change is not None else '—'} |"
        )
    if not pairs:
        # An empty table is not an answer: raising lets the provider router try
        # the next tool (web research for a headline that merely mentions a coin)
        # instead of reporting "1 successful" with nothing in it.
        raise RuntimeError(f"DEX Screener found no liquid pairs for {subject!r}")
    lines.extend([
        "",
        "Price is the priced token's (the first in the pair) in USD; a row where your token is the second name prices the other token. "
        "\"24h price change\" is a price move, not volume growth. "
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
    pairs = _rank_pairs(pairs)
    lines = ["# Token liquidity venues", "", "| Pair | Chain / DEX | Priced token | Price | 24h volume | Liquidity |", "|---|---|---|---:|---:|---:|"]
    for pair in pairs:
        base = (pair.get("baseToken") or {}).get("symbol") or "?"
        quote = (pair.get("quoteToken") or {}).get("symbol") or "?"
        lines.append(
            f"| [{base}/{quote}]({pair.get('url') or '#'}) | {pair.get('chainId') or '?'} / {pair.get('dexId') or '?'} | {base} | "
            f"{_money(pair.get('priceUsd'))} | {_money((pair.get('volume') or {}).get('h24'))} | {_money((pair.get('liquidity') or {}).get('usd'))} |"
        )
    lines += ["", "Price is the priced token's (the first in the pair) in USD; a row where the requested token is the second name prices the other token."]
    return compact_tool_result("\n".join(lines))
