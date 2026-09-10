"""Optional first-party adapters for identity, risk, DeFi and wallet data.

All tools in this module are read-only.  Execution providers (Relay, Jupiter and
LI.FI) deliberately live outside the research router so a fallback can never
turn a research request into a transaction.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.market_providers import _address, _chain, _has_chain_and_address, _money, _snapshot
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
}
_SECURITY = re.compile(r"\b(?:safe|safety|security|risk|rug|scam|honeypot|sellability|audit)\b", re.I)
_WALLET_ACTIVITY = re.compile(r"\b(?:wallet|transactions?|activity|history|transfers?)\b", re.I)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _evm_exact(request: str) -> bool:
    try:
        return _chain(request) in _EVM_CHAIN_IDS and _address(request).startswith("0x")
    except ValueError:
        return False


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

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "coingecko_token_by_contract", self.name, ("token_discovery", "market_data"), self.token,
            matches=_has_chain_and_address, keywords=("identity", "metadata", "token", "contract", "price"),
            chains=tuple(_CG_PLATFORMS), quota_per_minute=settings.coingecko_requests_per_minute,
            cache_ttl_seconds=60, priority=2,
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
            keywords=("honeypot", "sellability", "security", "risk"), chains=tuple(_EVM_CHAIN_IDS),
            quota_per_minute=settings.honeypot_requests_per_minute, cache_ttl_seconds=60, priority=8,
        ))


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
        ignored = {"show", "top", "defi", "protocol", "protocols", "tvl", "on", "for", "the"} | chain_terms
        terms = {word.lower() for word in re.findall(r"[A-Za-z0-9-]+", request) if word.lower() not in ignored}
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

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "defillama_chain_tvl", self.name, ("defi_data",), self.tvl,
            matches=lambda request: bool(re.search(r"\b(?:defi|tvl|total value locked|chain tvl)\b", request, re.I)) and not bool(re.search(r"\bprotocols?\b", request, re.I)),
            keywords=("defi", "tvl", "protocol"), quota_per_minute=settings.defillama_requests_per_minute,
            cache_ttl_seconds=120, priority=9,
        ))
        router.register(ProviderTool(
            "defillama_protocols", self.name, ("defi_data",), self.protocols,
            matches=lambda request: bool(re.search(r"\b(?:defi protocols?|protocol tvl|top protocols?)\b", request, re.I)),
            keywords=("defi", "protocol", "tvl"), quota_per_minute=settings.defillama_requests_per_minute,
            cache_ttl_seconds=120, priority=10,
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

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "goldrush_wallet_transactions", self.name, ("wallet_intelligence",), self.transactions,
            enabled=self.enabled,
            matches=lambda request: _evm_exact(request) and bool(_WALLET_ACTIVITY.search(request)),
            keywords=("wallet", "transactions", "activity", "history"), chains=tuple(_GOLDRUSH_CHAINS),
            quota_per_minute=settings.goldrush_requests_per_minute, cache_ttl_seconds=30, priority=9,
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
            matches=lambda request: _has_chain_and_address(request) and _chain(request) == "solana" and bool(_WALLET_ACTIVITY.search(request)),
            keywords=("wallet", "transactions", "activity", "history"), chains=("solana",),
            quota_per_minute=settings.helius_requests_per_minute, cache_ttl_seconds=20, priority=11,
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
            matches=lambda request: bool(re.search(r"\b(?:listing|listings|listed|delisting|delisted|exchange announcement)\b", request, re.I)),
            keywords=("listing", "delisting", "announcement", "binance", "bybit", "okx"),
            cost_usd=settings.perplexity_web_search_cost_usd, quota_per_minute=settings.perplexity_requests_per_minute,
            cache_ttl_seconds=60, priority=10,
        ))


ADDITIONAL_PROVIDERS = (
    CoinGeckoProvider, CoinMarketCapProvider, GoPlusProvider, HoneypotProvider,
    DefiLlamaProvider, GoldRushProvider, HeliusProvider, ExchangeAnnouncementsProvider,
)
