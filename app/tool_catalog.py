"""What every provider tool can and cannot answer -- the routing catalogue.

The router's regex gates say when a tool *may* fire; they say nothing about
what the tool's API actually returns, so a request asking for one thing
("tokens ranked by 24h volume") could be answered by a tool built for another
("tokens someone paid to boost") whenever the words overlapped. This module
fixes that at the source: every registered tool carries a ToolSpec naming the
API it calls, the inputs it needs, the fields it returns, and the *dimensions*
of a question it can answer (or explicitly cannot). The router reads the
request's asked dimensions and scores tools by fit, so the same vocabulary
that documents a tool also routes to it. `python scripts/tool_catalog_doc.py`
renders the same specs to docs/tool-catalog.md.

Dimensions are the subjects a market question can be about: a ranking by
volume, a ranking by price change, paid boosts, narratives, new listings,
holders, security, trades, balances, transactions, perps, TVL, fees, yields,
sentiment, social, exchange listings, news, people, projects, VCs, docs, URLs.

Endpoint paths and response fields below were checked 2026-09-16 against
official docs or, where docs were unreachable/gated, the live endpoint
itself (CoinGecko /coins/markets; GoPlus token_security; honeypot.is
IsHoneypot; GoldRush balances_v2; DefiLlama /protocols, fetched live;
DEX Screener /token-boosts/top/v1, fetched live; GeckoTerminal
trending_pools, fetched live; Jupiter tokens/v2 search, fetched live;
Solana RPC getTokenLargestAccounts; Bitquery DEXTrades/DEXTradeByTokens
docs). Birdeye, Mobula, CoinMarketCap, LunarCrush, RootData and the
Hyperliquid/GoldRush info endpoint were left as prior best-knowledge --
their docs sites returned 403/redirected past what WebFetch could follow
and there was no live no-key endpoint to confirm against directly; flag
for a follow-up pass with an authenticated fetch if a misroute ever
traces back to one of them.
"""

from __future__ import annotations

import re

from app.routing.lexicon import SECURITY_WORDS
from dataclasses import dataclass, field

DIMENSION_PATTERNS: dict[str, re.Pattern] = {
    "volume": re.compile(r"\bvolumes?\b|\bmost\s+traded\b|\bturnover\b", re.I),
    "price_change": re.compile(r"\bgainers?\b|\blosers?\b|\bmovers?\b|\bwinners?\b|\bperformers?\b|%\s*change|\bup\s+the\s+most\b|\bdown\s+the\s+most\b|\bpump(?:ing|ed)?\b|\bdump(?:ing|ed)?\b|\bbiggest\s+(?:moves?|drops?|jumps?)\b", re.I),
    "portfolio": re.compile(r"\bportfolios?\b|\bnet\s*worth\b|\ballocations?\b|\bpnl\b|\bp&l\b|\bprofit(?:able|s)?\b|\bwin\s*rate\b|\btrack\s+record\b", re.I),
    "boosts": re.compile(r"\bboost(?:ed|s)?\b|\bpromot(?:ed|ion|ions)\b|\bpaid\s+(?:ads?|attention)\b", re.I),
    "narratives": re.compile(r"\bnarratives?\b|\bmetas?\b|\bthemes?\b|\bsectors?\s+(?:trending|hot)\b", re.I),
    "new_listings": re.compile(r"\b(?:new(?:ly)?|latest|just|recently)\s+(?:launched|created|listed|deployed|minted|tokens?|pairs?|pools?|coins?|profiles?)\b|\bfresh\s+(?:launches?|pairs?)\b", re.I),
    "liquidity": re.compile(r"\bliquidity\b|\bliquidity\s+pools?\b|\blp\b", re.I),
    # Subjects TradingView's tools answer on the user's own account (2026-09-18).
    "technicals": re.compile(r"\btechnicals?\b|\brsi\b|\bmacd\b|\bmoving\s+averages?\b|\bindicators?\b|\boscillators?\b", re.I),
    "fundamentals": re.compile(r"\bfundamentals?\b|\bp/e\b|\bvaluation\b|\bmargins?\b|\brevenue\b|\beps\b|\banalyst\s+(?:targets?|consensus|ratings?)\b|\bprice\s+targets?\b", re.I),
    "unlocks": re.compile(r"\bunlocks?\b|\bunlocking\b|\bvesting\b|\bcliff\b|\bemissions?\s+schedule\b|\brelease\s+schedule\b|\bsupply\s+schedule\b", re.I),
    "earnings": re.compile(r"\bearnings\s+(?:date|call|report|calendar)\b|\breport(?:s|ing)?\s+earnings\b|\bnext\s+earnings\b", re.I),
    "holders": re.compile(r"\bholders?\b|\bwhales?\b|\bconcentration\b|\btop\s+wallets\b|\bdistribution\b", re.I),
    "security": re.compile(SECURITY_WORDS, re.I),          # the one security vocabulary (app/routing/lexicon.py)
    "trades": re.compile(r"\b(?:recent|latest|last)\s+(?:trades?|swaps?|buys?|sells?)\b|\bdex\s+trades?\b|\btrade\s+history\b", re.I),
    "balances": re.compile(r"\bbalances?\b|\bholdings?\b|\bportfolio\b|\bwhat\s+do\s+i\s+(?:hold|own)\b", re.I),
    "transactions": re.compile(r"\btransactions?\b|\btxs?\b|\bactivity\b|\btransfers?\b|\bwallet\s+history\b", re.I),
    "perps": re.compile(r"\bhyperliquid\b|\bperps?\b|\bperpetuals?\b|\bfunding\s+rate\b|\bopen\s+interest\b|\bliquidation\s+price\b|\bleverage\b", re.I),
    "tvl": re.compile(r"\btvl\b|\btotal\s+value\s+locked\b|\bvalue\s+locked\b", re.I),
    "fees": re.compile(r"\bfees?\b|\brevenues?\b|\bearnings\b|\bprotocol\s+income\b", re.I),
    "yields": re.compile(r"\bapy\b|\bapr\b|\byields?\b|\byield\s+farming\b|\bstaking\s+rates?\b|\blending\s+rates?\b|\bbest\s+(?:rates?|returns?)\b", re.I),
    "sentiment": re.compile(r"\bfear\s*(?:and|&|/)\s*greed\b|\baltcoin\s+season\b|\balt\s+season\b|\bmarket\s+(?:mood|sentiment)\b|\bsentiment\b", re.I),
    "social": re.compile(r"\bkols?\b|\btwitter\b|\bx\.com\b|\btweets?\b|\binfluencers?\b|\bsocial\b|\bgalaxy\s+score\b", re.I),
    "listings": re.compile(r"\b(?:de)?list(?:ed|ing|ings)\b\s*(?:on|announcement|by)?|\bexchange\s+announcements?\b", re.I),
    "news": re.compile(r"\bnews\b|\bheadlines?\b|\bannounce(?:d|ment|ments)?\b|\bwhat\s+happened\b|\bwhy\s+(?:is|did|was)\b", re.I),
    "people": re.compile(r"\bfounders?\b|\bceo\b|\bcto\b|\bwho\s+(?:is|founded|runs|built)\b|\bteam\s+behind\b", re.I),
    "projects": re.compile(r"\bproject\s+(?:info|profile|background|overview)\b|\bwhat\s+is\s+[a-z0-9]+\s+(?:protocol|project)\b|\bbackground\s+on\b", re.I),
    "vcs": re.compile(r"\binvest(?:ed|ors?|ments?)\b|\bbackers?\b|\bfunding\s+rounds?\b|\braised\b|\bvcs?\b|\bventure\b|\bportfolio\s+companies\b", re.I),
    "docs": re.compile(r"\bhow\s+does\b|\bhow\s+do\b|\bexplain\b|\bdocs?\b|\bdocumentation\b|\bgovernance\b|\bproposals?\b|\bwhat\s+is\s+(?:a|an|the)\b", re.I),
    "url": re.compile(r"https?://\S+", re.I),
}


def asked_dimensions(request: str) -> frozenset[str]:
    """The subjects a request asks about, from the shared vocabulary above."""
    text = request or ""
    return frozenset(name for name, pattern in DIMENSION_PATTERNS.items() if pattern.search(text))


@dataclass(frozen=True)
class ToolSpec:
    name: str
    api: str                                   # provider + product, human readable
    endpoints: tuple[str, ...]                 # endpoints or docs pages the tool calls
    requires: tuple[str, ...]                  # inputs the request must carry: address, chain, symbol, wallet, protocol, url, query
    returns: tuple[str, ...]                   # fields in the answer
    dimensions: frozenset[str]                 # subjects it can answer (rank, filter or report on)
    not_for: frozenset[str] = frozenset()      # subjects it must never be chosen for, even when words overlap
    coverage: str = ""                         # chains / scope
    freshness: str = ""                        # how live the data is
    answers: tuple[str, ...] = ()              # example requests it is the right tool for
    not_answers: tuple[str, ...] = ()          # example requests that look similar but belong elsewhere
    summary: str = ""                          # one line for the semantic fallback and the doc

    def fit(self, request: str) -> float:
        """Routing bonus: +2.5 per asked dimension this tool serves, -6 per
        asked dimension it explicitly cannot serve. Zero when nothing is asked."""
        asked = asked_dimensions(request)
        if not asked:
            return 0.0
        return 2.5 * len(asked & self.dimensions) - 6.0 * len(asked & self.not_for)


def _spec(name, api, endpoints, requires, returns, dimensions, *, not_for=(), coverage="", freshness="", answers=(), not_answers=(), summary=""):
    return ToolSpec(name, api, tuple(endpoints), tuple(requires), tuple(returns), frozenset(dimensions), frozenset(not_for),
                    coverage, freshness, tuple(answers), tuple(not_answers), summary)


_EVM = "ethereum, base, arbitrum, optimism, bsc, polygon, avalanche"

TOOL_SPECS: dict[str, ToolSpec] = {spec.name: spec for spec in [
    # ---------------------------------------------------------------- market data / discovery
    _spec("coingecko_top_volume", "CoinGecko markets", [
              "GET /coins/markets?vs_currency=usd&order=volume_desc&per_page=<=250&page=&price_change_percentage=24h[&category=<chain>-ecosystem]",
          ], ["query"], [
              "id", "symbol", "name", "current_price", "market_cap", "market_cap_rank", "total_volume",
              "price_change_percentage_24h", "high_24h", "low_24h", "circulating_supply", "ath", "last_updated",
          ], {"volume"},
          not_for={"boosts", "narratives", "new_listings"}, coverage="market-wide (order=volume_desc, up to 250/page), or one chain's CoinGecko ecosystem category (" + _EVM + ", solana, sui)",
          freshness="Demo/keyless tier refreshes every 60s per CoinGecko's docs; ranking is done client-side here -- live-verified 2026-09 that order=volume_desc is silently ignored on the free/demo tier",
          answers=["trending tokens by volume in 24hrs", "top coins by 24h volume on solana", "most traded tokens today"],
          not_answers=["trending tokens on pump.fun (paid boosts)", "top gainers today (price change)"],
          summary="Tokens ranked by 24h trading volume, market-wide or within one chain's ecosystem; a volume ranking, never paid boosts"),
    _spec("coingecko_gainers_losers", "CoinGecko markets", [
              "GET /coins/markets?vs_currency=usd&category=<chain>-ecosystem&price_change_percentage=24h&per_page=100",
          ], ["chain"], ["id", "symbol", "current_price", "price_change_percentage_24h", "total_volume", "market_cap"], {"price_change"},
          not_for={"boosts", "volume", "narratives"}, coverage=_EVM + ", solana, sui (CoinGecko ecosystem categories)",
          freshness="Demo tier refreshes every 60s per CoinGecko's docs; client-side ranking, $10K volume floor applied here",
          answers=["top gainers on solana", "biggest losers on base today"], not_answers=["top tokens by volume (volume ranking)", "trending tokens on base (attention)"],
          summary="Top gaining or losing tokens by 24h price change within one chain's ecosystem"),
    _spec("dexscreener_boosted_tokens", "DEX Screener token boosts", [
              "GET /token-boosts/top/v1  (returns: url, chainId, tokenAddress, totalAmount, icon, header, description, links, openGraph -- 60 req/min)",
              "GET /token-boosts/latest/v1",
              "GET /tokens/v1/{chainId}/{tokenAddresses}  (price/volume/liquidity enrichment for the boosted addresses)",
          ], ["query"], ["chainId", "tokenAddress", "totalAmount (boost spend, not a market metric)", "price", "24h volume", "liquidity", "24h change"], {"boosts"},
          not_for={"volume", "price_change", "holders", "security"}, coverage="solana, base, " + _EVM + ", robinhood, pump.fun/launchpads", freshness="live (60s cache)",
          answers=["trending tokens on pump.fun", "hot tokens on solana right now", "boosted tokens on base"],
          not_answers=["tokens by 24h volume (CoinGecko ranking)", "top gainers (price change)"],
          summary="Tokens currently paid-boosted on DEX Screener (attention, not organic demand), with live price, volume and liquidity"),
    _spec("dexscreener_trending_metas", "DEX Screener boosts, grouped", ["GET /token-boosts/top/v1 (grouped by narrative)"],
          ["query"], ["narrative", "example tokens", "aggregate volume"], {"narratives"},
          not_for={"volume", "price_change", "holders", "social"}, coverage="multi-chain", freshness="live (60s cache)",
          answers=["what narratives are trending", "hot metas right now"], not_answers=["trending tokens (a token list)", "tokens by volume"],
          summary="Narratives and metas currently drawing attention on DEX Screener, not a token list"),
    _spec("dexscreener_latest_profiles", "DEX Screener token profiles", ["GET /token-profiles/latest/v1  (returns: url, chainId, tokenAddress, icon, header, description, links -- 60 req/min)"],
          ["query"], ["chainId", "tokenAddress", "description", "links"], {"new_listings"},
          not_for={"volume", "price_change", "holders", "security"}, coverage="multi-chain", freshness="live",
          answers=["newest token profiles", "latest launches with a profile"], not_answers=["new pools on base (GeckoTerminal)"],
          summary="Newest token profiles published on DEX Screener (fresh launches with metadata)"),
    _spec("dexscreener_pair_search", "DEX Screener search", ["GET /latest/dex/search?q=<symbol or name>"],
          ["symbol"], ["pair", "chain", "dex", "price", "24h volume", "liquidity", "24h change", "pair age"], {"liquidity"},
          not_for={"holders", "security", "balances", "transactions", "boosts", "price_change", "narratives", "new_listings", "unlocks"},
          coverage="every DEX Screener chain", freshness="live",
          answers=["price and liquidity of BONK", "USELESS token pairs"], not_answers=["tokens ranked by volume (a ranking, not one token)", "boosted tokens (a list)"],
          summary="Trading pairs for one token by symbol or name: price, liquidity, volume and 24h change per pair"),
    _spec("dexscreener_token_pairs", "DEX Screener tokens", ["GET /tokens/v1/<chain>/<address>"],
          ["chain", "address"], ["pair", "dex", "price", "24h volume", "liquidity", "24h change", "buys/sells"], {"liquidity", "volume", "security"},
          not_for={"holders", "balances", "transactions"}, coverage="every DEX Screener chain", freshness="live",
          answers=["pairs for 0x... on base", "liquidity of <mint> on solana"], summary="Trading pairs for one token by chain and contract address"),
    _spec("geckoterminal_pools", "GeckoTerminal (CoinGecko DEX API)", [
              "GET /networks/{network}/trending_pools",
              "GET /networks/{network}/new_pools",
              "GET /networks/{network}/dexes/{dex}/pools",
              "  (JSON:API; data[].attributes: name, address, base_token_price_usd, quote_token_price_usd, "
              "price_change_percentage.{m5,m15,m30,h1,h6,h24}, volume_usd.{m5,...,h24}, reserve_in_usd, "
              "fdv_usd, market_cap_usd, pool_created_at, transactions.{...} -- confirmed live 2026-09)",
          ], ["chain"], ["name", "address", "base_token_price_usd", "volume_usd.h24", "reserve_in_usd", "price_change_percentage.h24", "fdv_usd", "market_cap_usd", "pool_created_at"], {"new_listings", "liquidity", "volume"},
          not_for={"holders", "security", "boosts"}, coverage="solana, base, " + _EVM + ", pump.fun and other launchpads", freshness="live (60s cache)",
          answers=["trending pools on base", "new pairs on solana", "new pump.fun launches"], not_answers=["tokens by 24h volume market-wide (CoinGecko)"],
          summary="Real trending or newly-created pools on one chain or launchpad, with price, volume, liquidity and age"),
    _spec("birdeye_token_overview", "Birdeye", ["GET /defi/token_overview?address=<mint>"],
          ["chain", "address"], ["price", "24h volume", "liquidity", "market cap", "holders", "24h change", "trades"], {"liquidity", "volume", "holders"},
          not_for={"security", "balances", "transactions"}, coverage="solana, base, " + _EVM, freshness="live",
          answers=["overview of <mint> on solana"], summary="One token's overview by address: price, volume, liquidity, market cap, holder count"),
    _spec("mobula_token_holders", "Mobula holder positions", ["GET /api/2/token/holder-positions?address=<address>&blockchain=<chain>"], ["address", "chain"],
          ["walletAddress", "percentageOfTotalSupply", "tokenAmountUSD", "buys", "sells", "realizedPnlUSD", "unrealizedPnlUSD", "firstTradeAt", "labels"],
          {"holders", "security"}, not_for={"price_change", "volume"}, coverage="Solana and EVM chains Mobula indexes", freshness="live",
          answers=["top holders of this memecoin", "is this token bundled", "are snipers still holding", "holder concentration"],
          summary="Who holds a token: share of supply, trading, PnL and behaviour labels per wallet"),
    _spec("mobula_token_trades", "Mobula token trades", ["GET /api/2/token/trades?address=<address>&blockchain=<chain>"], ["address", "chain"],
          ["date", "type", "baseTokenAmountUSD", "baseTokenPriceUSD", "swapSenderAddress", "platform", "transactionHash"],
          {"trades", "transactions"}, not_for={"holders", "security"}, coverage="Solana and EVM chains Mobula indexes", freshness="live",
          answers=["latest trades for this token", "recent buys and sells", "who is trading it right now"],
          summary="The latest indexed swaps for a token with size, price, wallet and venue"),
    _spec("mobula_wallet_deployer", "Mobula deployer", ["GET /api/2/wallet/deployer?wallet=<address>&blockchain=<chain>"], ["wallet"],
          ["token name", "symbol", "deployedAt", "marketCapUSD"], {"security"}, not_for={"price_change", "holders"},
          coverage="Solana and EVM chains Mobula indexes", freshness="live",
          answers=["what else did this dev deploy", "deployer track record", "who launched this token"],
          summary="The other tokens a wallet has deployed, for checking a memecoin developer's track record"),
    _spec("mobula_token_first_buyers", "Mobula first buyers", ["GET /api/1/token/first-buyers?asset=<address>&blockchain=<chain>&limit=100"], ["address", "chain"],
          ["address", "initialAmount", "currentBalance", "firstHoldingDate", "tags"], {"holders", "security"}, not_for={"price_change", "volume"},
          coverage="Solana and EVM chains Mobula indexes; up to 100 buyers", freshness="live",
          answers=["who bought this token first", "are the snipers still holding", "did early buyers exit"],
          summary="The first wallets into a token, whether they still hold, and which are tagged as snipers"),
    _spec("mobula_token_bundle", "Mobula first buyers + wallet funding", ["GET /api/1/token/first-buyers?asset=", "GET /api/2/wallet/funding?wallet= (first 25 buyers)"],
          ["address", "chain"], ["same-second buyer groups", "shared-funder clusters", "overlap", "retained share", "verdict"], {"security", "holders"},
          not_for={"price_change", "volume"}, coverage="Solana and EVM chains Mobula indexes", freshness="live",
          answers=["is this token bundled", "were the first buys coordinated", "are the early wallets linked"],
          summary="Bundle reconstruction: same-second first buys and shared funding sources among the earliest wallets"),
    _spec("mobula_new_launches", "Mobula Pulse", ["GET /api/2/pulse?chainId=<chain>&limit=<n>"], ["chain"],
          ["new[]", "bonding[]", "bonded[]: symbol, name, price, marketCap, liquidity, holdersCount, createdAt"], {"new_listings"},
          not_for={"security", "holders"}, coverage="Solana, Base, BNB Chain, Ethereum, HyperEVM", freshness="live",
          answers=["new launches on solana", "what is bonding on pump.fun", "graduated tokens on base today"],
          summary="What is launching right now: new, bonding and graduated tokens on a chain"),
    _spec("binance_spot_listing", "Binance exchangeInfo", ["GET https://api.binance.com/api/v3/exchangeInfo?permissions=SPOT (no key)"], ["symbol"],
          ["symbol", "status", "baseAsset", "quoteAsset"], {"listings"}, not_for={"price_change", "holders", "security"},
          coverage="Binance spot; not futures, not Binance Alpha", freshness="10 minutes",
          answers=["is BONK listed on Binance", "which Binance pairs trade PEPE"], summary="Whether a token is listed on Binance spot and which pairs trade, from Binance itself"),
    _spec("mobula_token_security", "Mobula token security", ["GET /api/2/token/security?blockchain=<chain>&address=<address>"], ["address", "chain"],
          ["liquidityAnalysis (burned/locked/contract/unlocked % per pool, topHolders, protocol)", "top10/50/100HoldingsPercentage",
           "isHoneypot", "isMintable", "isFreezable", "transferPausable", "renounced", "buy/sell/transferFeePercentage", "isLaunchpadToken"],
          {"security", "holders"}, not_for={"volume", "price_change"}, coverage="Solana and every EVM chain Mobula indexes",
          freshness="live", answers=["is the liquidity locked for this memecoin", "can this token be rugged", "is 0x… a honeypot"],
          summary="Whether a token's liquidity can be pulled: LP burned/locked/unlocked per pool, holder concentration, fees and the mint/freeze/pause switches"),
    # ---------------------------------------------------------------- wallet tracking (Mobula, first by user decision 2026-09-18)
    _spec("mobula_wallet_portfolio", "Mobula wallet", ["GET /api/1/wallet/portfolio?wallet=<address>"], ["wallet"],
          ["symbol", "chains", "token_balance", "price", "estimated_balance", "price_change_24h", "allocation", "total_wallet_balance"],
          {"balances", "portfolio"}, not_for={"security", "holders", "perps"}, coverage="40+ chains in one call", freshness="live",
          answers=["0x… wallet portfolio", "what does this wallet hold", "net worth of this address"],
          not_answers=["open perps for 0x… (goldrush_hyperliquid_positions)"],
          summary="Everything a wallet holds now across 40+ chains, priced, with each holding's share"),
    _spec("mobula_wallet_history", "Mobula wallet", ["GET /api/1/wallet/transactions?wallet=<address>", "GET /api/1/wallet/history?wallet=<address>"], ["wallet"],
          ["timestamp", "type", "asset", "amount", "amount_usd", "blockchain", "balance_history"],
          {"transactions", "balances"}, not_for={"security", "holders", "perps"}, coverage="40+ chains", freshness="live",
          answers=["when did this wallet buy BONK", "recent activity for 0x…", "how has this wallet's value moved"],
          summary="A wallet's dated transfers and trades, and its portfolio value over time"),
    _spec("mobula_wallet_analysis", "Mobula wallet", ["GET /api/2/wallet/analysis?wallet=<address>"], ["wallet"],
          ["totalValue", "periodTotalPnlUSD", "periodRealizedPnlUSD", "winRateDistribution", "marketCapDistribution", "fundingInfo", "labels"],
          {"portfolio"}, not_for={"security", "holders"}, coverage="40+ chains", freshness="live",
          answers=["is this wallet profitable", "pnl for 0x…", "what is this wallet's win rate"],
          summary="How a wallet has performed: realized PnL, win-rate and market-cap distribution, first funding, labels"),
    _spec("mobula_token_details", "Mobula", ["GET /token/details?address=<address>&blockchain=<chain>"],
          ["chain", "address"], ["price", "market cap", "volume", "liquidity", "supply", "contracts per chain"], {"liquidity", "volume"},
          not_for={"holders", "security", "balances"}, coverage="solana, base, " + _EVM, freshness="live",
          answers=["token details for 0x... on ethereum"], summary="One token's market details by address across chains (price, cap, volume, liquidity, supply)"),
    _spec("coingecko_token_by_contract", "CoinGecko coins", ["GET /coins/{platform}/contract/{address}"],
          ["chain", "address"], ["id", "symbol", "name", "market_data.current_price", "market_data.total_volume", "market_data.market_cap", "market_data.price_change_percentage_24h"], {"volume"},
          not_for={"holders", "security", "balances", "transactions"}, coverage=_EVM + ", solana", freshness="minutes",
          answers=["what is 0x... on base (identity + price)"], summary="Token identity, price, volume and market cap by chain and contract address"),
    _spec("coinmarketcap_token_by_contract", "CoinMarketCap", ["GET /v2/cryptocurrency/info?address=", "GET /v3/cryptocurrency/quotes/latest"],
          ["address"], ["name", "symbol", "price", "volume", "market cap", "24h change"], {"volume"},
          not_for={"holders", "security", "balances", "transactions"}, coverage="EVM tokens", freshness="minutes",
          answers=["CMC data for 0x..."], summary="Token identity and quote for an EVM token by contract address (CoinMarketCap)"),
    _spec("bitquery_recent_dex_trades", "Bitquery GraphQL", [
              "POST https://streaming.bitquery.io/graphql -- EVM: EVM(dataset:combined){DEXTrades(...)} "
              "(Block.Time, Trade.Buy/Sell.Currency, Trade.Buy/Sell.Amount, Trade.Buy/Sell.Price, Trade.Dex.ProtocolName, Transaction.Hash); "
              "Solana: a separate DEXTradeByTokens query per Bitquery's docs, not the EVM DEXTrades shape",
          ],
          ["chain", "address"], ["time", "dex", "side", "amount", "price", "trader"], {"trades"},
          not_for={"holders", "balances", "security"}, coverage="solana, " + _EVM, freshness="live",
          answers=["recent trades of 0x... on base", "last swaps for <mint>"], summary="Recent DEX trades for one token by chain and address"),
    _spec("goldrush_hyperliquid_market", "GoldRush Hyperliquid", ["POST https://hypercore.goldrushdata.com/info (metaAndAssetCtxs)"],
          ["symbol"], ["mark price", "oracle price", "funding rate", "open interest", "24h volume"], {"perps"},
          not_for={"holders", "security", "boosts"}, coverage="Hyperliquid perpetuals", freshness="live",
          answers=["ETH funding rate on hyperliquid", "open interest for SOL perps"], summary="Hyperliquid perpetual futures market data: mark/oracle price, funding, open interest, volume"),
    # ---------------------------------------------------------------- holders / security
    _spec("bitquery_token_top_holders", "Bitquery GraphQL", ["POST graphql: Solana BalanceUpdates by mint"],
          ["chain", "address"], ["wallet", "balance", "share of supply"], {"holders"},
          not_for={"volume", "price_change", "boosts", "security"}, coverage="solana", freshness="minutes",
          answers=["top holders of <mint>"], summary="Top wallet holders and their share of supply for a Solana token by mint"),
    _spec("solana_rpc_token_top_holders", "Solana JSON-RPC", ["getTokenLargestAccounts(pubkey, {commitment}) -- caps at the 20 largest token accounts, per Solana's own docs", "getMultipleAccounts (owner lookup)"],
          ["address"], ["address (token account)", "amount (raw, base-10 string)", "decimals", "uiAmountString"], {"holders"},
          not_for={"volume", "price_change", "boosts"}, coverage="solana (keyless failover)", freshness="live",
          answers=["largest holders of <mint>"], summary="Largest 20 token accounts for a Solana mint with owner wallets and share of supply"),
    _spec("goldrush_token_top_holders", "GoldRush (Covalent)", ["GET /v1/{chainName}/tokens/{address}/token_holders_v2/  (same balances_v2 field shape, per holder wallet)"],
          ["chain", "address"], ["address (holder wallet)", "balance", "total_supply"], {"holders"},
          not_for={"volume", "price_change", "boosts"}, coverage=_EVM, freshness="minutes",
          answers=["top holders of 0x... on base"], summary="Top wallet holders and their share of supply for an EVM token by contract"),
    _spec("goplus_token_security", "GoPlus", ["GET /api/v1/token_security/{chain_id}?contract_addresses={address}  (20+ chain ids: 1 eth, 56 bsc, 137 polygon, 42161 arbitrum, 8453 base, 10 optimism, 43114 avalanche, ...)"],
          ["chain", "address"], [
              "is_honeypot", "is_mintable", "owner_address", "creator_address", "buy_tax", "sell_tax",
              "is_blacklisted", "is_whitelisted", "is_proxy", "is_open_source", "is_anti_whale",
              "cannot_sell_all", "transfer_pausable", "slippage_modifiable", "can_take_back_ownership",
              "hidden_owner", "external_call", "holder_count", "lp_holder_count", "trust_list",
          ], {"security"},
          not_for={"volume", "price_change", "boosts"}, coverage=_EVM, freshness="live",
          answers=["is 0x... on bsc safe", "rug check 0x..."], summary="Token security scan for an EVM contract: honeypot, blacklist, mintable, taxes, holder flags"),
    _spec("honeypot_token_security", "honeypot.is", ["GET /v2/IsHoneypot?address={address}[&chainID=][&pair=]  (chainID omitted: picks the chain with the most liquidity for that address)"],
          ["chain", "address"], [
              "honeypotResult.isHoneypot", "simulationResult.buyTax", "simulationResult.sellTax", "simulationResult.transferTax",
              "simulationSuccess", "holderAnalysis", "contractCode.openSource", "contractCode.isProxy",
          ], {"security"},
          not_for={"volume", "price_change", "boosts"}, coverage=_EVM, freshness="live (buy/sell simulation, not a static scan)",
          answers=["can I sell 0x... on ethereum", "honeypot check"], summary="Honeypot simulation for an EVM token: taxes, sellability, contract flags"),
    _spec("solana_token_security", "Jupiter tokens v2 + Shield", [
              "GET https://lite-api.jup.ag/tokens/v2/search?query={mint}  (confirmed live 2026-09: id, name, symbol, icon, decimals, holderCount, "
              "fdv, mcap, usdPrice, liquidity, isVerified, organicScore, organicScoreLabel, tags, "
              "audit.{mintAuthorityDisabled,freezeAuthorityDisabled,topHoldersPercentage,devMints}, "
              "stats24h.{priceChange,volumeChange,buyVolume,sellVolume,numBuys,numSells,numTraders,numNetBuyers})",
              "GET /shield?mints={mint}  (warnings array per mint)",
          ], ["address"], ["isVerified", "organicScore", "holderCount", "audit.mintAuthorityDisabled", "audit.freezeAuthorityDisabled", "audit.topHoldersPercentage", "warnings"], {"security", "holders"},
          not_for={"volume", "price_change", "boosts"}, coverage="solana", freshness="live",
          answers=["is <mint> safe", "freeze authority on <mint>"], summary="Jupiter Shield safety dossier for a Solana token by mint: verification, authorities, warnings"),
    # ---------------------------------------------------------------- wallets
    _spec("goldrush_wallet_balances", "GoldRush (Covalent)", ["GET /v1/{chainName}/address/{walletAddress}/balances_v2/?quote-currency=USD&no-spam=true"],
          ["chain", "wallet"], ["contract_address", "contract_ticker_symbol", "contract_decimals", "balance", "quote (USD value)", "quote_rate", "type", "is_native_token"], {"balances"},
          not_for={"security", "holders", "boosts"}, coverage=_EVM + ", solana (balances only)", freshness="live",
          answers=["balances of 0x... on base", "holdings of <wallet> on solana"], summary="Current token balances and USD values for a wallet address"),
    _spec("bitquery_wallet_balances", "Bitquery GraphQL", ["POST graphql: EVM BalanceUpdates by address"],
          ["chain", "wallet"], ["token", "balance"], {"balances"},
          not_for={"security", "holders"}, coverage=_EVM, freshness="minutes",
          answers=["what does 0x... hold on ethereum"], summary="Token balances for an EVM wallet (Bitquery; failover for GoldRush)"),
    _spec("goldrush_wallet_transactions", "GoldRush (Covalent)", ["GET /v1/{chainName}/address/{walletAddress}/transactions_v3/"],
          ["chain", "wallet"], ["tx_hash", "block_signed_at", "from_address", "to_address", "value", "fees_paid", "log_events"], {"transactions"},
          not_for={"balances", "security"}, coverage=_EVM, freshness="live",
          answers=["recent transactions of 0x... on arbitrum"], summary="Recent transaction history for an EVM wallet on one chain"),
    _spec("helius_wallet_transactions", "Helius", ["POST /?api-key= getTransactionsForAddress / parsed transactions"],
          ["wallet"], ["signature", "time", "type", "amounts", "counterparties"], {"transactions"},
          not_for={"balances", "security"}, coverage="solana", freshness="live",
          answers=["recent activity of <solana wallet>"], summary="Recent parsed transaction history for a Solana wallet"),
    _spec("goldrush_hyperliquid_positions", "GoldRush Hyperliquid", ["POST https://hypercore.goldrushdata.com/info (clearinghouseState)"],
          ["wallet"], ["coin", "size", "entry", "leverage", "liquidation price", "pnl"], {"perps", "balances"},
          not_for={"security", "holders"}, coverage="Hyperliquid (EVM address)", freshness="live",
          answers=["my hyperliquid positions", "open perps for 0x..."], summary="A wallet's open Hyperliquid perpetual positions, leverage and liquidation price"),
    # ---------------------------------------------------------------- DeFi data
    _spec("defillama_chain_tvl", "DefiLlama", ["GET https://api.llama.fi/v2/chains  (no API key -- confirmed live 2026-09)"], ["chain"], ["name (chain)", "tvl", "tokenSymbol"], {"tvl"},
          not_for={"fees", "yields", "volume"}, coverage="all chains DefiLlama tracks", freshness="no key required; refreshed regularly, no documented interval",
          answers=["TVL on solana", "which chain has the most value locked"], summary="Total value locked for a whole blockchain, or a ranked list of chains"),
    _spec("defillama_protocols", "DefiLlama", [
              "GET https://api.llama.fi/protocols  (no API key -- confirmed live 2026-09: id, name, symbol, chain, chains, category, tvl, chainTvls, "
              "change_1h, change_1d, change_7d, mcap, gecko_id, audits, twitter, github, listedAt)",
              "GET https://api.llama.fi/protocol/{slug}  (adds daily tvl history per chain)",
          ], ["protocol"], ["name", "tvl", "chainTvls", "category", "change_1d", "change_7d", "mcap"], {"tvl"},
          not_for={"fees", "yields", "volume", "holders"}, coverage="all protocols DefiLlama tracks", freshness="hourly",
          answers=["Aave TVL", "top DeFi protocols by TVL"], summary="TVL for a named DeFi protocol, or a ranked protocol list"),
    _spec("defillama_fees_revenue", "DefiLlama fees", ["GET https://api.llama.fi/overview/fees", "GET https://api.llama.fi/summary/fees/{protocol}"], ["protocol"], ["total24h", "total30d", "totalAllTime", "revenue24h", "chains"], {"fees"},
          not_for={"tvl", "yields", "volume"}, coverage="protocols with fee adapters", freshness="daily",
          answers=["how much does Uniswap earn in fees", "Jupiter revenue"], summary="Fees and revenue a protocol generates (24h, 30d, all-time)"),
    _spec("defillama_yields", "DefiLlama yields", ["GET https://yields.llama.fi/pools  (no API key)"], ["query"], ["pool", "project", "chain", "symbol", "apy", "apyBase", "apyReward", "tvlUsd", "stablecoin"], {"yields"},
          not_for={"tvl", "fees", "volume"}, coverage="all pools DefiLlama tracks", freshness="hourly",
          answers=["best USDC yields on base", "SOL staking rates"], summary="Best DeFi yields (APY) for an asset, chain or protocol"),
    # ---------------------------------------------------------------- sentiment / social / listings
    _spec("market_sentiment_snapshot", "alternative.me + CoinMarketCap", ["GET https://api.alternative.me/fng/", "altcoin season index"], ["query"],
          ["fear & greed", "altcoin season", "classification"], {"sentiment"}, not_for={"volume", "price_change", "boosts"},
          coverage="market-wide", freshness="daily", answers=["fear and greed today", "is it altcoin season"],
          summary="Market mood snapshot: Fear & Greed index and Altcoin Season index"),
    _spec("defillama_token_unlocks", "DefiLlama emissions", ["GET /emissions (slug list)", "GET /emission/<slug>"], ["symbol"],
          ["upcoming and recent unlock events: date, amount, unlock type, recipient category"], {"unlocks"},
          not_for={"volume", "holders", "security", "liquidity", "price_change"}, coverage="tokens with a documented vesting schedule on DefiLlama", freshness="hourly",
          answers=["OPEN token unlock schedule", "when is the next ARB unlock", "JUP vesting"], not_answers=["price of OPEN", "OPEN pairs"],
          summary="A token's unlock and vesting schedule from DefiLlama emissions"),
    _spec("x_social_trending", "LunarCrush + X search (market-wide)", ["GET https://lunarcrush.com/api4/public/coins/list/v1?sort=interactions_24h", "Perplexity web search over X posts"], ["query"],
          ["tokens and memes ranked by social interactions or by what posts are about, accounts, links, themes"], {"social", "narratives"},
          not_for={"volume", "holders", "security", "price_change"}, coverage="crypto Twitter, market-wide", freshness="hours",
          answers=["trending memes on twitter", "what is crypto twitter talking about", "hot memecoins on X right now"],
          not_answers=["what is CT saying about BONK (one token: x_kol_sentiment)", "trending tokens by volume"],
          summary="What crypto Twitter is posting about right now, market-wide, with accounts and links"),
    _spec("x_kol_sentiment", "LunarCrush + X search", ["GET https://lunarcrush.com/api4/public/coins/<symbol>/v1", "GET https://api.x.com/2/tweets/search/recent"], ["symbol"],
          ["galaxy score", "alt rank", "sentiment", "interactions", "top posts"], {"social", "sentiment"}, not_for={"volume", "holders", "security"},
          coverage="tokens LunarCrush tracks", freshness="hours", answers=["what are KOLs saying about SOL", "social sentiment on PEPE"],
          summary="Social read on one token: LunarCrush scores and sentiment with top posts"),
    _spec("exchange_listing_announcements", "Exchange announcement feeds", ["Binance / Bybit / OKX / Coinbase announcement pages"], ["symbol"],
          ["exchange", "title", "date", "link"], {"listings", "news"}, not_for={"volume", "price_change", "boosts"},
          coverage="major centralized exchanges", freshness="hours", answers=["is XYZ listed on Binance", "recent delistings"],
          summary="Official exchange listing and delisting announcements for a token"),
    # ---------------------------------------------------------------- knowledge / research
    _spec("knowledge_base_search", "Orbit knowledge service", ["hybrid FTS + pgvector retrieval over protocol docs, GitHub, governance, incidents, stablecoins"], ["query"],
          ["passages with citations", "entities", "relations"], {"docs", "projects"}, not_for={"volume", "price_change", "boosts", "balances", "transactions"},
          coverage="200 protocols in the registry, PLUS a dedicated ingested history of past security incidents/hacks, governance "
                    "votes and proposals, and investor/funder and stablecoin-backing records for those protocols -- these are real, "
                    "indexed categories here, not just docs/mechanism explainers",
          freshness="ingested; hours to days -- the right tool for a protocol's documented PAST (an incident that already "
                     "happened, a vote already taken, funding already raised), never for something breaking right now",
          answers=["how does Aave E-mode work", "who competes with Uniswap", "what is a liquidity pool",
                    "has Aave ever been hacked", "who are the investors backing EigenLayer", "what did the Lido community vote on recently"],
          not_answers=["Aave TVL (live number)", "trending tokens", "did X get hacked today (breaking news, not yet ingested)"],
          summary="Cited passages from protocol docs, GitHub, governance AND a dedicated ingested history of past incidents and "
                   "investor/funding records -- not just mechanism explainers, and not for live numbers or breaking news"),
    _spec("rootdata_project_search", "RootData", ["POST /open/ser_inv (projects)", "POST /open/get_item"], ["query"],
          ["project", "category", "funding", "investors", "team", "socials"], {"projects", "vcs"}, not_for={"volume", "price_change", "boosts", "security"},
          coverage="RootData's project database", freshness="days", answers=["background on Berachain", "who invested in EigenLayer"],
          summary="Project profile: category, funding history, investors and team from RootData"),
    _spec("rootdata_people_search", "RootData", ["POST /open/ser_inv (people)"], ["query"], ["person", "role", "projects", "socials"], {"people"},
          not_for={"volume", "price_change"}, coverage="RootData's people database", freshness="days",
          answers=["who is the founder of Jupiter"], summary="People profiles: roles, projects and socials from RootData"),
    _spec("rootdata_vc_search", "RootData", ["POST /open/ser_inv (VCs)"], ["query"], ["fund", "portfolio", "recent rounds"], {"vcs"},
          not_for={"volume", "price_change"}, coverage="RootData's investor database", freshness="days",
          answers=["a16z crypto portfolio", "who backs Monad"], summary="Venture fund profiles and portfolios from RootData"),
    _spec("perplexity_web_search", "Perplexity Sonar", ["POST /chat/completions (sonar, web-grounded)"], ["query"], ["cited summary"],
          {"news", "projects", "docs"}, not_for={"balances", "transactions", "holders"}, coverage="the open web", freshness="live web",
          answers=["why did SOL drop today", "latest on the CLARITY act"], summary="Web-grounded research summary with citations for news and open questions"),
    _spec("perplexity_finance_search", "Perplexity Sonar (finance)", ["POST /chat/completions (finance-tuned prompt)"], ["query"], ["cited summary"],
          {"news"}, not_for={"balances", "transactions", "holders", "boosts"}, coverage="equities, macro, crypto news", freshness="live web",
          answers=["NVDA earnings reaction", "Fed decision impact on crypto"], summary="Finance-focused web research with citations (equities, macro, crypto)"),
    _spec("tradingview_snapshot", "TradingView MCP (user's account)", ["search_symbols", "get_symbol_data", "get_technicals_rating"], ["symbol"],
          ["last price, change, volume, market cap, 52w range, RSI, technical rating"], {"volume", "price_change", "technicals"},
          not_for={"balances", "transactions", "holders", "security"}, coverage="crypto and equities on TradingView", freshness="live (delayed on some feeds)",
          answers=["AAPL price and technicals", "quote and RSI for SOL"], summary="Live quote, key stats and technical rating from the user's TradingView"),
    _spec("tradingview_financials", "TradingView MCP (user's account)", ["search_symbols", "get_financials", "get_forecasts"], ["symbol"],
          ["P/E, margins, ROE, growth, analyst consensus, price targets"], {"fundamentals"},
          not_for={"balances", "transactions", "holders", "security", "boosts"}, coverage="listed equities on TradingView", freshness="latest reported period",
          answers=["NVDA fundamentals and analyst targets", "is MSFT overvalued on P/E"], summary="Fundamentals and analyst forecasts from the user's TradingView"),
    _spec("tradingview_news", "TradingView MCP (user's account)", ["search_symbols", "get_news"], ["symbol"], ["headlines with sources"], {"news"},
          not_for={"balances", "transactions", "holders"}, coverage="symbols on TradingView", freshness="live",
          answers=["latest news on TSLA", "why is AAPL down today"], summary="Latest headlines for a symbol from the user's TradingView"),
    _spec("tradingview_earnings", "TradingView MCP (user's account)", ["search_symbols", "get_earnings_calendar"], ["symbol"], ["earnings dates, EPS and revenue estimates"], {"earnings"},
          not_for={"balances", "transactions", "holders"}, coverage="listed equities on TradingView", freshness="live",
          answers=["when does NVDA report earnings", "AAPL earnings date and estimates"], summary="Earnings calendar for a stock from the user's TradingView"),
    _spec("perplexity_people_search", "Perplexity Sonar (people)", ["POST /chat/completions (people-tuned prompt)"], ["query"], ["cited summary"],
          {"people"}, not_for={"balances", "transactions"}, coverage="the open web", freshness="live web",
          answers=["who is Anatoly Yakovenko"], summary="Web-grounded profile of a person with citations"),
    _spec("url_reader", "Orbit page reader", ["GET <url> (direct, browser-like)", "Perplexity fetch_url", "Perplexity web_search about the link"], ["url"],
          ["summary of the page against the user's ask", "source link"], {"url"}, not_for={"balances", "transactions", "holders"},
          coverage="any public page or post; token pages are turned into token questions before this runs", freshness="live",
          answers=["summarize https://...", "what does this article say https://...", "tl;dr https://x.com/..."],
          summary="Read a linked page three ways (direct fetch, Perplexity reader, search about the link) and summarize it with our own model"),
    _spec("perplexity_fetch_url", "Perplexity Sonar (URL)", ["POST /chat/completions with the page URL"], ["url"], ["summary of the page"], {"url"},
          not_for={"balances", "transactions"}, coverage="public pages", freshness="live", answers=["summarize https://..."],
          summary="Read and summarize one public web page by URL"),
    _spec("openai_web_search", "OpenAI Responses web search", ["POST /responses (web_search tool)"], ["query"], ["cited summary"],
          {"news", "url", "people"}, not_for={"balances", "transactions", "holders"}, coverage="the open web (failover)", freshness="live web",
          answers=["fallback web research when Perplexity is unavailable"], summary="Web search with citations via OpenAI (failover for Perplexity)"),
]}


def attach_specs(router) -> None:
    """Give every registered tool its spec (and a description for the
    semantic fallback when the registration set none). Tools without a spec
    are left as they are; test_tool_catalog fails the build for them."""
    for tool in router.tools():
        spec = TOOL_SPECS.get(tool.name)
        if spec is None:
            continue
        object.__setattr__(tool, "spec", spec)
        if not tool.description:
            object.__setattr__(tool, "description", spec.summary)


def catalog_markdown(router=None) -> str:
    """Render the catalogue as Markdown (docs/tool-catalog.md)."""
    lines = [
        "# Tool catalogue",
        "",
        "Generated from `app/tool_catalog.py` by `scripts/tool_catalog_doc.py`; do not edit by hand.",
        "Each tool lists the API it calls, the inputs a request must carry, the fields it returns,",
        "and the question *dimensions* it can answer or must never be chosen for. The router scores",
        "tools by these dimensions (`ToolSpec.fit`) on top of its regex gates and keyword hits.",
        "",
        "| Dimension | Asked when the request says |",
        "|---|---|",
    ]
    for name, pattern in DIMENSION_PATTERNS.items():
        lines.append(f"| `{name}` | `{pattern.pattern[:90]}{'…' if len(pattern.pattern) > 90 else ''}` |")
    caps = {}
    if router is not None:
        for tool in router.tools():
            caps[tool.name] = (tool.capabilities, tool.priority, tool.chains)
    for spec in sorted(TOOL_SPECS.values(), key=lambda s: s.name):
        cap, prio, chains = caps.get(spec.name, ((), None, ()))
        lines += ["", f"## {spec.name}", "", spec.summary, ""]
        lines.append(f"- **API**: {spec.api}")
        for endpoint in spec.endpoints:
            lines.append(f"  - `{endpoint}`")
        if cap:
            lines.append(f"- **Capabilities**: {', '.join(cap)} · priority {prio:g}")
        lines.append(f"- **Needs**: {', '.join(spec.requires) or 'nothing beyond the question'}")
        lines.append(f"- **Returns**: {', '.join(spec.returns)}")
        lines.append(f"- **Answers**: {', '.join(sorted(spec.dimensions))}")
        if spec.not_for:
            lines.append(f"- **Never for**: {', '.join(sorted(spec.not_for))}")
        if spec.coverage:
            lines.append(f"- **Coverage**: {spec.coverage}")
        if spec.freshness:
            lines.append(f"- **Freshness**: {spec.freshness}")
        if spec.answers:
            lines.append("- **Right for**: " + " · ".join(f"“{a}”" for a in spec.answers))
        if spec.not_answers:
            lines.append("- **Looks similar, belongs elsewhere**: " + " · ".join(f"“{a}”" for a in spec.not_answers))
    return "\n".join(lines) + "\n"
