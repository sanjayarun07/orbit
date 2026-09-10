"""Concrete, read-only market-data providers for the capability router."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

import httpx

from app.dexscreener_tools import (
    dexscreener_latest_profiles,
    dexscreener_pair_search,
    dexscreener_token_pairs,
    dexscreener_trending_metas,
)
from app.provider_router import ProviderRouter, ProviderTool
from app.settings import settings
from app.tool_results import compact_tool_result


_ADDRESS = re.compile(
    r"0x(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})(?![0-9a-fA-F])|"
    r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])"
)
_CHAIN_NAMES = ("solana", "base", "ethereum", "arbitrum", "bsc", "bnb", "polygon", "avalanche", "sui")
_SUPPORTED_CHAINS = ("solana", "base", "ethereum", "arbitrum", "bsc", "polygon", "avalanche", "sui")


def _matches(pattern: str):
    compiled = re.compile(pattern, re.IGNORECASE)
    return lambda request: bool(compiled.search(request))


def _address(request: str) -> str:
    match = _ADDRESS.search(request)
    if not match:
        raise ValueError("An exact token contract or mint is required")
    return match.group(0)


def _chain(request: str) -> str:
    lowered = request.lower()
    for chain in _CHAIN_NAMES:
        if re.search(rf"\b{chain}\b", lowered):
            return "bsc" if chain == "bnb" else chain
    raise ValueError("The token chain is required")


def _has_chain_and_address(request: str) -> bool:
    try:
        _address(request)
        _chain(request)
        return True
    except ValueError:
        return False


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
    return f"${number:,.8f}" if abs(number) < 0.01 else f"${number:,.4f}"


def _snapshot(provider: str, data: dict[str, Any], source: str) -> str:
    def first(*names: str):
        return next((data[name] for name in names if data.get(name) is not None), None)

    name = first("name", "symbol") or "Token"
    symbol = first("symbol") or "?"
    change = first("priceChange24hPercent", "priceChange24hPercentage", "price_change_24h")
    lines = [
        f"# {name} ({symbol})",
        f"**Provider**: {provider} · **Data freshness**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"- **Price**: {_money(first('price', 'priceUSD', 'value'))}",
        f"- **24h volume**: {_money(first('v24hUSD', 'volume24hUSD', 'volume24h', 'volume'))}",
        f"- **Liquidity**: {_money(first('liquidity', 'liquidityUSD'))}",
        f"- **Market cap**: {_money(first('mc', 'marketCap', 'marketCapUSD', 'market_cap'))}",
        f"- **24h change**: {f'{float(change):+.2f}%' if change is not None else '—'}",
        "",
        f"Source: [{provider}]({source})",
        "",
        "Verify the chain and exact contract. Provider metrics can differ because of pool coverage and pricing methodology.",
    ]
    return compact_tool_result("\n".join(lines))


class DexScreenerProvider:
    name = "dexscreener"

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "dexscreener_trending_metas", self.name, ("market_data", "token_discovery"),
            dexscreener_trending_metas, keywords=("trending", "trend", "narrative", "meta"),
            matches=_matches(r"\b(?:trending|trends?|narratives?|metas?|hot)\b"),
            chains=_SUPPORTED_CHAINS, quota_per_minute=60, cache_ttl_seconds=60, priority=5,
        ))
        router.register(ProviderTool(
            "dexscreener_latest_profiles", self.name, ("token_discovery",),
            dexscreener_latest_profiles, keywords=("new", "latest", "recent", "launch", "profile", "pairs"),
            matches=_matches(r"\b(?:(?:new|latest|recent)\s+(?:token\s+)?(?:profiles|pairs|launches)|new\s+tokens|token\s+launches)\b"),
            chains=_SUPPORTED_CHAINS, quota_per_minute=60, cache_ttl_seconds=45, priority=4,
        ))
        router.register(ProviderTool(
            "dexscreener_token_pairs", self.name, ("market_data", "token_discovery", "token_security"),
            dexscreener_token_pairs, matches=_has_chain_and_address,
            keywords=("contract", "mint", "liquidity", "pairs", "analyze"), chains=_SUPPORTED_CHAINS,
            quota_per_minute=300, cache_ttl_seconds=30, priority=3,
        ))
        router.register(ProviderTool(
            "dexscreener_pair_search", self.name, ("market_data", "token_discovery"),
            dexscreener_pair_search, keywords=("token", "coin", "pair", "price", "volume", "liquidity"),
            chains=_SUPPORTED_CHAINS, quota_per_minute=300, cache_ttl_seconds=30, priority=1,
        ))


class BirdeyeProvider:
    name = "birdeye"

    def enabled(self) -> bool:
        return bool(settings.birdeye_api_key)

    def token_overview(self, request: str) -> str:
        chain = _chain(request)
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.get(
                f"{settings.birdeye_base_url}/defi/token_overview",
                params={"address": _address(request)},
                headers={"X-API-KEY": settings.birdeye_api_key or "", "x-chain": chain},
            )
            response.raise_for_status()
            payload = response.json()
        data = payload.get("data") or {}
        if not isinstance(data, dict) or not data:
            raise RuntimeError("Birdeye returned no token overview")
        return _snapshot("Birdeye", data, "https://docs.birdeye.so/reference/get-defi-token_overview")

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            # market_data only: the overview formatter returns price/volume/
            # liquidity/mcap/change, never holder concentration, mint/freeze
            # authority, or tax flags, so it isn't a genuine security signal.
            "birdeye_token_overview", self.name, ("market_data",), self.token_overview,
            enabled=self.enabled, matches=_has_chain_and_address, keywords=("price", "volume", "liquidity", "overview"),
            chains=_SUPPORTED_CHAINS, cost_usd=settings.birdeye_request_cost_usd,
            quota_per_minute=settings.birdeye_requests_per_minute, cache_ttl_seconds=30, priority=5,
        ))


class MobulaProvider:
    name = "mobula"

    def enabled(self) -> bool:
        return bool(settings.mobula_api_key)

    def token_details(self, request: str) -> str:
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.get(
                f"{settings.mobula_base_url}/token/details",
                params={"address": _address(request), "blockchain": _chain(request), "currencies": "USD"},
                headers={"Authorization": settings.mobula_api_key or ""},
            )
            response.raise_for_status()
            payload = response.json()
        data = payload.get("data") or {}
        if not isinstance(data, dict) or not data:
            raise RuntimeError("Mobula returned no token details")
        return _snapshot("Mobula", data, "https://docs.mobula.io/spot/data/token-details")

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            # Same formatter/fields as Birdeye above: market_data only, not a
            # security signal despite the provider's own "details" naming.
            "mobula_token_details", self.name, ("market_data", "token_discovery"), self.token_details,
            enabled=self.enabled, matches=_has_chain_and_address,
            keywords=("details", "price", "volume", "liquidity"), chains=_SUPPORTED_CHAINS,
            cost_usd=settings.mobula_request_cost_usd, quota_per_minute=settings.mobula_requests_per_minute,
            cache_ttl_seconds=30, priority=4,
        ))


class BitqueryProvider:
    name = "bitquery"
    _EVM_NETWORKS = {"ethereum": "eth", "base": "base", "arbitrum": "arbitrum", "bsc": "bsc", "polygon": "matic", "avalanche": "avalanche"}

    def enabled(self) -> bool:
        return bool(settings.bitquery_api_key)

    def recent_dex_trades(self, request: str) -> str:
        chain, address = _chain(request), _address(request)
        if chain == "solana":
            query = """query TokenTrades($token: String!) { Solana(dataset: realtime) { DEXTradeByTokens(limit: {count: 10}, orderBy: {descending: Block_Time}, where: {Trade: {Currency: {MintAddress: {is: $token}}}, Transaction: {Result: {Success: true}}}) { Block { Time } Trade { PriceInUSD Currency { Symbol MintAddress } Dex { ProtocolName } } Transaction { Signature } } } }"""
            variables = {"token": address}
            path = ("data", "Solana", "DEXTradeByTokens")
        elif chain in self._EVM_NETWORKS:
            query = """query TokenTrades($network: evm_network!, $token: String!) { EVM(dataset: realtime, network: $network) { DEXTradeByTokens(limit: {count: 10}, orderBy: {descending: Block_Time}, where: {Trade: {Currency: {SmartContract: {is: $token}}}, TransactionStatus: {Success: true}}) { Block { Time } Trade { PriceInUSD Currency { Symbol SmartContract } Dex { ProtocolName } } Transaction { Hash } } } }"""
            variables = {"network": self._EVM_NETWORKS[chain], "token": address}
            path = ("data", "EVM", "DEXTradeByTokens")
        else:
            raise ValueError(f"Bitquery recent-trade adapter does not support {chain}")
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.post(
                settings.bitquery_graphql_url,
                headers={"Authorization": f"Bearer {settings.bitquery_api_key}"},
                json={"query": query, "variables": variables},
            )
            response.raise_for_status()
            payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(f"Bitquery GraphQL error: {payload['errors'][0].get('message', 'unknown')}")
        value: Any = payload
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        trades = value if isinstance(value, list) else []
        if not trades:
            raise RuntimeError("Bitquery returned no recent DEX trades")
        lines = [
            "# Recent on-chain DEX trades",
            f"**Provider**: Bitquery · **Data freshness**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "| Time | Token | Price | DEX | Transaction |",
            "|---|---|---:|---|---|",
        ]
        for row in trades:
            trade = row.get("Trade") or {}
            currency = trade.get("Currency") or {}
            transaction = row.get("Transaction") or {}
            tx = transaction.get("Signature") or transaction.get("Hash") or "—"
            lines.append(
                f"| {(row.get('Block') or {}).get('Time') or '—'} | {currency.get('Symbol') or '?'} | "
                f"{_money(trade.get('PriceInUSD'))} | {(trade.get('Dex') or {}).get('ProtocolName') or '—'} | `{str(tx)[:18]}…` |"
            )
        lines.extend(["", "Source: [Bitquery GraphQL API](https://docs.bitquery.io/)"])
        return compact_tool_result("\n".join(lines))

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "bitquery_recent_dex_trades", self.name, ("market_data",), self.recent_dex_trades,
            enabled=self.enabled, matches=lambda request: _has_chain_and_address(request) and bool(re.search(r"\b(?:trades?|buys?|sells?|flow)\b", request, re.IGNORECASE)),
            keywords=("trades", "buys", "sells", "flow"), chains=tuple(self._EVM_NETWORKS) + ("solana",),
            cost_usd=settings.bitquery_request_cost_usd, quota_per_minute=settings.bitquery_requests_per_minute,
            cache_ttl_seconds=20, priority=6,
        ))


MARKET_PROVIDERS = (DexScreenerProvider, BirdeyeProvider, MobulaProvider, BitqueryProvider)
