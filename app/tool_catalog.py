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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DIMENSION_PATTERNS: dict[str, re.Pattern] = {
    "volume": re.compile(r"\bvolumes?\b|\bmost\s+traded\b|\bturnover\b", re.I),
    "price_change": re.compile(r"\bgainers?\b|\blosers?\b|\bmovers?\b|\bwinners?\b|\bperformers?\b|%\s*change|\bup\s+the\s+most\b|\bdown\s+the\s+most\b|\bpump(?:ing|ed)?\b|\bdump(?:ing|ed)?\b|\bbiggest\s+(?:moves?|drops?|jumps?)\b", re.I),
    "boosts": re.compile(r"\bboost(?:ed|s)?\b|\bpromot(?:ed|ion|ions)\b|\bpaid\s+(?:ads?|attention)\b", re.I),
    "narratives": re.compile(r"\bnarratives?\b|\bmetas?\b|\bthemes?\b|\bsectors?\s+(?:trending|hot)\b", re.I),
    "new_listings": re.compile(r"\b(?:new(?:ly)?|latest|just|recently)\s+(?:launched|created|listed|deployed|minted|tokens?|pairs?|pools?|coins?|profiles?)\b|\bfresh\s+(?:launches?|pairs?)\b", re.I),
    "liquidity": re.compile(r"\bliquidity\b|\bliquidity\s+pools?\b|\blp\b", re.I),
    "holders": re.compile(r"\bholders?\b|\bwhales?\b|\bconcentration\b|\btop\s+wallets\b|\bdistribution\b", re.I),
    "security": re.compile(r"\bsafe(?:ty)?\b|\brug(?:pull)?\b|\bscam\b|\bhoneypot\b|\baudit(?:ed)?\b|\bmintable\b|\bfreeze\b|\bblacklist\b|\b(?:buy|sell)\s+tax\b|\bsellab\w+\b|\brisk(?:y)?\b", re.I),
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
    _spec("coingecko_top_volume", "CoinGecko markets", ["GET /coins/markets (vs_currency=usd, per_page, price_change_percentage=24h[, category=<chain>-ecosystem])"],
          ["query"], ["symbol", "price", "24h volume", "market cap", "24h change"], {"volume"},
          not_for={"boosts", "narratives", "new_listings"}, coverage="market-wide, or one chain's CoinGecko ecosystem category (" + _EVM + ", solana, sui)",
          freshness="minutes; ranking is client-side (the free tier ignores order=)",
          answers=["trending tokens by volume in 24hrs", "top coins by 24h volume on solana", "most traded tokens today"],
          not_answers=["trending tokens on pump.fun (paid boosts)", "top gainers today (price change)"],
          summary="Tokens ranked by 24h trading volume, market-wide or within one chain's ecosystem; a volume ranking, never paid boosts"),
    _spec("coingecko_gainers_losers", "CoinGecko markets", ["GET /coins/markets?category=<chain>-ecosystem&price_change_percentage=24h"],
          ["chain"], ["symbol", "price", "24h change", "24h volume"], {"price_change"},
          not_for={"boosts", "volume", "narratives"}, coverage=_EVM + ", solana, sui (CoinGecko ecosystem categories)", freshness="minutes; client-side ranking, $10K volume floor",
          answers=["top gainers on solana", "biggest losers on base today"], not_answers=["top tokens by volume (volume ranking)", "trending tokens on base (attention)"],
          summary="Top gaining or losing tokens by 24h price change within one chain's ecosystem"),
    _spec("dexscreener_boosted_tokens", "DEX Screener token boosts", ["GET /token-boosts/top/v1", "GET /token-boosts/latest/v1", "GET /tokens/v1/<chain>/<addresses> (enrichment)"],
          ["query"], ["symbol", "chain", "price", "24h volume", "liquidity", "24h change", "boost amount"], {"boosts"},
          not_for={"volume", "price_change", "holders", "security"}, coverage="solana, base, " + _EVM + ", robinhood, pump.fun/launchpads", freshness="live (60s cache)",
          answers=["trending tokens on pump.fun", "hot tokens on solana right now", "boosted tokens on base"],
          not_answers=["tokens by 24h volume (CoinGecko ranking)", "top gainers (price change)"],
          summary="Tokens currently paid-boosted on DEX Screener (attention, not organic demand), with live price, volume and liquidity"),
    _spec("dexscreener_trending_metas", "DEX Screener boosts, grouped", ["GET /token-boosts/top/v1 (grouped by narrative)"],
          ["query"], ["narrative", "example tokens", "aggregate volume"], {"narratives"},
          not_for={"volume", "price_change", "holders"}, coverage="multi-chain", freshness="live (60s cache)",
          answers=["what narratives are trending", "hot metas right now"], not_answers=["trending tokens (a token list)", "tokens by volume"],
          summary="Narratives and metas currently drawing attention on DEX Screener, not a token list"),
    _spec("dexscreener_latest_profiles", "DEX Screener token profiles", ["GET /token-profiles/latest/v1"],
          ["query"], ["symbol", "chain", "address", "description", "links"], {"new_listings"},
          not_for={"volume", "price_change", "holders", "security"}, coverage="multi-chain", freshness="live",
          answers=["newest token profiles", "latest launches with a profile"], not_answers=["new pools on base (GeckoTerminal)"],
          summary="Newest token profiles published on DEX Screener (fresh launches with metadata)"),
    _spec("dexscreener_pair_search", "DEX Screener search", ["GET /latest/dex/search?q=<symbol or name>"],
          ["symbol"], ["pair", "chain", "dex", "price", "24h volume", "liquidity", "24h change", "pair age"], {"liquidity"},
          not_for={"holders", "security", "balances", "transactions", "boosts", "price_change", "narratives", "new_listings"},
          coverage="every DEX Screener chain", freshness="live",
          answers=["price and liquidity of BONK", "USELESS token pairs"], not_answers=["tokens ranked by volume (a ranking, not one token)", "boosted tokens (a list)"],
          summary="Trading pairs for one token by symbol or name: price, liquidity, volume and 24h change per pair"),
    _spec("dexscreener_token_pairs", "DEX Screener tokens", ["GET /tokens/v1/<chain>/<address>"],
          ["chain", "address"], ["pair", "dex", "price", "24h volume", "liquidity", "24h change", "buys/sells"], {"liquidity", "volume", "security"},
          not_for={"holders", "balances", "transactions"}, coverage="every DEX Screener chain", freshness="live",
          answers=["pairs for 0x... on base", "liquidity of <mint> on solana"], summary="Trading pairs for one token by chain and contract address"),
    _spec("geckoterminal_pools", "GeckoTerminal (CoinGecko DEX API)", ["GET /networks/<network>/trending_pools", "GET /networks/<network>/new_pools", "GET /networks/<network>/dexes/<dex>/pools"],
          ["chain"], ["pool", "dex", "price", "24h volume", "liquidity", "24h change", "created at"], {"new_listings", "liquidity", "volume"},
          not_for={"holders", "security", "boosts"}, coverage="solana, base, " + _EVM + ", pump.fun and other launchpads", freshness="live (60s cache)",
          answers=["trending pools on base", "new pairs on solana", "new pump.fun launches"], not_answers=["tokens by 24h volume market-wide (CoinGecko)"],
          summary="Real trending or newly-created pools on one chain or launchpad, with price, volume, liquidity and age"),
    _spec("birdeye_token_overview", "Birdeye", ["GET /defi/token_overview?address=<mint>"],
          ["chain", "address"], ["price", "24h volume", "liquidity", "market cap", "holders", "24h change", "trades"], {"liquidity", "volume", "holders"},
          not_for={"security", "balances", "transactions"}, coverage="solana, base, " + _EVM, freshness="live",
          answers=["overview of <mint> on solana"], summary="One token's overview by address: price, volume, liquidity, market cap, holder count"),
    _spec("mobula_token_details", "Mobula", ["GET /token/details?address=<address>&blockchain=<chain>"],
          ["chain", "address"], ["price", "market cap", "volume", "liquidity", "supply", "contracts per chain"], {"liquidity", "volume"},
          not_for={"holders", "security", "balances"}, coverage="solana, base, " + _EVM, freshness="live",
          answers=["token details for 0x... on ethereum"], summary="One token's market details by address across chains (price, cap, volume, liquidity, supply)"),
    _spec("coingecko_token_by_contract", "CoinGecko coins", ["GET /coins/<platform>/contract/<address>"],
          ["chain", "address"], ["name", "symbol", "price", "volume", "market cap", "24h change"], {"volume"},
          not_for={"holders", "security", "balances", "transactions"}, coverage=_EVM + ", solana", freshness="minutes",
          answers=["what is 0x... on base (identity + price)"], summary="Token identity, price, volume and market cap by chain and contract address"),
    _spec("coinmarketcap_token_by_contract", "CoinMarketCap", ["GET /v2/cryptocurrency/info?address=", "GET /v3/cryptocurrency/quotes/latest"],
          ["address"], ["name", "symbol", "price", "volume", "market cap", "24h change"], {"volume"},
          not_for={"holders", "security", "balances", "transactions"}, coverage="EVM tokens", freshness="minutes",
          answers=["CMC data for 0x..."], summary="Token identity and quote for an EVM token by contract address (CoinMarketCap)"),
    _spec("bitquery_recent_dex_trades", "Bitquery GraphQL", ["POST graphql: EVM DEXTrades / Solana DEXTradeByTokens"],
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
    _spec("solana_rpc_token_top_holders", "Solana JSON-RPC", ["getTokenLargestAccounts", "getMultipleAccounts (owner lookup)"],
          ["address"], ["owner wallet", "amount", "share of supply"], {"holders"},
          not_for={"volume", "price_change", "boosts"}, coverage="solana (keyless failover)", freshness="live",
          answers=["largest holders of <mint>"], summary="Largest 20 token accounts for a Solana mint with owner wallets and share of supply"),
    _spec("goldrush_token_top_holders", "GoldRush (Covalent)", ["GET /<chain>/tokens/<address>/token_holders_v2/"],
          ["chain", "address"], ["wallet", "balance", "share of supply"], {"holders"},
          not_for={"volume", "price_change", "boosts"}, coverage=_EVM, freshness="minutes",
          answers=["top holders of 0x... on base"], summary="Top wallet holders and their share of supply for an EVM token by contract"),
    _spec("goplus_token_security", "GoPlus", ["GET /token_security/<chain_id>?contract_addresses="],
          ["chain", "address"], ["honeypot", "blacklist", "mintable", "buy/sell tax", "owner", "holder count", "proxy"], {"security"},
          not_for={"volume", "price_change", "boosts"}, coverage=_EVM, freshness="live",
          answers=["is 0x... on bsc safe", "rug check 0x..."], summary="Token security scan for an EVM contract: honeypot, blacklist, mintable, taxes, holder flags"),
    _spec("honeypot_token_security", "honeypot.is", ["GET /IsHoneypot?address=&chainID="],
          ["chain", "address"], ["buy/sell/transfer tax", "sellable", "open source", "proxy"], {"security"},
          not_for={"volume", "price_change", "boosts"}, coverage=_EVM, freshness="live (simulation)",
          answers=["can I sell 0x... on ethereum", "honeypot check"], summary="Honeypot simulation for an EVM token: taxes, sellability, contract flags"),
    _spec("solana_token_security", "Jupiter tokens v2 + Shield", ["GET /tokens/v2/search", "GET /shield?mints="],
          ["address"], ["verified", "tags", "organic score", "holders", "authorities", "concentration", "warnings"], {"security", "holders"},
          not_for={"volume", "price_change", "boosts"}, coverage="solana", freshness="live",
          answers=["is <mint> safe", "freeze authority on <mint>"], summary="Jupiter Shield safety dossier for a Solana token by mint: verification, authorities, warnings"),
    # ---------------------------------------------------------------- wallets
    _spec("goldrush_wallet_balances", "GoldRush (Covalent)", ["GET /<chain>/address/<wallet>/balances_v2/"],
          ["chain", "wallet"], ["token", "balance", "usd value"], {"balances"},
          not_for={"security", "holders", "boosts"}, coverage=_EVM + ", solana (balances only)", freshness="live",
          answers=["balances of 0x... on base", "holdings of <wallet> on solana"], summary="Current token balances and USD values for a wallet address"),
    _spec("bitquery_wallet_balances", "Bitquery GraphQL", ["POST graphql: EVM BalanceUpdates by address"],
          ["chain", "wallet"], ["token", "balance"], {"balances"},
          not_for={"security", "holders"}, coverage=_EVM, freshness="minutes",
          answers=["what does 0x... hold on ethereum"], summary="Token balances for an EVM wallet (Bitquery; failover for GoldRush)"),
    _spec("goldrush_wallet_transactions", "GoldRush (Covalent)", ["GET /<chain>/address/<wallet>/transactions_v3/"],
          ["chain", "wallet"], ["hash", "time", "from/to", "value", "method"], {"transactions"},
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
    _spec("defillama_chain_tvl", "DefiLlama", ["GET /v2/chains"], ["chain"], ["chain", "tvl", "change"], {"tvl"},
          not_for={"fees", "yields", "volume"}, coverage="all chains DefiLlama tracks", freshness="hourly",
          answers=["TVL on solana", "which chain has the most value locked"], summary="Total value locked for a whole blockchain, or a ranked list of chains"),
    _spec("defillama_protocols", "DefiLlama", ["GET /protocols", "GET /protocol/<slug>"], ["protocol"], ["protocol", "tvl", "chains", "category", "change"], {"tvl"},
          not_for={"fees", "yields", "volume", "holders"}, coverage="all protocols DefiLlama tracks", freshness="hourly",
          answers=["Aave TVL", "top DeFi protocols by TVL"], summary="TVL for a named DeFi protocol, or a ranked protocol list"),
    _spec("defillama_fees_revenue", "DefiLlama fees", ["GET /overview/fees", "GET /summary/fees/<protocol>"], ["protocol"], ["24h fees", "30d fees", "revenue", "all-time"], {"fees"},
          not_for={"tvl", "yields", "volume"}, coverage="protocols with fee adapters", freshness="daily",
          answers=["how much does Uniswap earn in fees", "Jupiter revenue"], summary="Fees and revenue a protocol generates (24h, 30d, all-time)"),
    _spec("defillama_yields", "DefiLlama yields", ["GET https://yields.llama.fi/pools"], ["query"], ["pool", "project", "chain", "apy", "tvl", "stablecoin"], {"yields"},
          not_for={"tvl", "fees", "volume"}, coverage="all pools DefiLlama tracks", freshness="hourly",
          answers=["best USDC yields on base", "SOL staking rates"], summary="Best DeFi yields (APY) for an asset, chain or protocol"),
    # ---------------------------------------------------------------- sentiment / social / listings
    _spec("market_sentiment_snapshot", "alternative.me + CoinMarketCap", ["GET https://api.alternative.me/fng/", "altcoin season index"], ["query"],
          ["fear & greed", "altcoin season", "classification"], {"sentiment"}, not_for={"volume", "price_change", "boosts"},
          coverage="market-wide", freshness="daily", answers=["fear and greed today", "is it altcoin season"],
          summary="Market mood snapshot: Fear & Greed index and Altcoin Season index"),
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
          coverage="200 protocols in the registry plus incidents and stablecoins", freshness="ingested; hours to days",
          answers=["how does Aave E-mode work", "who competes with Uniswap", "what is a liquidity pool"],
          not_answers=["Aave TVL (live number)", "trending tokens"], summary="Cited passages from protocol docs, GitHub and governance for explanatory questions"),
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
    _spec("perplexity_people_search", "Perplexity Sonar (people)", ["POST /chat/completions (people-tuned prompt)"], ["query"], ["cited summary"],
          {"people"}, not_for={"balances", "transactions"}, coverage="the open web", freshness="live web",
          answers=["who is Anatoly Yakovenko"], summary="Web-grounded profile of a person with citations"),
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
