"""Optional first-party adapters for identity, risk, DeFi and wallet data.

All tools in this module are read-only.  Execution providers (Relay, Jupiter and
LI.FI) deliberately live outside the research router so a fallback can never
turn a research request into a transaction.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re

from app.routing import lexicon

from app.routing.entities import extract_chains
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.market_providers import VOLUME_RANKED, _address, _chain, _has_chain_and_address, _money, _snapshot, ATTENTION_WORD, BARE_TRENDING
from app.perplexity_tools import perplexity_available, perplexity_web_search
from app.provider_router import ProviderRouter, ProviderTool
from app.settings import settings
from app.tool_results import compact_tool_result


_EVM_CHAIN_IDS = {
    "ethereum": 1, "bsc": 56, "polygon": 137, "avalanche": 43114,
    "arbitrum": 42161, "base": 8453, "optimism": 10, "robinhood": 4663,
}
_CG_PLATFORMS = {
    "ethereum": "ethereum", "bsc": "binance-smart-chain", "polygon": "polygon-pos",
    "avalanche": "avalanche", "arbitrum": "arbitrum-one", "base": "base",
    "optimism": "optimistic-ethereum", "solana": "solana",
}
_GOLDRUSH_CHAINS = {
    "ethereum": "eth-mainnet", "bsc": "bsc-mainnet", "polygon": "matic-mainnet",
    "avalanche": "avalanche-mainnet", "arbitrum": "arbitrum-mainnet", "base": "base-mainnet",
    "optimism": "optimism-mainnet",
    # Solana balances (not transactions -- verified live: GoldRush's
    # Foundational API on Solana currently has exactly one working
    # endpoint, balances_v2; no transaction-history endpoint exists there).
    "solana": "solana-mainnet",
}
# GoldRush chains that support the balances_v2 endpoint on a non-EVM
# (base58) address -- today just Solana. Transactions stays EVM-only.
_GOLDRUSH_NON_EVM_CHAINS = {"solana"}
_SECURITY = lexicon.SECURITY          # the one security vocabulary (app/routing/lexicon.py)
_WALLET_ACTIVITY = re.compile(r"\b(?:wallet|transactions?|activity|history|transfers?)\b", re.I)
# CoinGecko's curated "ecosystem" category per chain -- the closest free,
# keyless proxy for "tokens native to/associated with this chain" their
# public API offers. Verified live: their own order=..._desc sort param is
# silently ignored on the free tier, so ranking is done client-side instead.
_GAINERS_CATEGORY = {
    "solana": "solana-ecosystem", "base": "base-ecosystem", "ethereum": "ethereum-ecosystem",
    "arbitrum": "arbitrum-ecosystem", "avalanche": "avalanche-ecosystem", "polygon": "polygon-ecosystem",
    "sui": "sui-ecosystem", "bsc": "binance-smart-chain",
}
_GAINERS_LOSERS = re.compile(
    r"\bgainers?\b|\blosers?\b|\b(?:top|biggest|best|worst)\s+(?:movers?|performers?)\b|\bwinners?\b"
    # Slang for the same ranking: "what spiked hardest today", "biggest drops".
    r"|\b(?:spiked|pumped|jumped|surged|mooned|dumped|crashed|tanked)\b"
    r"|\b(?:moved|up|down)\s+the\s+most\b|\bbiggest\s+(?:moves?|jumps?|drops?|dumps?|pumps?)\b", re.I
)
_PEGGED_OR_WRAPPED = re.compile(r"^(?:usd\w*|\w*usd[cdtes]?|dai|fdusd|pyusd|tusd|busd|frax|w(?:sol|eth|btc|bnb|avax|pol|matic)|steth|cbbtc|weth)$", re.I)
_LOSER_WORDS = re.compile(r"\blosers?\b|\bworst\b|\bdump(?:ing)?\b|\bdeclin\w*\b|\bfalling\b", re.I)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _evm_exact(request: str) -> bool:
    try:
        return _chain(request) in _EVM_CHAIN_IDS and _address(request).startswith("0x")
    except ValueError:
        return False


def _goldrush_balances_exact(request: str) -> bool:
    """Like _evm_exact, but also accepts a Solana chain + base58 address --
    GoldRush's balances_v2 endpoint covers Solana; its other endpoints
    (transactions) don't, so this is deliberately not used for those.
    """
    try:
        chain, address = _chain(request), _address(request)
    except ValueError:
        return False
    if chain not in _GOLDRUSH_CHAINS:
        return False
    if chain in _GOLDRUSH_NON_EVM_CHAINS:
        return not address.startswith("0x")
    return address.startswith("0x")


def _has_gainers_chain(request: str) -> bool:
    """A chain with a curated ecosystem category, or no chain at all: "what
    coins spiked hardest today" is a market-wide question the same endpoint
    answers without a category (the overview card's movers list is the same
    query). A chain WITHOUT a category is the one case the tool cannot serve."""
    try:
        return _chain(request) in _GAINERS_CATEGORY
    except ValueError:
        return True


class CoinGeckoProvider:
    name = "coingecko"

    def token(self, request: str) -> str:
        chain = _chain(request)
        platform = _CG_PLATFORMS.get(chain)
        if not platform:
            raise ValueError(f"CoinGecko contract lookup does not support {chain}")
        headers = {"x-cg-pro-api-key": settings.coingecko_api_key} if settings.coingecko_api_key else {}
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.get(
                f"{settings.coingecko_base_url}/coins/{platform}/contract/{_address(request)}",
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        market = payload.get("market_data") or {}
        data = {
            "name": payload.get("name"), "symbol": str(payload.get("symbol") or "").upper(),
            "price": (market.get("current_price") or {}).get("usd"),
            "volume": (market.get("total_volume") or {}).get("usd"),
            "marketCap": (market.get("market_cap") or {}).get("usd"),
            "price_change_24h": market.get("price_change_percentage_24h"),
        }
        return _snapshot("CoinGecko", data, "https://docs.coingecko.com/reference/coins-contract-address")

    def top_volume(self, request: str) -> str:
        """Tokens ranked by 24h trading volume: the whole market (top 250 by
        market cap, re-ranked by volume) or one chain's ecosystem category.
        Ranking is client-side; the free tier ignores CoinGecko's order param."""
        try:
            chain = _chain(request)
        except ValueError:
            chain = None
        category = _GAINERS_CATEGORY.get(chain) if chain else None
        params = {"vs_currency": "usd", "per_page": 100 if category else 250, "page": 1, "price_change_percentage": "24h", "order": "market_cap_desc"}
        if category:
            params["category"] = category
        headers = {"x-cg-pro-api-key": settings.coingecko_api_key} if settings.coingecko_api_key else {}
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.get(f"{settings.coingecko_base_url}/coins/markets", params=params, headers=headers)
            response.raise_for_status()
            rows = response.json()
        candidates = [row for row in (rows if isinstance(rows, list) else []) if (row.get("total_volume") or 0) > 0]
        # "Trending" asks want what is moving, so dollar-pegged and wrapped
        # assets (always the volume leaders) are left out; a plain "top by
        # volume" keeps them.
        trending = bool(re.search(r"\btrending\b", request, re.I))
        if trending:
            candidates = [row for row in candidates if not _PEGGED_OR_WRAPPED.match(str(row.get("symbol") or ""))]
        candidates.sort(key=lambda row: row.get("total_volume") or 0, reverse=True)
        top = candidates[:10]
        scope = f" on {chain.title()}" if category else ""
        if not top:
            return (f"# Top tokens by 24h volume{scope}\n\nCoinGecko returned no volume data right now.\n\n"
                    "Source: [CoinGecko markets](https://docs.coingecko.com/reference/coins-markets)")
        lines = [
            f"# Top tokens by 24h volume{scope}",
            f"**Data freshness**: {_utc()} · 24h trading volume across exchanges tracked by CoinGecko",
            "",
            "| # | Token | Price | 24h volume | Market cap | 24h change |",
            "|---:|---|---:|---:|---:|---:|",
        ]
        for i, row in enumerate(top, 1):
            change = row.get("price_change_percentage_24h")
            lines.append(
                f"| {i} | {(row.get('symbol') or '?').upper()} | {_money(row.get('current_price'))} | {_money(row.get('total_volume'))} | "
                f"{_money(row.get('market_cap'))} | {'' if change is None else f'{change:+.1f}%'} |"
            )
        source = f"CoinGecko markets, {category} category" if category else "CoinGecko markets, top 250 by market cap re-ranked by volume"
        lines.extend([
            "",
            f"Source: [{source}](https://docs.coingecko.com/reference/coins-markets)",
            "",
            ("**Note**: stablecoins and wrapped assets are left out of a trending list; ask for *top tokens by volume* to include them."
             if trending else
             "**Note**: volume counts centralized and decentralized exchanges CoinGecko tracks, so stablecoins and "
             "wrapped assets rank high by design; ask for *trending tokens by volume* to leave them out."),
        ])
        return "\n".join(lines)

    def gainers_losers(self, request: str) -> str:
        try:
            chain = _chain(request)
        except ValueError:
            chain = None    # market-wide: the top 250 by market cap, no category
        category = _GAINERS_CATEGORY.get(chain) if chain else None
        if chain and not category:
            raise ValueError(f"CoinGecko gainers/losers has no curated ecosystem category for {chain}")
        losers = bool(_LOSER_WORDS.search(request))
        headers = {"x-cg-pro-api-key": settings.coingecko_api_key} if settings.coingecko_api_key else {}
        params = {"vs_currency": "usd", "per_page": 100 if category else 250, "page": 1, "price_change_percentage": "24h"}
        if category:
            params["category"] = category
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.get(
                f"{settings.coingecko_base_url}/coins/markets",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            rows = response.json()
        # A tiny-volume coin can show a huge % swing from one thin trade --
        # a $10K 24h-volume floor keeps the list to moves with some real
        # trading behind them, without requiring a paid liquidity provider.
        candidates = [
            row for row in (rows if isinstance(rows, list) else [])
            if row.get("price_change_percentage_24h") is not None and (row.get("total_volume") or 0) >= 10_000
        ]
        candidates.sort(key=lambda row: row["price_change_percentage_24h"], reverse=not losers)
        top = candidates[:10]
        label = "Losers" if losers else "Gainers"
        scope = f"on {chain.title()}" if chain else "market-wide (top 250 by market cap)"
        basis = (f"CoinGecko's {chain.title()} ecosystem category (tokens associated with {chain.title()}; prices and volumes are global across venues, not trades on a {chain.title()} venue)"
                 if chain else "CoinGecko's top 250 by market cap")
        if not top:
            return (
                f"# {label} {scope}\n\nNo coins with at least $10K in 24h "
                "volume were returned by CoinGecko right now.\n\n"
                "Source: [CoinGecko markets](https://docs.coingecko.com/reference/coins-markets)"
            )
        lines = [
            f"# {label} {scope}",
            f"**Data freshness**: {_utc()} · **Basis**: {basis}",
            "",
            "| Token | Price | 24h change | 24h volume |",
            "|---|---:|---:|---:|",
        ]
        for row in top:
            change = row["price_change_percentage_24h"]
            lines.append(
                f"| {(row.get('symbol') or '?').upper()} | {_money(row.get('current_price'))} | "
                f"{change:+.1f}% | {_money(row.get('total_volume'))} |"
            )
        lines.extend([
            "",
            f"Source: [CoinGecko markets{', ' + category + ' category' if category else ''}](https://docs.coingecko.com/reference/coins-markets)",
            "",
            "**Note**: CoinGecko's ecosystem category can include bridged/wrapped assets alongside "
            "natively-deployed tokens. A large 24h move on modest volume can reverse quickly -- check "
            "liquidity before trading.",
        ])
        return compact_tool_result("\n".join(lines))

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "coingecko_token_by_contract", self.name, ("token_discovery", "market_data"), self.token,
            matches=_has_chain_and_address, keywords=("identity", "metadata", "token", "contract", "price"),
            chains=tuple(_CG_PLATFORMS), quota_per_minute=settings.coingecko_requests_per_minute,
            cache_ttl_seconds=60, priority=2,
            description="Token identity, price, volume, and market cap for a token, looked up by chain and contract address",
        ))
        router.register(ProviderTool(
            "coingecko_top_volume", self.name, ("token_discovery", "market_data"), self.top_volume,
            # A volume ranking, and the organic answer to a bare "trending
            # tokens" with no chain and no attention word (boosts take those).
            matches=lambda request: bool(VOLUME_RANKED.search(request)) or (
                bool(BARE_TRENDING.search(request)) and not ATTENTION_WORD.search(request) and not extract_chains(request)),
            keywords=("volume", "traded", "trending", "top", "tokens", "coins"),
            chains=tuple(_GAINERS_CATEGORY), quota_per_minute=settings.coingecko_requests_per_minute,
            cache_ttl_seconds=120, priority=9,
            description="Tokens ranked by 24h trading volume, market-wide or within one chain's ecosystem (a volume ranking, not paid boosts)",
        ))
        router.register(ProviderTool(
            "coingecko_gainers_losers", self.name, ("token_discovery", "market_data"), self.gainers_losers,
            matches=lambda request: bool(_GAINERS_LOSERS.search(request)) and _has_gainers_chain(request),
            keywords=("gainers", "losers", "movers", "winners", "performers"),
            chains=tuple(_GAINERS_CATEGORY), quota_per_minute=settings.coingecko_requests_per_minute,
            cache_ttl_seconds=120, priority=8,
            description="Top gaining or losing tokens by 24h price change within a specific chain's ecosystem",
        ))


class CoinMarketCapProvider:
    name = "coinmarketcap"

    def enabled(self) -> bool:
        return bool(settings.coinmarketcap_api_key)

    def token(self, request: str) -> str:
        headers = {"X-CMC_PRO_API_KEY": settings.coinmarketcap_api_key or ""}
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            info = client.get(
                f"{settings.coinmarketcap_base_url}/v2/cryptocurrency/info",
                params={"address": _address(request)}, headers=headers,
            )
            info.raise_for_status()
            values = (info.json().get("data") or {}).values()
            asset = next((row for rows in values for row in (rows if isinstance(rows, list) else [rows])), None)
            if not isinstance(asset, dict) or asset.get("id") is None:
                raise RuntimeError("CoinMarketCap did not resolve that contract")
            quote = client.get(
                f"{settings.coinmarketcap_base_url}/v3/cryptocurrency/quotes/latest",
                params={"id": asset["id"], "convert": "USD"}, headers=headers,
            )
            quote.raise_for_status()
        quote_data = (quote.json().get("data") or {}).get(str(asset["id"])) or {}
        usd = (quote_data.get("quote") or {}).get("USD") or {}
        return _snapshot("CoinMarketCap", {
            "name": asset.get("name"), "symbol": asset.get("symbol"), "price": usd.get("price"),
            "volume": usd.get("volume_24h"), "marketCap": usd.get("market_cap"),
            "price_change_24h": usd.get("percent_change_24h"),
        }, "https://coinmarketcap.com/api/documentation/pro-api-reference")

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "coinmarketcap_token_by_contract", self.name, ("token_discovery", "market_data"), self.token,
            enabled=self.enabled, matches=_has_chain_and_address,
            keywords=("identity", "metadata", "token", "contract", "price"),
            quota_per_minute=settings.coinmarketcap_requests_per_minute, cache_ttl_seconds=60, priority=1,
            description="Token identity, price, volume, and market cap for an EVM token, looked up by contract address",
        ))


class GoPlusProvider:
    name = "goplus"

    def security(self, request: str) -> str:
        chain, address = _chain(request), _address(request)
        headers = {"Authorization": f"Bearer {settings.goplus_access_token}"} if settings.goplus_access_token else {}
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.get(
                f"{settings.goplus_base_url}/token_security/{_EVM_CHAIN_IDS[chain]}",
                params={"contract_addresses": address}, headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        data = (payload.get("result") or {}).get(address.lower()) or (payload.get("result") or {}).get(address)
        if not isinstance(data, dict):
            raise RuntimeError("GoPlus returned no security record")
        critical_flags = {
            "Honeypot": data.get("is_honeypot"), "Blacklisted": data.get("is_blacklisted"),
            "Cannot sell all": data.get("cannot_sell_all"), "Hidden owner": data.get("hidden_owner"),
        }
        caution_flags = {
            "Mintable": data.get("is_mintable"), "Pausable transfers": data.get("transfer_pausable"),
            "Proxy contract": data.get("is_proxy"),
        }
        risky = [name for name, value in critical_flags.items() if str(value).lower() in {"1", "true"}]
        cautions = [name for name, value in caution_flags.items() if str(value).lower() in {"1", "true"}]
        lines = [
            f"# Token security — {data.get('token_name') or address}",
            f"**Provider**: GoPlus · **Checked**: {_utc()}", "",
            f"- **Assessment**: {'High-risk flags detected' if risky else 'No listed blocking flags detected'}",
            f"- **Blocking flags**: {', '.join(risky) if risky else 'None in the returned checks'}",
            f"- **Caution / contract features**: {', '.join(cautions) if cautions else 'None in the returned checks'}",
            f"- **Buy / sell tax**: {data.get('buy_tax', '—')} / {data.get('sell_tax', '—')}",
            f"- **Holders / LP holders**: {data.get('holder_count', '—')} / {data.get('lp_holder_count', '—')}", "",
            "Source: [GoPlus Token Security API](https://docs.gopluslabs.io/reference/tokensecurityusingget_1)",
            "A clean API result is not a guarantee. Verify liquidity, authorities, holder concentration and sellability.",
        ]
        return compact_tool_result("\n".join(lines))

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "goplus_token_security", self.name, ("token_security",), self.security,
            matches=lambda request: _evm_exact(request) and bool(_SECURITY.search(request)),
            keywords=("security", "risk", "honeypot", "safe"), chains=tuple(_EVM_CHAIN_IDS),
            quota_per_minute=settings.goplus_requests_per_minute, cache_ttl_seconds=90, priority=10,
            description="Token security scan for an EVM contract: honeypot/blacklist/mintable flags, buy/sell tax, holder concentration",
        ))


class HoneypotProvider:
    name = "honeypot"

    def security(self, request: str) -> str:
        chain, address = _chain(request), _address(request)
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.get(
                f"{settings.honeypot_base_url}/IsHoneypot",
                params={"address": address, "chainID": _EVM_CHAIN_IDS[chain]},
            )
            response.raise_for_status()
            data = response.json()
        result, simulation, code = data.get("honeypotResult") or {}, data.get("simulationResult") or {}, data.get("contractCode") or {}
        lines = [
            f"# Honeypot simulation — {data.get('token', {}).get('name') or address}",
            f"**Provider**: Honeypot.is · **Checked**: {_utc()}", "",
            f"- **Honeypot**: {'Yes' if result.get('isHoneypot') else 'No'}",
            f"- **Reason**: {result.get('honeypotReason') or '—'}",
            f"- **Buy / sell / transfer tax**: {simulation.get('buyTax', '—')}% / {simulation.get('sellTax', '—')}% / {simulation.get('transferTax', '—')}%",
            f"- **Open source / proxy**: {code.get('openSource', '—')} / {code.get('isProxy', '—')}", "",
            "Source: [Honeypot.is API](https://docs.honeypot.is/quickstart)",
            "Simulation coverage varies by chain and route; treat this as one signal, not a guarantee.",
        ]
        return compact_tool_result("\n".join(lines))

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "honeypot_token_security", self.name, ("token_security",), self.security,
            matches=lambda request: _evm_exact(request) and bool(_SECURITY.search(request)),
            keywords=("honeypot", "sellability", "sellable", "can i sell", "security", "risk"), chains=tuple(_EVM_CHAIN_IDS),
            quota_per_minute=settings.honeypot_requests_per_minute, cache_ttl_seconds=60, priority=8,
            description="Honeypot simulation for an EVM token: buy/sell/transfer tax, sellability, proxy/open-source contract check",
        ))


_DEFI_CHAIN_WORDS = {"ethereum", "solana", "base", "arbitrum", "bsc", "avalanche", "polygon", "optimism"}
_DEFI_IGNORED_WORDS = {
    "show", "top", "defi", "protocol", "protocols", "tvl", "on", "for", "the", "total",
    "value", "locked", "chain", "what", "whats", "s", "is", "are", "current", "latest",
} | _DEFI_CHAIN_WORDS


def _named_defi_subject(request: str) -> set[str]:
    """Words in the request that aren't generic DeFi/TVL vocabulary or a chain
    name -- i.e. a specific protocol like "Aave" or "Uniswap". A non-empty
    result means the request is about one protocol, not a chain's total TVL.
    """
    return {word.lower() for word in re.findall(r"[A-Za-z0-9-]+", request) if word.lower() not in _DEFI_IGNORED_WORDS}


_LLAMA_CACHE: dict[str, tuple[float, Any]] = {}
_LLAMA_CACHE_TTL = {"fees": 300.0, "pools": 600.0}


def _llama_cached(key: str, url: str, ttl: float, extract):
    """The dimensions and pools payloads are 4-11 MB: fetch once per TTL for
    the whole process, whatever the request wording."""
    import time as _time

    now = _time.monotonic()
    hit = _LLAMA_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    with httpx.Client(timeout=max(30.0, settings.provider_request_timeout_seconds)) as client:
        response = client.get(url)
        response.raise_for_status()
        value = extract(response.json())
    _LLAMA_CACHE[key] = (now, value)
    return value


def _llama_overview(kind: str, data_type: str | None = None) -> list[dict]:
    url = f"{settings.defillama_base_url}/overview/{kind}?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true" + (f"&dataType={data_type}" if data_type else "")
    return _llama_cached(f"{kind}:{data_type or 'fees'}", url, _LLAMA_CACHE_TTL["fees"], lambda d: [r for r in (d.get("protocols") or []) if isinstance(r, dict)])


def _llama_pools() -> list[dict]:
    return _llama_cached("pools", f"{settings.defillama_yields_base_url}/pools", _LLAMA_CACHE_TTL["pools"], lambda d: [p for p in (d.get("data") or []) if isinstance(p, dict)])


# An address the request treats as a TOKEN (token/mint/contract, first
# buyers, bundles, holders) is not a wallet whose history to read: the ANSEM
# mint's incoming dust transfers were shown as "token transactions" (UI run,
# 2026-09-23). "wallet" in the request keeps the wallet reading.
_TOKEN_ROLE = re.compile(r"\b(?:token|mint|contract|ca|memecoin|first\s+buyers?|early\s+buyers?|buyers?|bundl\w+|snip\w+|holders?|deployer|launch\w*)\b", re.I)


def _token_role(request: str) -> bool:
    text = request or ""
    return bool(_TOKEN_ROLE.search(text)) and not re.search(r"\bwallet\b", text, re.I)


def _requested_chain(request: str) -> str | None:
    for chain in ("ethereum", "solana", "base", "arbitrum", "optimism", "polygon", "bsc", "avalanche", "hyperliquid", "sui", "aptos", "tron", "linea", "scroll", "berachain", "sonic", "mantle", "blast"):
        if re.search(rf"\b{chain}\b", request, re.I):
            return chain
    if re.search(r"\bbnb\b", request, re.I):
        return "bsc"
    return None


class DefiLlamaProvider:
    name = "defillama"

    def tvl(self, request: str) -> str:
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.get(f"{settings.defillama_base_url}/v2/chains")
            response.raise_for_status()
            rows = response.json()
        chain = next((word for word in ("Ethereum", "Solana", "Base", "Arbitrum", "BSC", "Avalanche", "Polygon", "Optimism") if re.search(rf"\b{word}\b", request, re.I)), None)
        selected = [row for row in rows if not chain or str(row.get("name", "")).lower() == chain.lower()]
        selected = sorted(selected, key=lambda row: float(row.get("tvl") or 0), reverse=True)[:10]
        if not selected:
            raise RuntimeError("DeFiLlama returned no matching chain TVL")
        lines = ["# DeFi chain TVL", f"**Provider**: DeFiLlama · **Checked**: {_utc()}", "", "| Chain | TVL |", "|---|---:|"]
        lines.extend(f"| {row.get('name', '—')} | {_money(row.get('tvl'))} |" for row in selected)
        lines.extend(["", "Source: [DeFiLlama API](https://defillama.com/docs/api)"])
        return compact_tool_result("\n".join(lines))

    def protocols(self, request: str) -> str:
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.get(f"{settings.defillama_base_url}/protocols")
            response.raise_for_status()
            rows = response.json()
        chain_terms = {"ethereum", "solana", "base", "arbitrum", "bsc", "avalanche", "polygon", "optimism"}
        # Only a named subject matches a protocol: "what is Aave's TVL right now"
        # matched What The Hook on "what" (UI run, 2026-09-23).
        terms = {t for t in _named_defi_subject(request) if len(t) >= 3} - chain_terms
        matches = [row for row in rows if terms & set(re.findall(r"[a-z0-9-]+", str(row.get("name") or row.get("slug") or "").lower()))]
        requested_chain = next((chain for chain in chain_terms if re.search(rf"\b{chain}\b", request, re.I)), None)
        candidates = matches or rows
        if requested_chain:
            candidates = [row for row in candidates if requested_chain in {str(chain).lower() for chain in row.get("chains") or []}]
        selected = sorted(candidates, key=lambda row: float(row.get("tvl") or 0), reverse=True)[:10]
        if not selected:
            raise RuntimeError("DeFiLlama returned no matching protocols")
        lines = ["# DeFi protocols", f"**Provider**: DeFiLlama · **Checked**: {_utc()}", "", "| Protocol | TVL | Chains |", "|---|---:|---|"]
        for row in selected:
            chains = ", ".join((row.get("chains") or [])[:4]) or "—"
            lines.append(f"| {row.get('name') or '—'} | {_money(row.get('tvl'))} | {chains} |")
        lines.extend(["", "Source: [DeFiLlama API](https://defillama.com/docs/api)"])
        return compact_tool_result("\n".join(lines))

    # ---------------------------------------------------------------- fees / revenue
    def fees(self, request: str) -> str:
        """Fees and revenue per protocol from DefiLlama's dimensions API: a named
        protocol's 24h / 7d / 30d / all-time figures with methodology, or the
        top ten by 24h fees when no protocol is named."""
        fees_rows = _llama_overview("fees")
        revenue_rows = {row.get("name"): row for row in _llama_overview("fees", data_type="dailyRevenue")}
        subject = _named_defi_subject(request) - {"fees", "fee", "revenue", "revenues", "earnings", "earn", "income", "make", "makes", "much", "how", "does", "generate", "generates", "collect", "collects"}
        matches = [row for row in fees_rows if subject and subject & set(re.findall(r"[a-z0-9-]+", str(row.get("name") or "").lower()))] if subject else []
        chain = _requested_chain(request)
        candidates = matches or fees_rows
        if chain and not matches:
            candidates = [row for row in candidates if chain in {str(c).lower() for c in (row.get("chains") or [])}]
        selected = sorted(candidates, key=lambda row: float(row.get("total24h") or 0), reverse=True)[: (5 if matches else 10)]
        if not selected:
            raise RuntimeError("DeFiLlama has no fee data for that request")
        lines = ["# Protocol fees and revenue", f"**Provider**: DeFiLlama · **Checked**: {_utc()}", "",
                 "| Protocol | Category | Fees 24h | Fees 30d | Revenue 24h | Revenue 30d | Fees all-time |", "|---|---|---:|---:|---:|---:|---:|"]
        for row in selected:
            rev = revenue_rows.get(row.get("name")) or {}
            lines.append(f"| {row.get('name') or '—'} | {row.get('category') or '—'} | {_money(row.get('total24h'))} | {_money(row.get('total30d'))} | "
                         f"{_money(rev.get('total24h'))} | {_money(rev.get('total30d'))} | {_money(row.get('totalAllTime'))} |")
        if matches:
            method = (selected[0].get("methodology") or {}) if isinstance(selected[0].get("methodology"), dict) else {}
            notes = [f"- **{k}**: {v}" for k, v in method.items() if isinstance(v, str) and v][:5]
            if notes:
                lines += ["", "Methodology (DefiLlama):", *notes]
        lines += ["", "Fees are what users pay the protocol; revenue is the share kept by the protocol or its token holders. Source: [DeFiLlama fees](https://defillama.com/fees)"]
        return compact_tool_result("\n".join(lines))

    # ---------------------------------------------------------------- yields
    def yields(self, request: str) -> str:
        """Best yields from DefiLlama's pools dataset, filtered by the asset,
        chain and protocol named in the request; pools under $1M TVL are skipped
        unless nothing else matches."""
        pools = _llama_pools()
        symbols = {s for s in re.findall(r"\$?\b([A-Za-z]{2,6})\b", request) if s.isupper() and s not in {"TVL", "APY", "APR", "DEFI", "USD", "ETF", "AI"}}
        symbols |= {w.upper() for w in re.findall(r"\b(usdc|usdt|dai|usde|eth|weth|steth|wsteth|sol|jitosol|btc|wbtc|cbbtc|usds|pyusd|frax|ghо|gho|lusd|susde)\b", request, re.I)}
        chain = _requested_chain(request)
        subject = _named_defi_subject(request) - {"yield", "yields", "apy", "apr", "best", "rate", "rates", "highest", "safest", "earn", "lend", "lending", "stake", "staking", "farm", "farming", "pools", "pool", "stable", "stablecoin", "stablecoins"} - {s.lower() for s in symbols}
        wants_stable = bool(re.search(r"\bstable(?:coin)?s?\b", request, re.I))
        # "without exposing me to another volatile token", "single-asset", "only USDC": one-asset pools only (UI run, 2026-09-23)
        wants_single = bool(re.search(r"\b(?:without\s+(?:exposing\s+me\s+to\s+)?(?:another|other|any|a)\s+(?:volatile\s+)?(?:token|asset|coin)s?|single[- ]asset|only\s+(?:usdc|usdt|dai|usde|stables?)|no\s+(?:il|impermanent\s+loss)|not\s+(?:an?\s+)?lp)\b", request, re.I))
        # "best USDC yield" reads as the asset's own yield: single-asset pools rank
        # first, LPs pairing it with a volatile token follow under their own
        # heading (a 400% LP led "best USDC yield", live 2026-09-23).
        wants_lp = bool(re.search(r"\b(?:lp|liquidity\s+pool|pairs?|farm\w*|pool\s+with)\b", request, re.I))
        rows = [p for p in pools if isinstance(p, dict) and p.get("apy") is not None]
        if wants_single:
            rows = [p for p in rows if str(p.get("exposure") or "").lower() == "single"]
        if symbols:
            rows = [p for p in rows if any(s in {t.upper() for t in re.split(r"[-/ ]", str(p.get("symbol") or ""))} for s in symbols)]
        if chain:
            rows = [p for p in rows if str(p.get("chain") or "").lower() == chain]
        if subject:
            # A named protocol narrows the pools; leftover words ("pay", "safest") must not empty them.
            by_project = [p for p in rows if subject & set(re.findall(r"[a-z0-9-]+", str(p.get("project") or "").lower()))]
            rows = by_project or rows
        if wants_stable:
            rows = [p for p in rows if p.get("stablecoin")]
        liquid = [p for p in rows if float(p.get("tvlUsd") or 0) >= 1_000_000]
        rows = liquid or [p for p in rows if float(p.get("tvlUsd") or 0) >= 100_000]
        rows = [p for p in rows if 0 <= float(p.get("apy") or 0) <= 500]   # 4-digit APYs are dust pools or errors
        single_first = bool(symbols) and not wants_lp and not wants_single
        selected = sorted(rows, key=lambda p: (0 if (single_first and str(p.get("exposure") or "").lower() == "single") else 1, -float(p.get("apy") or 0)))[:8]
        if not selected:
            raise RuntimeError("DeFiLlama has no yield pools matching that request")
        what = " ".join(x for x in [", ".join(sorted(symbols)) if symbols else "", f"on {chain.title()}" if chain else "", "stablecoin" if wants_stable else "", "single-asset only" if wants_single else ""] if x).strip()
        lines = [f"# Yields{': ' + what if what else ''}", f"**Provider**: DeFiLlama · **Checked**: {_utc()} · pools ≥ $1M TVL, APY ≤ 500%", ""]
        if single_first:
            lines.append("Single-asset pools first (the asset alone, no impermanent loss); LP pools pairing it with another token follow and carry that token's price risk.")
            lines.append("")
        lines += ["| Pool | Project | Chain | Exposure | APY | Base / reward | TVL | Notes |", "|---|---|---|---|---:|---:|---:|---|"]
        for p in selected:
            notes = ", ".join(x for x in ["stablecoin" if p.get("stablecoin") else "", f"IL risk: {p.get('ilRisk')}" if p.get("ilRisk") and p.get("ilRisk") != "no" else "", f"{p.get('exposure')} exposure" if p.get("exposure") else ""] if x)
            base, reward = p.get("apyBase"), p.get("apyReward")
            lines.append(f"| {p.get('symbol') or '—'} | {p.get('project') or '—'} | {p.get('chain') or '—'} | {'single-asset' if str(p.get('exposure') or '').lower() == 'single' else 'LP (multi-asset)'} | {float(p.get('apy') or 0):.2f}% | "
                         f"{(f'{float(base):.2f}%' if base is not None else '—')} / {(f'{float(reward):.2f}%' if reward is not None else '—')} | {_money(p.get('tvlUsd'))} | {notes or '—'} |")
        lines += ["", "APY = base + reward incentives; reward APY depends on token prices and can end. Source: [DeFiLlama yields](https://defillama.com/yields)"]
        return compact_tool_result("\n".join(lines))

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "defillama_fees_revenue", self.name, ("defi_data",), self.fees,
            matches=lambda request: bool(re.search(r"\b(?:fees?|revenues?|earnings|protocol income|(?:how much|what) (?:does|do|did) \w+ (?:make|earn|generate|collect))\b", request, re.I))
                and not re.search(r"\b(?:gas|network|transaction|tx|withdrawal|trading|swap|maker|taker|priority) fees?\b", request, re.I),
            keywords=("fees", "revenue", "earnings", "protocol"), quota_per_minute=settings.defillama_requests_per_minute,
            cache_ttl_seconds=300, priority=10,
            description="Fees and revenue a DeFi protocol generates (24h, 30d, all-time) from DefiLlama, e.g. 'how much revenue does Aave make' or 'top protocols by fees'",
        ))
        router.register(ProviderTool(
            "defillama_yields", self.name, ("defi_data",), self.yields,
            matches=lambda request: bool(re.search(r"\b(?:apy|apr|yields?|yield farming|best (?:rate|return)s?|lending rates?|staking rates?|where (?:can|should) i (?:earn|lend|stake)|earn (?:on|with) (?:my )?[A-Za-z]+)\b", request, re.I))
                and not re.search(r"\b(?:bond|treasury|t-bill|savings account|dividend|fed|federal reserve|rate (?:hike|cut|decision)|equities|stocks?|wall street|nasdaq|s&p)\b", request, re.I),
            keywords=("yield", "apy", "apr", "lending", "staking", "pools"), quota_per_minute=settings.defillama_requests_per_minute,
            cache_ttl_seconds=600, priority=10,
            description="Best DeFi yields (APY) for an asset, chain or protocol from DefiLlama's pools, e.g. 'best USDC yield on Base' or 'stETH staking APY'",
        ))
        router.register(ProviderTool(
            # Chain TVL only when there's no leftover named subject (e.g. "Aave")
            # after removing generic DeFi/TVL words and chain names -- a
            # specific-protocol query like "Aave TVL" must not be answered
            # with an unrelated list of blockchain totals.
            "defillama_chain_tvl", self.name, ("defi_data",), self.tvl,
            matches=lambda request: bool(re.search(r"\b(?:defi|tvl|total value locked|chain tvl)\b", request, re.I))
                and not bool(re.search(r"\bprotocols?\b", request, re.I))
                and not _named_defi_subject(request),
            keywords=("defi", "tvl", "protocol"), quota_per_minute=settings.defillama_requests_per_minute,
            cache_ttl_seconds=120, priority=9,
            description="Total value locked (TVL) for a whole blockchain -- not a specific named protocol",
        ))
        router.register(ProviderTool(
            # Also catches "<Protocol> TVL" (e.g. "Aave TVL", "Uniswap TVL"):
            # a TVL request with a named subject beyond chain/DeFi vocabulary.
            # protocols() does its own name/slug matching against that subject.
            "defillama_protocols", self.name, ("defi_data",), self.protocols,
            matches=lambda request: bool(re.search(r"\b(?:defi protocols?|protocol tvl|top protocols?)\b", request, re.I))
                or (bool(re.search(r"\btvl\b", request, re.I)) and bool(_named_defi_subject(request))),
            keywords=("defi", "protocol", "tvl"), quota_per_minute=settings.defillama_requests_per_minute,
            cache_ttl_seconds=120, priority=10,
            description="Total value locked (TVL) for a specific named DeFi protocol like Aave or Uniswap, or a ranked list of top protocols",
        ))


class GoldRushProvider:
    name = "goldrush"

    def enabled(self) -> bool:
        return bool(settings.goldrush_api_key)

    def transactions(self, request: str) -> str:
        chain, address = _chain(request), _address(request)
        if chain not in _GOLDRUSH_CHAINS:
            raise ValueError(f"GoldRush wallet history does not support {chain}")
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.get(
                f"{settings.goldrush_base_url}/{_GOLDRUSH_CHAINS[chain]}/address/{address}/transactions_v3/",
                params={"no-logs": "true"}, headers={"Authorization": f"Bearer {settings.goldrush_api_key}"},
            )
            response.raise_for_status()
            payload = response.json()
        rows = (payload.get("data") or {}).get("items") or payload.get("items") or []
        if not rows:
            return f"# Wallet activity\n\nNo transactions were returned for `{address}` on {chain}.\n\nSource: [GoldRush](https://goldrush.dev/docs/)"
        lines = ["# Recent wallet transactions", f"**Provider**: GoldRush · **Checked**: {_utc()}", "", "| Time | Status | From → To | Value | Transaction |", "|---|---|---|---:|---|"]
        for row in rows[:10]:
            success = row.get("successful")
            status = "Success" if success is True else "Failed" if success is False else "—"
            tx = str(row.get("tx_hash") or "—")
            lines.append(f"| {row.get('block_signed_at') or '—'} | {status} | `{str(row.get('from_address') or '—')[:8]}…` → `{str(row.get('to_address') or '—')[:8]}…` | {_money(row.get('value_quote'))} | `{tx[:12]}…` |")
        lines.extend(["", "Source: [GoldRush Address Activity](https://goldrush.dev/docs/)"])
        return compact_tool_result("\n".join(lines))

    def balances(self, request: str) -> str:
        """Current multi-chain token balances -- unlike Bitquery's fallback
        (limited to whatever dataset:realtime happens to have indexed
        recently, since our Bitquery plan blocks the combined/archive
        datasets that would give a true current-state read), Covalent's
        balances_v2 endpoint is a real indexed current-balance snapshot,
        not a realtime-window-limited approximation.
        """
        chain, address = _chain(request), _address(request)
        if chain not in _GOLDRUSH_CHAINS:
            raise ValueError(f"GoldRush balances does not support {chain}")
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.get(
                f"{settings.goldrush_base_url}/{_GOLDRUSH_CHAINS[chain]}/address/{address}/balances_v2/",
                params={"quote-currency": "USD", "no-spam": "true"},
                headers={"Authorization": f"Bearer {settings.goldrush_api_key}"},
            )
            response.raise_for_status()
            payload = response.json()
        rows = (payload.get("data") or {}).get("items") or payload.get("items") or []

        def as_float(raw: Any) -> float:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return 0.0

        entries = []
        for row in rows:
            decimals = row.get("contract_decimals")
            raw_balance = as_float(row.get("balance"))
            if raw_balance <= 0 or not isinstance(decimals, int):
                continue
            amount = raw_balance / (10 ** decimals)
            entries.append((row, amount))
        entries.sort(key=lambda item: as_float(item[0].get("quote")), reverse=True)
        if not entries:
            return f"# Wallet token balances\n\nNo positive token balances were returned for `{address}` on {chain}.\n\nSource: [GoldRush Balances](https://goldrush.dev/docs/)"

        # A balance GoldRush could not price is UNKNOWN, never $0: live, a
        # Solana wallet came back with PENGU and WIF at "$0.00000000" because
        # no quote_rate was returned, and a reader took that as worthless.
        def priced(row: dict) -> bool:
            return row.get("quote_rate") is not None and row.get("quote") is not None

        with_price = [(row, amount) for row, amount in entries if priced(row)]
        without_price = [(row, amount) for row, amount in entries if not priced(row)]
        total = sum(as_float(row.get("quote")) for row, _ in with_price)
        lines = [
            "# Wallet token balances",
            f"**Provider**: GoldRush · **Checked**: {_utc()} · **Priced total**: {_money(total) if with_price else '—'} "
            f"({len(with_price)} priced, {len(without_price)} without a price)",
            "",
        ]
        if not with_price:
            lines += ["GoldRush returned no USD prices for any of these balances, so their value is unknown -- not zero. "
                      "Ask for a specific token's price, or try the wallet again in a moment.", ""]
        lines += ["| Token | Balance | USD Value |", "|---|---:|---:|"]

        def name(row: dict) -> str:
            # Solana SPL entries can come back with a null ticker/display
            # name (verified live) -- fall back to a shortened mint address
            # rather than showing a bare "?" for an otherwise-real balance.
            contract = str(row.get("contract_address") or "")
            fallback = f"{contract[:4]}…{contract[-4:]}" if len(contract) > 10 else "?"
            return row.get("contract_ticker_symbol") or row.get("contract_display_name") or fallback

        for row, amount in with_price[:25]:
            lines.append(f"| {name(row)} | {amount:,.4f} | {_money(row.get('quote'))} |")
        shown = 0
        for row, amount in without_price:
            if shown >= 8:
                break
            lines.append(f"| {name(row)} | {amount:,.4f} | unknown (no price) |")
            shown += 1
        if len(without_price) > shown:
            lines.append(f"| … {len(without_price) - shown} more without a price | | |")
        lines.extend(["", "Source: [GoldRush Balances](https://goldrush.dev/docs/)"])
        return compact_tool_result("\n".join(lines))

    def hyperliquid_positions(self, request: str) -> str:
        """Hyperliquid perp + spot account state -- a separate GoldRush
        product (hypercore.goldrushdata.com), not the Foundational API used
        by balances/transactions above, but the same API key. A drop-in
        for Hyperliquid's own public /info API's batch* endpoints; verified
        live against a real account (matching figures Nansen independently
        reported for the same wallet).
        """
        address = _address(request)
        if not address.startswith("0x"):
            raise ValueError("Hyperliquid accounts are EVM-addressed")
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            headers = {"Authorization": f"Bearer {settings.goldrush_api_key}"}
            perp_response = client.post(
                "https://hypercore.goldrushdata.com/info", headers=headers,
                json={"type": "batchClearinghouseState", "users": [address]},
            )
            perp_response.raise_for_status()
            spot_response = client.post(
                "https://hypercore.goldrushdata.com/info", headers=headers,
                json={"type": "batchSpotClearinghouseState", "users": [address]},
            )
            spot_response.raise_for_status()
        perp_rows = perp_response.json()
        spot_rows = spot_response.json()
        perp_state = perp_rows[0] if isinstance(perp_rows, list) and perp_rows else {}
        spot_state = spot_rows[0] if isinstance(spot_rows, list) and spot_rows else {}
        margin = perp_state.get("marginSummary") or {}
        positions = perp_state.get("assetPositions") or []
        balances = [row for row in (spot_state.get("balances") or []) if float(row.get("total") or 0) > 0]

        if not margin and not positions and not balances:
            return f"# Hyperliquid positions\n\nNo Hyperliquid account state was returned for `{address}`.\n\nSource: [GoldRush Hyperliquid API](https://goldrush.dev/platform/products/hyperliquid/)"

        lines = ["# Hyperliquid positions", f"**Provider**: GoldRush · **Checked**: {_utc()}", ""]
        if margin:
            lines.extend([
                "## Account Summary",
                f"**Account Value (USD)**: {_money(margin.get('accountValue'))}",
                "",
                f"**Total Notional (USD)**: {_money(margin.get('totalNtlPos'))}",
                "",
                f"**Margin Used (USD)**: {_money(margin.get('totalMarginUsed'))}",
                "",
                f"**Withdrawable (USD)**: {_money(perp_state.get('withdrawable'))}",
                "",
            ])
        if positions:
            lines.extend([
                "## Perp Positions",
                "| Token | Side | Leverage | Position Value (USD) | Size | Entry Price | Liquidation Price | Funding (USD) | Unrealized PnL (USD) | ROI |",
                "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
            ])
            for entry in positions:
                position = entry.get("position") or {}
                size = float(position.get("szi") or 0)
                leverage = position.get("leverage") or {}
                funding = (position.get("cumFunding") or {}).get("sinceOpen")
                roi = position.get("returnOnEquity")
                roi_str = f"{float(roi) * 100:+.1f}%" if roi is not None else "—"
                lines.append(
                    f"| {position.get('coin') or '?'} | {'Short' if size < 0 else 'Long'} | "
                    f"{leverage.get('value', '—')}x ({leverage.get('type', '—')}) | "
                    f"{_money(position.get('positionValue'))} | {abs(size):,.4f} | "
                    f"{_money(position.get('entryPx'))} | {_money(position.get('liquidationPx'))} | "
                    f"{_money(funding)} | {_money(position.get('unrealizedPnl'))} | {roi_str} |"
                )
            lines.append("")
        if balances:
            lines.extend(["## Spot Balances", "| Token | Amount |", "|---|---:|"])
            for row in balances:
                lines.append(f"| {row.get('coin') or '?'} | {float(row.get('total') or 0):,.4f} |")
            lines.append("")
        lines.append("Source: [GoldRush Hyperliquid API](https://goldrush.dev/platform/products/hyperliquid/)")
        return compact_tool_result("\n".join(lines))

    def token_top_holders(self, request: str) -> str:
        """Ranked token-holder list -- EVM only. Verified live: Covalent's
        token_holders_v2 endpoint explicitly rejects Solana
        ("Chain: solana-mainnet is not currently supported for this
        endpoint"), unlike balances_v2 -- see BitqueryProvider.token_top_holders
        for the Solana equivalent this deliberately leaves to Bitquery.
        """
        chain, address = _chain(request), _address(request)
        if chain not in _GOLDRUSH_CHAINS or chain in _GOLDRUSH_NON_EVM_CHAINS:
            raise ValueError(f"GoldRush top-holders does not support {chain}")
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.get(
                # No page-size param -- verified live it 400s ("Page size not
                # supported") on this endpoint, unlike balances_v2. Default
                # page size (100) is plenty for a top-25 ranking.
                f"{settings.goldrush_base_url}/{_GOLDRUSH_CHAINS[chain]}/tokens/{address}/token_holders_v2/",
                headers={"Authorization": f"Bearer {settings.goldrush_api_key}"},
            )
            response.raise_for_status()
            payload = response.json()
        rows = (payload.get("data") or {}).get("items") or []
        if not rows:
            return f"# Top token holders\n\nNo holder data was returned for `{address}` on {chain}.\n\nSource: [GoldRush Token Holders](https://goldrush.dev/docs/)"

        def as_float(raw: Any) -> float:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return 0.0

        total_supply = as_float(rows[0].get("total_supply"))
        decimals = rows[0].get("contract_decimals")
        symbol = rows[0].get("contract_ticker_symbol") or "?"
        lines = [
            "# Top token holders",
            f"**Provider**: GoldRush · **Checked**: {_utc()} · **Token**: {symbol} on {chain}",
            "",
            "| # | Holder | Balance | % of Supply |",
            "|---:|---|---:|---:|",
        ]
        for rank, row in enumerate(rows[:25], start=1):
            raw_balance = as_float(row.get("balance"))
            amount = raw_balance / (10 ** decimals) if isinstance(decimals, int) else raw_balance
            pct = f"{raw_balance / total_supply * 100:.2f}%" if total_supply else "—"
            holder = str(row.get("address") or "—")
            lines.append(f"| {rank} | `{holder[:6]}…{holder[-4:]}` | {amount:,.2f} | {pct} |")
        lines.extend([
            "",
            "**Note**: this ranks raw on-chain balances, so a DEX pool's own reserve address, a locked-LP "
            "vault, or another program-owned account can appear in this list alongside real individual "
            "holders -- it is not pre-filtered to exclude them.",
            "",
            "Source: [GoldRush Token Holders](https://goldrush.dev/docs/)",
        ])
        return compact_tool_result("\n".join(lines))

    def hyperliquid_market(self, request: str) -> str:
        """Hyperliquid perp market data (funding, mark/oracle price, open
        interest, 24h volume) -- chain-agnostic and does NOT require a
        wallet address, unlike hyperliquid_positions above. Verified live:
        hypercore.goldrushdata.com/info's metaAndAssetCtxs type returns all
        234 listed perps' current market context in one call.
        """
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.post(
                "https://hypercore.goldrushdata.com/info",
                headers={"Authorization": f"Bearer {settings.goldrush_api_key}"},
                json={"type": "metaAndAssetCtxs"},
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, list) or len(payload) < 2:
            raise RuntimeError("Unexpected metaAndAssetCtxs response shape")
        universe = (payload[0] or {}).get("universe") or []
        ctxs = payload[1] or []
        rows = list(zip(universe, ctxs))

        # A named coin (e.g. "BTC", "funding rate for kPEPE") -- match
        # against the real listed universe (verified live: 234 real assets,
        # including mixed-case "scaled" tickers like kPEPE/kSHIB/kBONK, so a
        # blanket all-caps requirement would miss those) rather than a fixed
        # symbol list. Short (<=2 char) tickers require a case-SENSITIVE
        # all-caps match in the request -- verified live that case-
        # insensitive matching false-positives hard here: "Show me
        # Hyperliquid market data" matched the common word "me" against a
        # real ticker, ME (Magic Eden), and silently narrowed to one asset
        # instead of a general overview. Longer tickers are safer to match
        # case-insensitively (collisions with common English words get rarer
        # as length grows), still guarded by an explicit stopword list.
        known = {asset.get("name", ""): idx for idx, (asset, _ctx) in enumerate(rows)}
        known_upper = {name.upper(): name for name in known}
        _STOPWORDS = {"THE", "FOR", "AND", "PRICE", "RATE", "ARE", "NOW", "GET", "ALL", "ANY", "ONE"}
        named = None
        for word in re.findall(r"\b[A-Za-z0-9]{2,10}\b", request):
            if len(word) <= 2:
                if word in known:  # case-sensitive for short tickers
                    named = word
                    break
            elif word.upper() in known_upper and word.upper() not in _STOPWORDS:
                named = known_upper[word.upper()]
                break

        lines = ["# Hyperliquid market data", f"**Provider**: GoldRush · **Checked**: {_utc()}", ""]
        if named:
            asset, ctx = rows[known[named]]
            lines.extend([
                f"## {named}",
                f"**Mark Price (USD)**: {_money(ctx.get('markPx'))}",
                "",
                f"**Oracle Price (USD)**: {_money(ctx.get('oraclePx'))}",
                "",
                f"**Funding Rate**: {float(ctx.get('funding') or 0) * 100:.4f}% (per hour)",
                "",
                f"**Open Interest**: {float(ctx.get('openInterest') or 0):,.2f} {named}",
                "",
                f"**24h Volume (USD)**: {_money(ctx.get('dayNtlVlm'))}",
                "",
                f"**Max Leverage**: {asset.get('maxLeverage', '—')}x",
                "",
            ])
        else:
            ranked = sorted(rows, key=lambda pair: float(pair[1].get("dayNtlVlm") or 0), reverse=True)[:15]
            lines.extend([
                "## Top perps by 24h volume",
                "| Asset | Mark Price | Funding (hourly) | Open Interest | 24h Volume |",
                "|---|---:|---:|---:|---:|",
            ])
            for asset, ctx in ranked:
                lines.append(
                    f"| {asset.get('name') or '?'} | {_money(ctx.get('markPx'))} | "
                    f"{float(ctx.get('funding') or 0) * 100:.4f}% | "
                    f"{float(ctx.get('openInterest') or 0):,.0f} | {_money(ctx.get('dayNtlVlm'))} |"
                )
            lines.append("")
        lines.append("Source: [GoldRush Hyperliquid API](https://goldrush.dev/platform/products/hyperliquid/)")
        return compact_tool_result("\n".join(lines))

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "goldrush_wallet_transactions", self.name, ("wallet_intelligence",), self.transactions,
            enabled=self.enabled,
            matches=lambda request: _evm_exact(request) and bool(_WALLET_ACTIVITY.search(request)),
            keywords=("wallet", "transactions", "activity", "history"), chains=tuple(_GOLDRUSH_CHAINS),
            quota_per_minute=settings.goldrush_requests_per_minute, cache_ttl_seconds=30, priority=9,
            description="Recent transaction history for an EVM wallet address on a specific chain",
        ))
        router.register(ProviderTool(
            # Priority above Bitquery's wallet_balances (7): a real indexed
            # current-balance snapshot is preferable to a realtime-window
            # approximation whenever GoldRush is configured.
            "goldrush_wallet_balances", self.name, ("wallet_intelligence",), self.balances,
            enabled=self.enabled,
            matches=lambda request: _goldrush_balances_exact(request) and bool(re.search(r"\b(?:balances?|holdings?)\b", request, re.IGNORECASE)),
            keywords=("balances", "holdings", "tokens"), chains=tuple(_GOLDRUSH_CHAINS),
            quota_per_minute=settings.goldrush_requests_per_minute, cache_ttl_seconds=120, priority=10,
            description="Current token balances and holdings for an EVM wallet address",
        ))
        router.register(ProviderTool(
            # Separate product/endpoint (hypercore.goldrushdata.com, not the
            # Foundational API) -- Hyperliquid accounts are EVM-addressed,
            # so this only ever matches a 0x address, on any chain (the
            # account isn't itself chain-scoped the way balances/
            # transactions are).
            "goldrush_hyperliquid_positions", self.name, ("wallet_intelligence",), self.hyperliquid_positions,
            enabled=self.enabled,
            matches=lambda request: bool(re.search(r"0x[0-9a-fA-F]{40}", request))
                and bool(re.search(r"\b(?:hyperliquid|perps?|perpetuals?|leverage|liquidation|funding)\b", request, re.IGNORECASE)),
            keywords=("hyperliquid", "perps", "leverage", "liquidation"),
            quota_per_minute=settings.goldrush_requests_per_minute, cache_ttl_seconds=30, priority=10,
            description="A wallet's open Hyperliquid perpetual futures positions, leverage, and liquidation price",
        ))
        router.register(ProviderTool(
            # Registered under token_discovery/token_security -- verified
            # live this session that a "top holders" request classifies into
            # those capabilities (checked ahead of market_data), where it was
            # previously falling through to dexscreener_token_pairs (no
            # keyword gate, so it wins any chain+address request by default).
            # A higher priority + a "holders" keyword requirement here wins
            # specifically for holder queries without displacing dexscreener
            # for other chain+address requests under the same capabilities.
            "goldrush_token_top_holders", self.name, ("token_discovery", "token_security"), self.token_top_holders,
            enabled=self.enabled,
            matches=lambda request: _has_chain_and_address(request) and bool(re.search(r"\b(?:top\s+)?holders?\b", request, re.IGNORECASE))
                and _chain(request) not in _GOLDRUSH_NON_EVM_CHAINS,
            keywords=("holders", "top holders", "holder distribution"), chains=tuple(c for c in _GOLDRUSH_CHAINS if c not in _GOLDRUSH_NON_EVM_CHAINS),
            quota_per_minute=settings.goldrush_requests_per_minute, cache_ttl_seconds=300, priority=10,
            description="Top wallet holders and their percentage of supply for an EVM token, by contract address",
        ))
        router.register(ProviderTool(
            # Chain-agnostic and deliberately does NOT require a wallet
            # address (unlike hyperliquid_positions above) -- this answers
            # "what's the funding rate/price on Hyperliquid for X", not
            # "what does wallet X hold on Hyperliquid".
            "goldrush_hyperliquid_market", self.name, ("market_data",), self.hyperliquid_market,
            enabled=self.enabled,
            matches=lambda request: bool(re.search(r"\bhyperliquid\b", request, re.IGNORECASE))
                and bool(re.search(r"\b(?:funding|mark\s*price|oracle\s*price|open\s*interest|oi|volume|perps?|perpetuals?|market|price|rate)\b", request, re.IGNORECASE)),
            keywords=("hyperliquid", "funding rate", "mark price", "open interest", "oi", "perps"),
            quota_per_minute=settings.goldrush_requests_per_minute, cache_ttl_seconds=15, priority=10,
            description="Hyperliquid perpetual futures market data: mark price, oracle price, funding rate, open interest, 24h volume",
        ))


class HeliusProvider:
    name = "helius"

    @staticmethod
    def api_key() -> str | None:
        if settings.helius_api_key:
            return settings.helius_api_key
        parsed = urlparse(settings.solana_rpc_url)
        return (parse_qs(parsed.query).get("api-key") or [None])[0] if "helius" in parsed.netloc else None

    def enabled(self) -> bool:
        return bool(self.api_key())

    def transactions(self, request: str) -> str:
        address = _address(request)
        url = f"https://mainnet.helius-rpc.com/?api-key={self.api_key()}"
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.post(url, json={
                "jsonrpc": "2.0", "id": "orbit-wallet-history", "method": "getTransactionsForAddress",
                "params": [address, {"transactionDetails": "signatures", "limit": 10, "sortOrder": "desc"}],
            })
            response.raise_for_status()
            payload = response.json()
        if payload.get("error"):
            raise RuntimeError(str(payload["error"].get("message") or "Helius RPC error"))
        result: Any = payload.get("result") or {}
        rows = result.get("data") if isinstance(result, dict) else result
        rows = rows if isinstance(rows, list) else []
        lines = ["# Recent Solana wallet transactions", f"**Provider**: Helius · **Checked**: {_utc()}", ""]
        if not rows:
            lines.append("No recent transactions were returned for this address.")
        else:
            lines.extend(["| Time | Slot | Signature |", "|---|---:|---|"])
            for row in rows:
                if isinstance(row, str):
                    row = {"signature": row}
                timestamp = row.get("blockTime") or row.get("timestamp") or "—"
                if isinstance(timestamp, (int, float)):
                    timestamp = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                lines.append(f"| {timestamp} | {row.get('slot', '—')} | `{str(row.get('signature') or '—')[:18]}…` |")
        lines.extend(["", "Source: [Helius getTransactionsForAddress](https://www.helius.dev/docs/api-reference/rpc/http/gettransactionsforaddress)"])
        return compact_tool_result("\n".join(lines))

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "helius_wallet_transactions", self.name, ("wallet_intelligence",), self.transactions,
            enabled=self.enabled,
            matches=lambda request: _has_chain_and_address(request) and _chain(request) == "solana" and bool(_WALLET_ACTIVITY.search(request)) and not _token_role(request),
            keywords=("wallet", "transactions", "activity", "history"), chains=("solana",),
            quota_per_minute=settings.helius_requests_per_minute, cache_ttl_seconds=20, priority=11,
            description="Recent transaction history for a Solana wallet address",
        ))


class ExchangeAnnouncementsProvider:
    name = "exchange_announcements"

    def search(self, request: str) -> str:
        return perplexity_web_search(
            "Find the latest relevant official exchange listing or delisting announcements for this request: "
            f"{request}. Prefer primary pages on Binance, Bybit, and OKX. Include event date, publication date, "
            "asset, exchange, and direct source URL. Do not invent or predict announcements."
        )

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "exchange_listing_announcements", self.name, ("listing_events",), self.search,
            enabled=perplexity_available,
            matches=lambda request: bool(re.search(r"\b(?:listings?|listed|delistings?|delisted|exchange announcements?)\b", request, re.I)),
            keywords=("listing", "delisting", "announcement", "binance", "bybit", "okx"),
            cost_usd=settings.perplexity_web_search_cost_usd, quota_per_minute=settings.perplexity_requests_per_minute,
            cache_ttl_seconds=60, priority=10,
            description="Recent official exchange listing or delisting announcements for a token, from Binance, Bybit, or OKX",
        ))


ADDITIONAL_PROVIDERS = (
    CoinGeckoProvider, CoinMarketCapProvider, GoPlusProvider, HoneypotProvider,
    DefiLlamaProvider, GoldRushProvider, HeliusProvider, ExchangeAnnouncementsProvider,
)
