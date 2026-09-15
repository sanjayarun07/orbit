"""Chain-agnostic token resolution by symbol.

Resolves a bare ticker to concrete (chain, address) candidates across every
chain via DEX Screener's search, ranked by liquidity. Callers auto-resolve a
clear winner and otherwise ask the user to disambiguate -- we never silently
pick among comparable same-symbol tokens on different chains.
"""

from __future__ import annotations

import logging

import httpx

from app.dexscreener_tools import _get
from app.settings import settings

logger = logging.getLogger(__name__)

# A dominant candidate resolves automatically; comparable ones are ambiguous.
_WINNER_LIQUIDITY_FLOOR = 50_000.0
_WINNER_LIQUIDITY_RATIO = 5.0

# Bitquery is the primary resolver on EVM chains: it ranks same-ticker tokens by
# real on-chain trade volume + distinct traders, cleanly separating the canonical
# token from copycats where DEX Screener's free text search cannot. Solana keeps
# using Jupiter's verified registry (free + canonical), so it is not listed here.
_BITQUERY_EVM_NETWORKS = {
    "ethereum": "eth", "base": "base", "arbitrum": "arbitrum",
    "bsc": "bsc", "bnb": "bsc", "polygon": "matic", "avalanche": "avalanche",
    "optimism": "optimism",
}
# A realtime-window candidate needs at least this many distinct traders to count
# as a real token rather than dust/wash noise.
_BITQUERY_MIN_TRADERS = 5

_BITQUERY_SYMBOL_QUERY = (
    "query($network: evm_network!, $sym: String!) {"
    " EVM(network: $network, dataset: realtime) {"
    " DEXTradeByTokens(limit: {count: 8}, orderBy: {descendingByField: \"vol\"},"
    " where: {Trade: {Currency: {Symbol: {is: $sym}}}, TransactionStatus: {Success: true}}) {"
    " Trade { Currency { Name Symbol SmartContract } }"
    " vol: sum(of: Trade_Side_AmountInUSD)"
    " traders: count(distinct: Trade_Sender) } } }"
)


def bitquery_evm_candidates(symbol: str, chain: str) -> list[dict]:
    """Same-ticker EVM tokens on `chain`, ranked by realtime trade volume, from
    Bitquery. Each candidate carries `liquidity_usd` = traded volume (the ranking
    magnitude, so `clear_winner` works unchanged) plus `traders`. Empty when
    Bitquery is unconfigured, the chain is unsupported, or nothing qualifies."""
    symbol = symbol.strip().lstrip("$")
    network = _BITQUERY_EVM_NETWORKS.get((chain or "").strip().lower())
    if not symbol or not network or not settings.bitquery_api_key:
        return []
    try:
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.post(
                settings.bitquery_graphql_url,
                headers={"Authorization": f"Bearer {settings.bitquery_api_key}"},
                json={"query": _BITQUERY_SYMBOL_QUERY, "variables": {"network": network, "sym": symbol}},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("bitquery symbol resolve failed for %s on %s: %s", symbol, chain, exc)
        return []
    if payload.get("errors"):
        logger.warning("bitquery symbol resolve error for %s on %s: %s", symbol, chain, payload["errors"][:1])
        return []
    rows = (((payload.get("data") or {}).get("EVM") or {}).get("DEXTradeByTokens")) or []
    candidates: list[dict] = []
    for row in rows:
        currency = (row.get("Trade") or {}).get("Currency") or {}
        address = str(currency.get("SmartContract") or "")
        if (currency.get("Symbol") or "").upper() != symbol.upper() or not address:
            continue
        try:
            traders = int(row.get("traders") or 0)
        except (TypeError, ValueError):
            traders = 0
        if traders < _BITQUERY_MIN_TRADERS:
            continue
        try:
            volume = float(row.get("vol") or 0)
        except (TypeError, ValueError):
            volume = 0.0
        candidates.append({
            "chain": chain,
            "address": address,
            "name": currency.get("Name") or symbol,
            "symbol": currency.get("Symbol") or symbol,
            "liquidity_usd": volume,
            "traders": traders,
        })
    return candidates


def token_candidates(symbol: str, chains: tuple[str, ...] = ()) -> list[dict]:
    """Distinct tokens matching `symbol` across chains, richest liquidity first.

    Each candidate: {chain, address, name, symbol, liquidity_usd}. When `chains`
    is given, only those chains are returned.
    """
    symbol = symbol.strip().lstrip("$")
    if not symbol:
        return []
    data = _get("/latest/dex/search", {"q": symbol})
    pairs = (data.get("pairs") or []) if isinstance(data, dict) else []
    by_key: dict[tuple[str, str], dict] = {}
    for pair in pairs:
        # The searched symbol can be either side of a pair -- a base token
        # (PEPE/WETH) or, very commonly for stablecoins, the quote (WETH/USDC).
        # Match whichever side carries it so quote-side tokens resolve too.
        for side_key in ("baseToken", "quoteToken"):
            side = pair.get(side_key) or {}
            if (side.get("symbol") or "").upper() != symbol.upper():
                continue
            chain = str(pair.get("chainId") or "")
            address = str(side.get("address") or "")
            if not chain or not address:
                continue
            try:
                liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
            except (TypeError, ValueError):
                liquidity = 0.0
            key = (chain, address)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = {
                    "chain": chain,
                    "address": address,
                    "name": side.get("name") or symbol,
                    "symbol": side.get("symbol") or symbol,
                    # Sum across pools: a token's real depth is spread over many
                    # pools, so max-per-pool would let a single deep pool of an
                    # unrelated same-ticker token outrank a blue-chip.
                    "liquidity_usd": liquidity,
                }
            else:
                existing["liquidity_usd"] += liquidity
            break  # counted on the matching side; don't double-count
    candidates = sorted(by_key.values(), key=lambda c: c["liquidity_usd"], reverse=True)
    if chains:
        wanted = {c.lower() for c in chains}
        candidates = [c for c in candidates if c["chain"].lower() in wanted]
    return candidates


def clear_winner(candidates: list[dict]) -> dict | None:
    """The unambiguous choice, or None when the top candidates are comparable
    (then the caller must ask the user)."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    top, second = candidates[0], candidates[1]
    if top["liquidity_usd"] >= _WINNER_LIQUIDITY_FLOOR and top["liquidity_usd"] >= _WINNER_LIQUIDITY_RATIO * max(second["liquidity_usd"], 1.0):
        return top
    return None
