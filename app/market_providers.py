"""Concrete, read-only market-data providers for the capability router."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

import httpx

from app.dexscreener_tools import (
    dexscreener_boosted_tokens,
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


# "trending/hot/top/latest TOKENS" (token-level discovery), or any request
# naming a launchpad -- distinct from "trending narratives/metas" (which is
# dexscreener_trending_metas) and from a generic "what's trending in crypto"
# market brief. The discovery word must actually MODIFY tokens/coins (adjacent,
# through a chain of discovery adjectives only) -- NOT merely co-occur with the
# word "token" somewhere in the sentence. Without this, "top holders of BONK
# token" matched ("top" ... "token") and wrongly returned a trending list.
TRENDING_TOKENS = re.compile(
    r"\b(?:trending|hot|hottest|top|latest|newest|popular|biggest)"
    r"(?:\s+(?:trending|hot|new|latest|top|meme|solana|base|bsc|active|popular|newest|biggest))*"
    r"\s+(?:tokens?|coins?|gems?|memecoins?)\b"
    r"|\b(?:pump\.?\s?fun|pumpfun|letsbonk|bonk\.?fun|moonshot)\b",
    re.IGNORECASE,
)
_TOKEN_WORD = re.compile(r"\b(?:tokens?|coins?|gems?|memecoins?)\b", re.IGNORECASE)
# "trending tokens by volume", "top coins by 24h volume", "most traded tokens
# today", "highest volume coins": a volume RANKING, which DEX Screener's paid
# boosts cannot answer -- CoinGecko markets sorted by 24h volume can.
VOLUME_RANKED = re.compile(
    r"\b(?:by|highest|most|largest|biggest|top)\b[^.?!\n]{0,30}?\b(?:24\s*-?\s*h(?:ou)?rs?|daily|trading)?\s*volumes?\b"
    r"|\bvolume\s+(?:leaders?|ranking|rank)\b|\bmost\s+traded\b",
    re.IGNORECASE,
)
_TREND_WORD = re.compile(r"\b(?:trending|trends?|narratives?|metas?|hot)\b", re.IGNORECASE)


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
    if abs(number) >= 1_000_000_000_000:
        return f"${number / 1_000_000_000_000:.2f}T"
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
            "dexscreener_boosted_tokens", self.name, ("market_data", "token_discovery"),
            dexscreener_boosted_tokens,
            keywords=("trending", "hot", "top", "tokens", "gems", "pump", "launchpad", "boosted"),
            # A ranked LIST OF TOKENS for "trending tokens on <chain/launchpad>".
            # Wins over trending_metas (narratives) and latest_profiles for
            # token-level asks via priority + keyword hits.
            matches=lambda request: bool(TRENDING_TOKENS.search(request)) and not VOLUME_RANKED.search(request),
            chains=_SUPPORTED_CHAINS + ("robinhood",),
            quota_per_minute=60, cache_ttl_seconds=60, priority=7,
            description="Ranked list of trending/boosted tokens on a chain or launchpad (pump.fun, etc.), with live price, volume, and liquidity",
        ))
        router.register(ProviderTool(
            "dexscreener_trending_metas", self.name, ("market_data", "token_discovery"),
            dexscreener_trending_metas, keywords=("trending", "trend", "narrative", "meta"),
            # Narratives/metas, NOT a token list -- so it must yield to
            # dexscreener_boosted_tokens whenever the user names tokens/coins/
            # gems (they want tokens, not narrative buckets).
            matches=lambda request: bool(_TREND_WORD.search(request)) and not _TOKEN_WORD.search(request),
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
            description="Trading pair price, liquidity, and volume for a token by chain and contract address",
        ))
        router.register(ProviderTool(
            # description is set here purely as a calibration/comparison
            # target (see app/routing/tool_semantic.py's docstring) -- this
            # tool has no `matches` gate at all (defaults to always-True), so
            # it never fails its regex gate and never actually enters the
            # semantic-fallback path at runtime. Its mis-ranking against
            # dexscreener_token_pairs for a chain+address request is
            # _score()'s soft-ranking territory, not something this layer
            # touches -- see the tool-retrieval plan's explicit scope note.
            "dexscreener_pair_search", self.name, ("market_data", "token_discovery"),
            dexscreener_pair_search, keywords=("token", "coin", "pair", "price", "volume", "liquidity"),
            chains=_SUPPORTED_CHAINS, quota_per_minute=300, cache_ttl_seconds=30, priority=1,
            description="Trading pair price, liquidity, and volume for a token by name or symbol search",
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

    def wallet_balances(self, request: str) -> str:
        """Raw on-chain spot token balances for a wallet.

        This exists as the wallet_intelligence fallback for when Nansen is
        unavailable or out of API credits (e.g. address_portfolio's Token
        Holdings section returning 403 Insufficient credits). It only covers
        spot balances -- Bitquery has no equivalent for Nansen's DeFi
        position aggregation or Hyperliquid perps data, so those sections
        have no fallback and stay disclosed as unavailable.
        """
        chain, address = _chain(request), _address(request)
        # dataset: "realtime" only -- "combined" (realtime + archive) and
        # "archive" alone both 403 on a realtime-only plan. This means a
        # wallet with no balance-changing activity inside the realtime
        # window can come back empty even when it demonstrably holds funds
        # (verified against known-active wallets during testing); it is not
        # equivalent to Nansen's full historical balance snapshot.
        if chain == "solana":
            # Solana's BalanceUpdate cube nests Currency under BalanceUpdate
            # itself (unlike EVM, where Currency is a sibling field) -- an
            # un-nested `Currency` field does not exist on this cube.
            query = """query WalletBalances($address: String!) { Solana(dataset: realtime) { BalanceUpdates(limit: {count: 25}, orderBy: {descendingByField: "balance"}, where: {BalanceUpdate: {Account: {Owner: {is: $address}}}}) { BalanceUpdate { Currency { Name Symbol MintAddress } } balance: sum(of: BalanceUpdate_PostBalance) } } }"""
            variables = {"address": address}
            path = ("data", "Solana", "BalanceUpdates")
        elif chain in self._EVM_NETWORKS:
            query = """query WalletBalances($network: evm_network!, $address: String!) { EVM(dataset: realtime, network: $network) { BalanceUpdates(limit: {count: 25}, orderBy: {descendingByField: "balance"}, where: {BalanceUpdate: {Address: {is: $address}}}) { Currency { Name Symbol SmartContract } balance: sum(of: BalanceUpdate_Amount) } } }"""
            variables = {"network": self._EVM_NETWORKS[chain], "address": address}
            path = ("data", "EVM", "BalanceUpdates")
        else:
            raise ValueError(f"Bitquery wallet-balance adapter does not support {chain}")
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
        rows = value if isinstance(value, list) else []

        def as_float(raw: Any) -> float:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return 0.0

        def currency_of(row: dict) -> dict:
            # EVM: Currency is a sibling of `balance`. Solana: it's nested
            # one level deeper, under BalanceUpdate.
            return row.get("Currency") or (row.get("BalanceUpdate") or {}).get("Currency") or {}

        # Bitquery's BalanceUpdate_Amount/BalanceUpdate_PostBalance sums are
        # already decimal-adjusted human-readable amounts (verified live:
        # e.g. a WIF balance of 258 came back as "258", not a raw atomic
        # integer) -- dividing by Currency.Decimals again would double-adjust
        # and silently produce garbage, so the balance is used as returned.
        entries = []
        for row in rows:
            balance = as_float(row.get("balance"))
            if balance <= 0:
                continue
            entries.append((currency_of(row), balance))
        entries.sort(key=lambda item: item[1], reverse=True)
        if not entries:
            return (
                f"# Wallet token balances\n\nNo positive token balances were returned for `{address}` on "
                f"{chain} within Bitquery's realtime window -- this does not necessarily mean the wallet "
                "is empty; it means no balance-changing activity for this address was indexed in that "
                "window.\n\nSource: [Bitquery GraphQL API](https://docs.bitquery.io/)"
            )
        lines = [
            "# Wallet token balances",
            f"**Provider**: Bitquery · **Data freshness**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "| Token | Balance |",
            "|---|---:|",
        ]
        def fmt_balance(value: float) -> str:
            # Avoid scientific notation ("4.17834e+07") for a raw token
            # amount -- comma-grouped for large balances, full precision
            # (trailing zeros trimmed) for sub-1 fractional balances.
            precision = 4 if abs(value) >= 1 else 8
            return f"{value:,.{precision}f}".rstrip("0").rstrip(".") or "0"

        for currency, balance in entries[:25]:
            symbol = currency.get("Symbol") or currency.get("Name") or "?"
            lines.append(f"| {symbol} | {fmt_balance(balance)} |")
        lines.extend([
            "",
            "Source: [Bitquery GraphQL API](https://docs.bitquery.io/)",
            "",
            "**Note**: raw on-chain token amounts from Bitquery's realtime dataset, not a full historical "
            "balance snapshot. Bitquery does not price every asset, so USD values are not included here -- "
            "cross-check against a priced source before treating any figure as final.",
        ])
        return compact_tool_result("\n".join(lines))

    def token_top_holders(self, request: str) -> str:
        """Ranked Solana token-holder list -- the Solana-side complement to
        GoldRushProvider.token_top_holders (EVM only; Covalent explicitly
        rejects Solana for its token_holders_v2 endpoint, verified live).

        Two real limitations, disclosed rather than silently patched:
        1. Bitquery's own `orderBy: {descendingByField: "balance"}` does NOT
           actually sort this aggregated field (verified live -- results came
           back unordered) -- sorted client-side here instead, over a wider
           fetch (200 rows) so the true top holders aren't missed.
        2. This ranks raw current balances with no pool/vault filtering -- a
           DEX pool's own reserve account (verified live: STONK's own
           Raydium pool showed up as a top "holder" of itself) or a locked-LP
           vault can appear alongside real holders. Disclosed in the output,
           not filtered out -- filtering would need knowing every pool
           address in advance, which isn't available generically.
        """
        chain, address = _chain(request), _address(request)
        if chain != "solana":
            raise ValueError("Bitquery top-holders (Solana-only complement) does not support this chain")
        query = """query TopHolders($token: String!) {
  Solana(dataset: realtime) {
    BalanceUpdates(
      limit: {count: 200}
      where: {BalanceUpdate: {Currency: {MintAddress: {is: $token}}}}
    ) {
      BalanceUpdate { Account { Owner } Currency { Symbol } }
      balance: sum(of: BalanceUpdate_PostBalance)
    }
  }
}"""
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            response = client.post(
                settings.bitquery_graphql_url,
                headers={"Authorization": f"Bearer {settings.bitquery_api_key}"},
                json={"query": query, "variables": {"token": address}},
            )
            response.raise_for_status()
            payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(f"Bitquery GraphQL error: {payload['errors'][0].get('message', 'unknown')}")
        rows = (((payload.get("data") or {}).get("Solana") or {}).get("BalanceUpdates")) or []

        def as_float(raw: Any) -> float:
            try:
                return float(raw)
            except (TypeError, ValueError):
                return 0.0

        symbol = "?"
        ranked: list[tuple[str, float]] = []
        for row in rows:
            update = row.get("BalanceUpdate") or {}
            owner = (update.get("Account") or {}).get("Owner")
            symbol = (update.get("Currency") or {}).get("Symbol") or symbol
            balance = as_float(row.get("balance"))
            if owner and balance > 0:
                ranked.append((owner, balance))
        ranked.sort(key=lambda pair: pair[1], reverse=True)
        if not ranked:
            return f"# Top token holders\n\nNo positive holder balances were returned for `{address}` on Solana.\n\nSource: [Bitquery GraphQL API](https://docs.bitquery.io/)"

        lines = [
            "# Top token holders",
            f"**Provider**: Bitquery · **Data freshness**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · **Token**: {symbol} on Solana",
            "",
            "| # | Holder | Balance |",
            "|---:|---|---:|",
        ]
        for rank, (owner, balance) in enumerate(ranked[:25], start=1):
            lines.append(f"| {rank} | `{owner[:6]}…{owner[-4:]}` | {balance:,.2f} |")
        lines.extend([
            "",
            "**Note**: ranked from a 200-row sample of recent Bitquery `realtime` balance updates, not a "
            "full on-chain scan, and not pre-filtered -- a DEX pool's own reserve address or a locked-LP "
            "vault can appear in this list alongside real individual holders.",
            "",
            "Source: [Bitquery GraphQL API](https://docs.bitquery.io/)",
        ])
        return compact_tool_result("\n".join(lines))

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "bitquery_recent_dex_trades", self.name, ("market_data",), self.recent_dex_trades,
            enabled=self.enabled, matches=lambda request: _has_chain_and_address(request) and bool(re.search(r"\b(?:trades?|buys?|sells?|flow)\b", request, re.IGNORECASE)),
            keywords=("trades", "buys", "sells", "flow"), chains=tuple(self._EVM_NETWORKS) + ("solana",),
            cost_usd=settings.bitquery_request_cost_usd, quota_per_minute=settings.bitquery_requests_per_minute,
            cache_ttl_seconds=20, priority=6,
        ))
        router.register(ProviderTool(
            # wallet_intelligence: the Nansen-unavailable/no-credits fallback
            # for raw spot balances. Priority is deliberately below
            # GoldRush/Helius (9/11) -- those are still preferred when they
            # apply -- but this is the only fallback that covers EVM *and*
            # Solana in one adapter, and the only one reachable when neither
            # GoldRush nor Helius is configured either.
            "bitquery_wallet_balances", self.name, ("wallet_intelligence",), self.wallet_balances,
            enabled=self.enabled,
            matches=lambda request: _has_chain_and_address(request) and bool(re.search(r"\b(?:balances?|holdings?)\b", request, re.IGNORECASE)),
            keywords=("balances", "holdings", "wallet", "tokens"), chains=tuple(self._EVM_NETWORKS) + ("solana",),
            cost_usd=settings.bitquery_request_cost_usd, quota_per_minute=settings.bitquery_requests_per_minute,
            cache_ttl_seconds=30, priority=7,
        ))
        router.register(ProviderTool(
            # Solana-only complement to goldrush_token_top_holders (EVM only,
            # Covalent explicitly rejects Solana for this endpoint -- see
            # its docstring). Registered under the same capabilities a "top
            # holders" request was verified live to classify into.
            "bitquery_token_top_holders", self.name, ("token_discovery", "token_security"), self.token_top_holders,
            enabled=self.enabled,
            matches=lambda request: _has_chain_and_address(request) and _chain(request) == "solana"
                and bool(re.search(r"\b(?:top\s+)?holders?\b", request, re.IGNORECASE)),
            keywords=("holders", "top holders"), chains=("solana",),
            cost_usd=settings.bitquery_request_cost_usd, quota_per_minute=settings.bitquery_requests_per_minute,
            cache_ttl_seconds=300, priority=9,
            description="Top wallet holders and their percentage of supply for a Solana token, by mint address",
        ))


class MarketSentimentProvider:
    """Crypto-wide mood indicators: Fear & Greed and Altcoin Season.

    Both sources are free, keyless public endpoints (verified live) -- no
    API key or settings flag gates this provider the way the paid ones are.
    """
    name = "market_sentiment"
    _FEAR_GREED_URL = "https://api.alternative.me/fng/"
    _ALTCOIN_SEASON_URL = "https://pro-api.coinmarketcap.com/public-api/v1/altcoin-season-index/latest"

    def snapshot(self, _request: str) -> str:
        with httpx.Client(timeout=settings.provider_request_timeout_seconds) as client:
            fear_greed_response = client.get(self._FEAR_GREED_URL, params={"limit": 1})
            fear_greed_response.raise_for_status()
            fear_greed = fear_greed_response.json()
            altcoin_response = client.get(self._ALTCOIN_SEASON_URL)
            altcoin_response.raise_for_status()
            altcoin = altcoin_response.json()

        fg_row = ((fear_greed or {}).get("data") or [{}])[0]
        fg_value = fg_row.get("value")
        fg_label = fg_row.get("value_classification")

        alt_data = (altcoin or {}).get("data") or {}
        alt_index = alt_data.get("altcoin_index")
        alt_high, alt_low = alt_data.get("yearly_high"), alt_data.get("yearly_low")

        if fg_value is None and alt_index is None:
            raise RuntimeError("Sentiment providers returned no data")

        season_label = "—"
        if isinstance(alt_index, (int, float)):
            season_label = (
                "Altcoin season" if alt_index >= 75 else
                "Bitcoin season" if alt_index <= 25 else "Neutral / mixed"
            )
        lines = [
            "# Market sentiment snapshot",
            f"**Data freshness**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
        ]
        if fg_value is not None:
            lines.append(f"- **Fear & Greed Index**: {fg_value}/100 -- {fg_label or '—'}")
        if alt_index is not None:
            lines.append(
                f"- **Altcoin Season Index**: {alt_index}/100 -- {season_label} (0 = full Bitcoin season, "
                "100 = full altcoin season; altcoin season means 75%+ of the top 100 coins beat Bitcoin "
                "over the trailing 90 days)"
            )
            if alt_high is not None and alt_low is not None:
                lines.append(f"  - 52-week range: {alt_low}–{alt_high}")
        lines.extend([
            "",
            "Source: [Alternative.me Fear & Greed Index](https://alternative.me/crypto/fear-and-greed-index/) "
            "· [CoinMarketCap Altcoin Season Index](https://coinmarketcap.com/charts/altcoin-season-index/)",
            "",
            "**Note**: sentiment indices are lagging/contrarian mood signals, not price predictions -- pair "
            "with your own analysis before acting on them.",
        ])
        return compact_tool_result("\n".join(lines))

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "market_sentiment_snapshot", self.name, ("market_sentiment",), self.snapshot,
            matches=lambda request: bool(re.search(
                r"\b(?:fear\s*(?:and|&)\s*greed|fear/greed|altcoin\s*season|alt\s*season|"
                r"market\s+sentiment|crypto\s+sentiment|greed\s+index)\b", request, re.IGNORECASE
            )),
            keywords=("sentiment", "fear", "greed", "altcoin season", "alt season"),
            chains=(), quota_per_minute=60, cache_ttl_seconds=300, priority=8,
        ))


MARKET_PROVIDERS = (DexScreenerProvider, BirdeyeProvider, MobulaProvider, BitqueryProvider, MarketSentimentProvider)
