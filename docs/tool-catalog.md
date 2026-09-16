# Tool catalogue

Generated from `app/tool_catalog.py` by `scripts/tool_catalog_doc.py`; do not edit by hand.
Each tool lists the API it calls, the inputs a request must carry, the fields it returns,
and the question *dimensions* it can answer or must never be chosen for. The router scores
tools by these dimensions (`ToolSpec.fit`) on top of its regex gates and keyword hits.

| Dimension | Asked when the request says |
|---|---|
| `volume` | `\bvolumes?\b|\bmost\s+traded\b|\bturnover\b` |
| `price_change` | `\bgainers?\b|\blosers?\b|\bmovers?\b|\bwinners?\b|\bperformers?\b|%\s*change|\bup\s+the\s+…` |
| `boosts` | `\bboost(?:ed|s)?\b|\bpromot(?:ed|ion|ions)\b|\bpaid\s+(?:ads?|attention)\b` |
| `narratives` | `\bnarratives?\b|\bmetas?\b|\bthemes?\b|\bsectors?\s+(?:trending|hot)\b` |
| `new_listings` | `\b(?:new(?:ly)?|latest|just|recently)\s+(?:launched|created|listed|deployed|minted|tokens?…` |
| `liquidity` | `\bliquidity\b|\bliquidity\s+pools?\b|\blp\b` |
| `holders` | `\bholders?\b|\bwhales?\b|\bconcentration\b|\btop\s+wallets\b|\bdistribution\b` |
| `security` | `\bsafe(?:ty)?\b|\brug(?:pull)?\b|\bscam\b|\bhoneypot\b|\baudit(?:ed)?\b|\bmintable\b|\bfre…` |
| `trades` | `\b(?:recent|latest|last)\s+(?:trades?|swaps?|buys?|sells?)\b|\bdex\s+trades?\b|\btrade\s+h…` |
| `balances` | `\bbalances?\b|\bholdings?\b|\bportfolio\b|\bwhat\s+do\s+i\s+(?:hold|own)\b` |
| `transactions` | `\btransactions?\b|\btxs?\b|\bactivity\b|\btransfers?\b|\bwallet\s+history\b` |
| `perps` | `\bhyperliquid\b|\bperps?\b|\bperpetuals?\b|\bfunding\s+rate\b|\bopen\s+interest\b|\bliquid…` |
| `tvl` | `\btvl\b|\btotal\s+value\s+locked\b|\bvalue\s+locked\b` |
| `fees` | `\bfees?\b|\brevenues?\b|\bearnings\b|\bprotocol\s+income\b` |
| `yields` | `\bapy\b|\bapr\b|\byields?\b|\byield\s+farming\b|\bstaking\s+rates?\b|\blending\s+rates?\b|…` |
| `sentiment` | `\bfear\s*(?:and|&|/)\s*greed\b|\baltcoin\s+season\b|\balt\s+season\b|\bmarket\s+(?:mood|se…` |
| `social` | `\bkols?\b|\btwitter\b|\bx\.com\b|\btweets?\b|\binfluencers?\b|\bsocial\b|\bgalaxy\s+score\…` |
| `listings` | `\b(?:de)?list(?:ed|ing|ings)\b\s*(?:on|announcement|by)?|\bexchange\s+announcements?\b` |
| `news` | `\bnews\b|\bheadlines?\b|\bannounce(?:d|ment|ments)?\b|\bwhat\s+happened\b|\bwhy\s+(?:is|di…` |
| `people` | `\bfounders?\b|\bceo\b|\bcto\b|\bwho\s+(?:is|founded|runs|built)\b|\bteam\s+behind\b` |
| `projects` | `\bproject\s+(?:info|profile|background|overview)\b|\bwhat\s+is\s+[a-z0-9]+\s+(?:protocol|p…` |
| `vcs` | `\binvest(?:ed|ors?|ments?)\b|\bbackers?\b|\bfunding\s+rounds?\b|\braised\b|\bvcs?\b|\bvent…` |
| `docs` | `\bhow\s+does\b|\bhow\s+do\b|\bexplain\b|\bdocs?\b|\bdocumentation\b|\bgovernance\b|\bpropo…` |
| `url` | `https?://\S+` |

## birdeye_token_overview

One token's overview by address: price, volume, liquidity, market cap, holder count

- **API**: Birdeye
  - `GET /defi/token_overview?address=<mint>`
- **Capabilities**: market_data · priority 5
- **Needs**: chain, address
- **Returns**: price, 24h volume, liquidity, market cap, holders, 24h change, trades
- **Answers**: holders, liquidity, volume
- **Never for**: balances, security, transactions
- **Coverage**: solana, base, ethereum, base, arbitrum, optimism, bsc, polygon, avalanche
- **Freshness**: live
- **Right for**: “overview of <mint> on solana”

## bitquery_recent_dex_trades

Recent DEX trades for one token by chain and address

- **API**: Bitquery GraphQL
  - `POST graphql: EVM DEXTrades / Solana DEXTradeByTokens`
- **Capabilities**: market_data · priority 6
- **Needs**: chain, address
- **Returns**: time, dex, side, amount, price, trader
- **Answers**: trades
- **Never for**: balances, holders, security
- **Coverage**: solana, ethereum, base, arbitrum, optimism, bsc, polygon, avalanche
- **Freshness**: live
- **Right for**: “recent trades of 0x... on base” · “last swaps for <mint>”

## bitquery_token_top_holders

Top wallet holders and their share of supply for a Solana token by mint

- **API**: Bitquery GraphQL
  - `POST graphql: Solana BalanceUpdates by mint`
- **Capabilities**: token_discovery, token_security · priority 9
- **Needs**: chain, address
- **Returns**: wallet, balance, share of supply
- **Answers**: holders
- **Never for**: boosts, price_change, security, volume
- **Coverage**: solana
- **Freshness**: minutes
- **Right for**: “top holders of <mint>”

## bitquery_wallet_balances

Token balances for an EVM wallet (Bitquery; failover for GoldRush)

- **API**: Bitquery GraphQL
  - `POST graphql: EVM BalanceUpdates by address`
- **Capabilities**: wallet_intelligence · priority 7
- **Needs**: chain, wallet
- **Returns**: token, balance
- **Answers**: balances
- **Never for**: holders, security
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche
- **Freshness**: minutes
- **Right for**: “what does 0x... hold on ethereum”

## coingecko_gainers_losers

Top gaining or losing tokens by 24h price change within one chain's ecosystem

- **API**: CoinGecko markets
  - `GET /coins/markets?category=<chain>-ecosystem&price_change_percentage=24h`
- **Capabilities**: token_discovery, market_data · priority 8
- **Needs**: chain
- **Returns**: symbol, price, 24h change, 24h volume
- **Answers**: price_change
- **Never for**: boosts, narratives, volume
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche, solana, sui (CoinGecko ecosystem categories)
- **Freshness**: minutes; client-side ranking, $10K volume floor
- **Right for**: “top gainers on solana” · “biggest losers on base today”
- **Looks similar, belongs elsewhere**: “top tokens by volume (volume ranking)” · “trending tokens on base (attention)”

## coingecko_token_by_contract

Token identity, price, volume and market cap by chain and contract address

- **API**: CoinGecko coins
  - `GET /coins/<platform>/contract/<address>`
- **Capabilities**: token_discovery, market_data · priority 2
- **Needs**: chain, address
- **Returns**: name, symbol, price, volume, market cap, 24h change
- **Answers**: volume
- **Never for**: balances, holders, security, transactions
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche, solana
- **Freshness**: minutes
- **Right for**: “what is 0x... on base (identity + price)”

## coingecko_top_volume

Tokens ranked by 24h trading volume, market-wide or within one chain's ecosystem; a volume ranking, never paid boosts

- **API**: CoinGecko markets
  - `GET /coins/markets (vs_currency=usd, per_page, price_change_percentage=24h[, category=<chain>-ecosystem])`
- **Capabilities**: token_discovery, market_data · priority 9
- **Needs**: query
- **Returns**: symbol, price, 24h volume, market cap, 24h change
- **Answers**: volume
- **Never for**: boosts, narratives, new_listings
- **Coverage**: market-wide, or one chain's CoinGecko ecosystem category (ethereum, base, arbitrum, optimism, bsc, polygon, avalanche, solana, sui)
- **Freshness**: minutes; ranking is client-side (the free tier ignores order=)
- **Right for**: “trending tokens by volume in 24hrs” · “top coins by 24h volume on solana” · “most traded tokens today”
- **Looks similar, belongs elsewhere**: “trending tokens on pump.fun (paid boosts)” · “top gainers today (price change)”

## coinmarketcap_token_by_contract

Token identity and quote for an EVM token by contract address (CoinMarketCap)

- **API**: CoinMarketCap
  - `GET /v2/cryptocurrency/info?address=`
  - `GET /v3/cryptocurrency/quotes/latest`
- **Capabilities**: token_discovery, market_data · priority 1
- **Needs**: address
- **Returns**: name, symbol, price, volume, market cap, 24h change
- **Answers**: volume
- **Never for**: balances, holders, security, transactions
- **Coverage**: EVM tokens
- **Freshness**: minutes
- **Right for**: “CMC data for 0x...”

## defillama_chain_tvl

Total value locked for a whole blockchain, or a ranked list of chains

- **API**: DefiLlama
  - `GET /v2/chains`
- **Capabilities**: defi_data · priority 9
- **Needs**: chain
- **Returns**: chain, tvl, change
- **Answers**: tvl
- **Never for**: fees, volume, yields
- **Coverage**: all chains DefiLlama tracks
- **Freshness**: hourly
- **Right for**: “TVL on solana” · “which chain has the most value locked”

## defillama_fees_revenue

Fees and revenue a protocol generates (24h, 30d, all-time)

- **API**: DefiLlama fees
  - `GET /overview/fees`
  - `GET /summary/fees/<protocol>`
- **Capabilities**: defi_data · priority 10
- **Needs**: protocol
- **Returns**: 24h fees, 30d fees, revenue, all-time
- **Answers**: fees
- **Never for**: tvl, volume, yields
- **Coverage**: protocols with fee adapters
- **Freshness**: daily
- **Right for**: “how much does Uniswap earn in fees” · “Jupiter revenue”

## defillama_protocols

TVL for a named DeFi protocol, or a ranked protocol list

- **API**: DefiLlama
  - `GET /protocols`
  - `GET /protocol/<slug>`
- **Capabilities**: defi_data · priority 10
- **Needs**: protocol
- **Returns**: protocol, tvl, chains, category, change
- **Answers**: tvl
- **Never for**: fees, holders, volume, yields
- **Coverage**: all protocols DefiLlama tracks
- **Freshness**: hourly
- **Right for**: “Aave TVL” · “top DeFi protocols by TVL”

## defillama_yields

Best DeFi yields (APY) for an asset, chain or protocol

- **API**: DefiLlama yields
  - `GET https://yields.llama.fi/pools`
- **Capabilities**: defi_data · priority 10
- **Needs**: query
- **Returns**: pool, project, chain, apy, tvl, stablecoin
- **Answers**: yields
- **Never for**: fees, tvl, volume
- **Coverage**: all pools DefiLlama tracks
- **Freshness**: hourly
- **Right for**: “best USDC yields on base” · “SOL staking rates”

## dexscreener_boosted_tokens

Tokens currently paid-boosted on DEX Screener (attention, not organic demand), with live price, volume and liquidity

- **API**: DEX Screener token boosts
  - `GET /token-boosts/top/v1`
  - `GET /token-boosts/latest/v1`
  - `GET /tokens/v1/<chain>/<addresses> (enrichment)`
- **Capabilities**: market_data, token_discovery · priority 7
- **Needs**: query
- **Returns**: symbol, chain, price, 24h volume, liquidity, 24h change, boost amount
- **Answers**: boosts
- **Never for**: holders, price_change, security, volume
- **Coverage**: solana, base, ethereum, base, arbitrum, optimism, bsc, polygon, avalanche, robinhood, pump.fun/launchpads
- **Freshness**: live (60s cache)
- **Right for**: “trending tokens on pump.fun” · “hot tokens on solana right now” · “boosted tokens on base”
- **Looks similar, belongs elsewhere**: “tokens by 24h volume (CoinGecko ranking)” · “top gainers (price change)”

## dexscreener_latest_profiles

Newest token profiles published on DEX Screener (fresh launches with metadata)

- **API**: DEX Screener token profiles
  - `GET /token-profiles/latest/v1`
- **Capabilities**: token_discovery · priority 4
- **Needs**: query
- **Returns**: symbol, chain, address, description, links
- **Answers**: new_listings
- **Never for**: holders, price_change, security, volume
- **Coverage**: multi-chain
- **Freshness**: live
- **Right for**: “newest token profiles” · “latest launches with a profile”
- **Looks similar, belongs elsewhere**: “new pools on base (GeckoTerminal)”

## dexscreener_pair_search

Trading pairs for one token by symbol or name: price, liquidity, volume and 24h change per pair

- **API**: DEX Screener search
  - `GET /latest/dex/search?q=<symbol or name>`
- **Capabilities**: market_data, token_discovery · priority 1
- **Needs**: symbol
- **Returns**: pair, chain, dex, price, 24h volume, liquidity, 24h change, pair age
- **Answers**: liquidity
- **Never for**: balances, boosts, holders, narratives, new_listings, price_change, security, transactions
- **Coverage**: every DEX Screener chain
- **Freshness**: live
- **Right for**: “price and liquidity of BONK” · “USELESS token pairs”
- **Looks similar, belongs elsewhere**: “tokens ranked by volume (a ranking, not one token)” · “boosted tokens (a list)”

## dexscreener_token_pairs

Trading pairs for one token by chain and contract address

- **API**: DEX Screener tokens
  - `GET /tokens/v1/<chain>/<address>`
- **Capabilities**: market_data, token_discovery, token_security · priority 3
- **Needs**: chain, address
- **Returns**: pair, dex, price, 24h volume, liquidity, 24h change, buys/sells
- **Answers**: liquidity, security, volume
- **Never for**: balances, holders, transactions
- **Coverage**: every DEX Screener chain
- **Freshness**: live
- **Right for**: “pairs for 0x... on base” · “liquidity of <mint> on solana”

## dexscreener_trending_metas

Narratives and metas currently drawing attention on DEX Screener, not a token list

- **API**: DEX Screener boosts, grouped
  - `GET /token-boosts/top/v1 (grouped by narrative)`
- **Capabilities**: market_data, token_discovery · priority 5
- **Needs**: query
- **Returns**: narrative, example tokens, aggregate volume
- **Answers**: narratives
- **Never for**: holders, price_change, volume
- **Coverage**: multi-chain
- **Freshness**: live (60s cache)
- **Right for**: “what narratives are trending” · “hot metas right now”
- **Looks similar, belongs elsewhere**: “trending tokens (a token list)” · “tokens by volume”

## exchange_listing_announcements

Official exchange listing and delisting announcements for a token

- **API**: Exchange announcement feeds
  - `Binance / Bybit / OKX / Coinbase announcement pages`
- **Capabilities**: listing_events · priority 10
- **Needs**: symbol
- **Returns**: exchange, title, date, link
- **Answers**: listings, news
- **Never for**: boosts, price_change, volume
- **Coverage**: major centralized exchanges
- **Freshness**: hours
- **Right for**: “is XYZ listed on Binance” · “recent delistings”

## geckoterminal_pools

Real trending or newly-created pools on one chain or launchpad, with price, volume, liquidity and age

- **API**: GeckoTerminal (CoinGecko DEX API)
  - `GET /networks/<network>/trending_pools`
  - `GET /networks/<network>/new_pools`
  - `GET /networks/<network>/dexes/<dex>/pools`
- **Capabilities**: market_data, token_discovery · priority 10
- **Needs**: chain
- **Returns**: pool, dex, price, 24h volume, liquidity, 24h change, created at
- **Answers**: liquidity, new_listings, volume
- **Never for**: boosts, holders, security
- **Coverage**: solana, base, ethereum, base, arbitrum, optimism, bsc, polygon, avalanche, pump.fun and other launchpads
- **Freshness**: live (60s cache)
- **Right for**: “trending pools on base” · “new pairs on solana” · “new pump.fun launches”
- **Looks similar, belongs elsewhere**: “tokens by 24h volume market-wide (CoinGecko)”

## goldrush_hyperliquid_market

Hyperliquid perpetual futures market data: mark/oracle price, funding, open interest, volume

- **API**: GoldRush Hyperliquid
  - `POST https://hypercore.goldrushdata.com/info (metaAndAssetCtxs)`
- **Capabilities**: market_data · priority 10
- **Needs**: symbol
- **Returns**: mark price, oracle price, funding rate, open interest, 24h volume
- **Answers**: perps
- **Never for**: boosts, holders, security
- **Coverage**: Hyperliquid perpetuals
- **Freshness**: live
- **Right for**: “ETH funding rate on hyperliquid” · “open interest for SOL perps”

## goldrush_hyperliquid_positions

A wallet's open Hyperliquid perpetual positions, leverage and liquidation price

- **API**: GoldRush Hyperliquid
  - `POST https://hypercore.goldrushdata.com/info (clearinghouseState)`
- **Capabilities**: wallet_intelligence · priority 10
- **Needs**: wallet
- **Returns**: coin, size, entry, leverage, liquidation price, pnl
- **Answers**: balances, perps
- **Never for**: holders, security
- **Coverage**: Hyperliquid (EVM address)
- **Freshness**: live
- **Right for**: “my hyperliquid positions” · “open perps for 0x...”

## goldrush_token_top_holders

Top wallet holders and their share of supply for an EVM token by contract

- **API**: GoldRush (Covalent)
  - `GET /<chain>/tokens/<address>/token_holders_v2/`
- **Capabilities**: token_discovery, token_security · priority 10
- **Needs**: chain, address
- **Returns**: wallet, balance, share of supply
- **Answers**: holders
- **Never for**: boosts, price_change, volume
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche
- **Freshness**: minutes
- **Right for**: “top holders of 0x... on base”

## goldrush_wallet_balances

Current token balances and USD values for a wallet address

- **API**: GoldRush (Covalent)
  - `GET /<chain>/address/<wallet>/balances_v2/`
- **Capabilities**: wallet_intelligence · priority 10
- **Needs**: chain, wallet
- **Returns**: token, balance, usd value
- **Answers**: balances
- **Never for**: boosts, holders, security
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche, solana (balances only)
- **Freshness**: live
- **Right for**: “balances of 0x... on base” · “holdings of <wallet> on solana”

## goldrush_wallet_transactions

Recent transaction history for an EVM wallet on one chain

- **API**: GoldRush (Covalent)
  - `GET /<chain>/address/<wallet>/transactions_v3/`
- **Capabilities**: wallet_intelligence · priority 9
- **Needs**: chain, wallet
- **Returns**: hash, time, from/to, value, method
- **Answers**: transactions
- **Never for**: balances, security
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche
- **Freshness**: live
- **Right for**: “recent transactions of 0x... on arbitrum”

## goplus_token_security

Token security scan for an EVM contract: honeypot, blacklist, mintable, taxes, holder flags

- **API**: GoPlus
  - `GET /token_security/<chain_id>?contract_addresses=`
- **Capabilities**: token_security · priority 10
- **Needs**: chain, address
- **Returns**: honeypot, blacklist, mintable, buy/sell tax, owner, holder count, proxy
- **Answers**: security
- **Never for**: boosts, price_change, volume
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche
- **Freshness**: live
- **Right for**: “is 0x... on bsc safe” · “rug check 0x...”

## helius_wallet_transactions

Recent parsed transaction history for a Solana wallet

- **API**: Helius
  - `POST /?api-key= getTransactionsForAddress / parsed transactions`
- **Capabilities**: wallet_intelligence · priority 11
- **Needs**: wallet
- **Returns**: signature, time, type, amounts, counterparties
- **Answers**: transactions
- **Never for**: balances, security
- **Coverage**: solana
- **Freshness**: live
- **Right for**: “recent activity of <solana wallet>”

## honeypot_token_security

Honeypot simulation for an EVM token: taxes, sellability, contract flags

- **API**: honeypot.is
  - `GET /IsHoneypot?address=&chainID=`
- **Capabilities**: token_security · priority 8
- **Needs**: chain, address
- **Returns**: buy/sell/transfer tax, sellable, open source, proxy
- **Answers**: security
- **Never for**: boosts, price_change, volume
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche
- **Freshness**: live (simulation)
- **Right for**: “can I sell 0x... on ethereum” · “honeypot check”

## knowledge_base_search

Cited passages from protocol docs, GitHub and governance for explanatory questions

- **API**: Orbit knowledge service
  - `hybrid FTS + pgvector retrieval over protocol docs, GitHub, governance, incidents, stablecoins`
- **Capabilities**: knowledge · priority 8
- **Needs**: query
- **Returns**: passages with citations, entities, relations
- **Answers**: docs, projects
- **Never for**: balances, boosts, price_change, transactions, volume
- **Coverage**: 200 protocols in the registry plus incidents and stablecoins
- **Freshness**: ingested; hours to days
- **Right for**: “how does Aave E-mode work” · “who competes with Uniswap” · “what is a liquidity pool”
- **Looks similar, belongs elsewhere**: “Aave TVL (live number)” · “trending tokens”

## market_sentiment_snapshot

Market mood snapshot: Fear & Greed index and Altcoin Season index

- **API**: alternative.me + CoinMarketCap
  - `GET https://api.alternative.me/fng/`
  - `altcoin season index`
- **Capabilities**: market_sentiment · priority 8
- **Needs**: query
- **Returns**: fear & greed, altcoin season, classification
- **Answers**: sentiment
- **Never for**: boosts, price_change, volume
- **Coverage**: market-wide
- **Freshness**: daily
- **Right for**: “fear and greed today” · “is it altcoin season”

## mobula_token_details

One token's market details by address across chains (price, cap, volume, liquidity, supply)

- **API**: Mobula
  - `GET /token/details?address=<address>&blockchain=<chain>`
- **Capabilities**: market_data, token_discovery · priority 4
- **Needs**: chain, address
- **Returns**: price, market cap, volume, liquidity, supply, contracts per chain
- **Answers**: liquidity, volume
- **Never for**: balances, holders, security
- **Coverage**: solana, base, ethereum, base, arbitrum, optimism, bsc, polygon, avalanche
- **Freshness**: live
- **Right for**: “token details for 0x... on ethereum”

## openai_web_search

Web search with citations via OpenAI (failover for Perplexity)

- **API**: OpenAI Responses web search
  - `POST /responses (web_search tool)`
- **Capabilities**: web_research, url_fetch, people_intelligence, finance_data · priority 1
- **Needs**: query
- **Returns**: cited summary
- **Answers**: news, people, url
- **Never for**: balances, holders, transactions
- **Coverage**: the open web (failover)
- **Freshness**: live web
- **Right for**: “fallback web research when Perplexity is unavailable”

## perplexity_fetch_url

Read and summarize one public web page by URL

- **API**: Perplexity Sonar (URL)
  - `POST /chat/completions with the page URL`
- **Capabilities**: url_fetch · priority 5
- **Needs**: url
- **Returns**: summary of the page
- **Answers**: url
- **Never for**: balances, transactions
- **Coverage**: public pages
- **Freshness**: live
- **Right for**: “summarize https://...”

## perplexity_finance_search

Finance-focused web research with citations (equities, macro, crypto)

- **API**: Perplexity Sonar (finance)
  - `POST /chat/completions (finance-tuned prompt)`
- **Capabilities**: finance_data, equity_research · priority 5
- **Needs**: query
- **Returns**: cited summary
- **Answers**: news
- **Never for**: balances, boosts, holders, transactions
- **Coverage**: equities, macro, crypto news
- **Freshness**: live web
- **Right for**: “NVDA earnings reaction” · “Fed decision impact on crypto”

## perplexity_people_search

Web-grounded profile of a person with citations

- **API**: Perplexity Sonar (people)
  - `POST /chat/completions (people-tuned prompt)`
- **Capabilities**: people_intelligence · priority 5
- **Needs**: query
- **Returns**: cited summary
- **Answers**: people
- **Never for**: balances, transactions
- **Coverage**: the open web
- **Freshness**: live web
- **Right for**: “who is Anatoly Yakovenko”

## perplexity_web_search

Web-grounded research summary with citations for news and open questions

- **API**: Perplexity Sonar
  - `POST /chat/completions (sonar, web-grounded)`
- **Capabilities**: web_research, equity_research, finance_data, project_intelligence, vc_intelligence · priority 4
- **Needs**: query
- **Returns**: cited summary
- **Answers**: docs, news, projects
- **Never for**: balances, holders, transactions
- **Coverage**: the open web
- **Freshness**: live web
- **Right for**: “why did SOL drop today” · “latest on the CLARITY act”

## rootdata_people_search

People profiles: roles, projects and socials from RootData

- **API**: RootData
  - `POST /open/ser_inv (people)`
- **Capabilities**: people_intelligence · priority 12
- **Needs**: query
- **Returns**: person, role, projects, socials
- **Answers**: people
- **Never for**: price_change, volume
- **Coverage**: RootData's people database
- **Freshness**: days
- **Right for**: “who is the founder of Jupiter”

## rootdata_project_search

Project profile: category, funding history, investors and team from RootData

- **API**: RootData
  - `POST /open/ser_inv (projects)`
  - `POST /open/get_item`
- **Capabilities**: project_intelligence · priority 12
- **Needs**: query
- **Returns**: project, category, funding, investors, team, socials
- **Answers**: projects, vcs
- **Never for**: boosts, price_change, security, volume
- **Coverage**: RootData's project database
- **Freshness**: days
- **Right for**: “background on Berachain” · “who invested in EigenLayer”

## rootdata_vc_search

Venture fund profiles and portfolios from RootData

- **API**: RootData
  - `POST /open/ser_inv (VCs)`
- **Capabilities**: vc_intelligence · priority 12
- **Needs**: query
- **Returns**: fund, portfolio, recent rounds
- **Answers**: vcs
- **Never for**: price_change, volume
- **Coverage**: RootData's investor database
- **Freshness**: days
- **Right for**: “a16z crypto portfolio” · “who backs Monad”

## solana_rpc_token_top_holders

Largest 20 token accounts for a Solana mint with owner wallets and share of supply

- **API**: Solana JSON-RPC
  - `getTokenLargestAccounts`
  - `getMultipleAccounts (owner lookup)`
- **Capabilities**: token_discovery, token_security · priority 7
- **Needs**: address
- **Returns**: owner wallet, amount, share of supply
- **Answers**: holders
- **Never for**: boosts, price_change, volume
- **Coverage**: solana (keyless failover)
- **Freshness**: live
- **Right for**: “largest holders of <mint>”

## solana_token_security

Jupiter Shield safety dossier for a Solana token by mint: verification, authorities, warnings

- **API**: Jupiter tokens v2 + Shield
  - `GET /tokens/v2/search`
  - `GET /shield?mints=`
- **Capabilities**: token_security · priority 10
- **Needs**: address
- **Returns**: verified, tags, organic score, holders, authorities, concentration, warnings
- **Answers**: holders, security
- **Never for**: boosts, price_change, volume
- **Coverage**: solana
- **Freshness**: live
- **Right for**: “is <mint> safe” · “freeze authority on <mint>”

## x_kol_sentiment

Social read on one token: LunarCrush scores and sentiment with top posts

- **API**: LunarCrush + X search
  - `GET https://lunarcrush.com/api4/public/coins/<symbol>/v1`
  - `GET https://api.x.com/2/tweets/search/recent`
- **Capabilities**: market_sentiment · priority 9
- **Needs**: symbol
- **Returns**: galaxy score, alt rank, sentiment, interactions, top posts
- **Answers**: sentiment, social
- **Never for**: holders, security, volume
- **Coverage**: tokens LunarCrush tracks
- **Freshness**: hours
- **Right for**: “what are KOLs saying about SOL” · “social sentiment on PEPE”
