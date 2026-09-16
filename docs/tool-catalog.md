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
  - `POST https://streaming.bitquery.io/graphql -- EVM: EVM(dataset:combined){DEXTrades(...)} (Block.Time, Trade.Buy/Sell.Currency, Trade.Buy/Sell.Amount, Trade.Buy/Sell.Price, Trade.Dex.ProtocolName, Transaction.Hash); Solana: a separate DEXTradeByTokens query per Bitquery's docs, not the EVM DEXTrades shape`
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
  - `GET /coins/markets?vs_currency=usd&category=<chain>-ecosystem&price_change_percentage=24h&per_page=100`
- **Capabilities**: token_discovery, market_data · priority 8
- **Needs**: chain
- **Returns**: id, symbol, current_price, price_change_percentage_24h, total_volume, market_cap
- **Answers**: price_change
- **Never for**: boosts, narratives, volume
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche, solana, sui (CoinGecko ecosystem categories)
- **Freshness**: Demo tier refreshes every 60s per CoinGecko's docs; client-side ranking, $10K volume floor applied here
- **Right for**: “top gainers on solana” · “biggest losers on base today”
- **Looks similar, belongs elsewhere**: “top tokens by volume (volume ranking)” · “trending tokens on base (attention)”

## coingecko_token_by_contract

Token identity, price, volume and market cap by chain and contract address

- **API**: CoinGecko coins
  - `GET /coins/{platform}/contract/{address}`
- **Capabilities**: token_discovery, market_data · priority 2
- **Needs**: chain, address
- **Returns**: id, symbol, name, market_data.current_price, market_data.total_volume, market_data.market_cap, market_data.price_change_percentage_24h
- **Answers**: volume
- **Never for**: balances, holders, security, transactions
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche, solana
- **Freshness**: minutes
- **Right for**: “what is 0x... on base (identity + price)”

## coingecko_top_volume

Tokens ranked by 24h trading volume, market-wide or within one chain's ecosystem; a volume ranking, never paid boosts

- **API**: CoinGecko markets
  - `GET /coins/markets?vs_currency=usd&order=volume_desc&per_page=<=250&page=&price_change_percentage=24h[&category=<chain>-ecosystem]`
- **Capabilities**: token_discovery, market_data · priority 9
- **Needs**: query
- **Returns**: id, symbol, name, current_price, market_cap, market_cap_rank, total_volume, price_change_percentage_24h, high_24h, low_24h, circulating_supply, ath, last_updated
- **Answers**: volume
- **Never for**: boosts, narratives, new_listings
- **Coverage**: market-wide (order=volume_desc, up to 250/page), or one chain's CoinGecko ecosystem category (ethereum, base, arbitrum, optimism, bsc, polygon, avalanche, solana, sui)
- **Freshness**: Demo/keyless tier refreshes every 60s per CoinGecko's docs; ranking is done client-side here -- live-verified 2026-09 that order=volume_desc is silently ignored on the free/demo tier
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
  - `GET https://api.llama.fi/v2/chains  (no API key -- confirmed live 2026-09)`
- **Capabilities**: defi_data · priority 9
- **Needs**: chain
- **Returns**: name (chain), tvl, tokenSymbol
- **Answers**: tvl
- **Never for**: fees, volume, yields
- **Coverage**: all chains DefiLlama tracks
- **Freshness**: no key required; refreshed regularly, no documented interval
- **Right for**: “TVL on solana” · “which chain has the most value locked”

## defillama_fees_revenue

Fees and revenue a protocol generates (24h, 30d, all-time)

- **API**: DefiLlama fees
  - `GET https://api.llama.fi/overview/fees`
  - `GET https://api.llama.fi/summary/fees/{protocol}`
- **Capabilities**: defi_data · priority 10
- **Needs**: protocol
- **Returns**: total24h, total30d, totalAllTime, revenue24h, chains
- **Answers**: fees
- **Never for**: tvl, volume, yields
- **Coverage**: protocols with fee adapters
- **Freshness**: daily
- **Right for**: “how much does Uniswap earn in fees” · “Jupiter revenue”

## defillama_protocols

TVL for a named DeFi protocol, or a ranked protocol list

- **API**: DefiLlama
  - `GET https://api.llama.fi/protocols  (no API key -- confirmed live 2026-09: id, name, symbol, chain, chains, category, tvl, chainTvls, change_1h, change_1d, change_7d, mcap, gecko_id, audits, twitter, github, listedAt)`
  - `GET https://api.llama.fi/protocol/{slug}  (adds daily tvl history per chain)`
- **Capabilities**: defi_data · priority 10
- **Needs**: protocol
- **Returns**: name, tvl, chainTvls, category, change_1d, change_7d, mcap
- **Answers**: tvl
- **Never for**: fees, holders, volume, yields
- **Coverage**: all protocols DefiLlama tracks
- **Freshness**: hourly
- **Right for**: “Aave TVL” · “top DeFi protocols by TVL”

## defillama_yields

Best DeFi yields (APY) for an asset, chain or protocol

- **API**: DefiLlama yields
  - `GET https://yields.llama.fi/pools  (no API key)`
- **Capabilities**: defi_data · priority 10
- **Needs**: query
- **Returns**: pool, project, chain, symbol, apy, apyBase, apyReward, tvlUsd, stablecoin
- **Answers**: yields
- **Never for**: fees, tvl, volume
- **Coverage**: all pools DefiLlama tracks
- **Freshness**: hourly
- **Right for**: “best USDC yields on base” · “SOL staking rates”

## dexscreener_boosted_tokens

Tokens currently paid-boosted on DEX Screener (attention, not organic demand), with live price, volume and liquidity

- **API**: DEX Screener token boosts
  - `GET /token-boosts/top/v1  (returns: url, chainId, tokenAddress, totalAmount, icon, header, description, links, openGraph -- 60 req/min)`
  - `GET /token-boosts/latest/v1`
  - `GET /tokens/v1/{chainId}/{tokenAddresses}  (price/volume/liquidity enrichment for the boosted addresses)`
- **Capabilities**: market_data, token_discovery · priority 7
- **Needs**: query
- **Returns**: chainId, tokenAddress, totalAmount (boost spend, not a market metric), price, 24h volume, liquidity, 24h change
- **Answers**: boosts
- **Never for**: holders, price_change, security, volume
- **Coverage**: solana, base, ethereum, base, arbitrum, optimism, bsc, polygon, avalanche, robinhood, pump.fun/launchpads
- **Freshness**: live (60s cache)
- **Right for**: “trending tokens on pump.fun” · “hot tokens on solana right now” · “boosted tokens on base”
- **Looks similar, belongs elsewhere**: “tokens by 24h volume (CoinGecko ranking)” · “top gainers (price change)”

## dexscreener_latest_profiles

Newest token profiles published on DEX Screener (fresh launches with metadata)

- **API**: DEX Screener token profiles
  - `GET /token-profiles/latest/v1  (returns: url, chainId, tokenAddress, icon, header, description, links -- 60 req/min)`
- **Capabilities**: token_discovery · priority 4
- **Needs**: query
- **Returns**: chainId, tokenAddress, description, links
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
  - `GET /networks/{network}/trending_pools`
  - `GET /networks/{network}/new_pools`
  - `GET /networks/{network}/dexes/{dex}/pools`
  - `  (JSON:API; data[].attributes: name, address, base_token_price_usd, quote_token_price_usd, price_change_percentage.{m5,m15,m30,h1,h6,h24}, volume_usd.{m5,...,h24}, reserve_in_usd, fdv_usd, market_cap_usd, pool_created_at, transactions.{...} -- confirmed live 2026-09)`
- **Capabilities**: market_data, token_discovery · priority 10
- **Needs**: chain
- **Returns**: name, address, base_token_price_usd, volume_usd.h24, reserve_in_usd, price_change_percentage.h24, fdv_usd, market_cap_usd, pool_created_at
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
  - `GET /v1/{chainName}/tokens/{address}/token_holders_v2/  (same balances_v2 field shape, per holder wallet)`
- **Capabilities**: token_discovery, token_security · priority 10
- **Needs**: chain, address
- **Returns**: address (holder wallet), balance, total_supply
- **Answers**: holders
- **Never for**: boosts, price_change, volume
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche
- **Freshness**: minutes
- **Right for**: “top holders of 0x... on base”

## goldrush_wallet_balances

Current token balances and USD values for a wallet address

- **API**: GoldRush (Covalent)
  - `GET /v1/{chainName}/address/{walletAddress}/balances_v2/?quote-currency=USD&no-spam=true`
- **Capabilities**: wallet_intelligence · priority 10
- **Needs**: chain, wallet
- **Returns**: contract_address, contract_ticker_symbol, contract_decimals, balance, quote (USD value), quote_rate, type, is_native_token
- **Answers**: balances
- **Never for**: boosts, holders, security
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche, solana (balances only)
- **Freshness**: live
- **Right for**: “balances of 0x... on base” · “holdings of <wallet> on solana”

## goldrush_wallet_transactions

Recent transaction history for an EVM wallet on one chain

- **API**: GoldRush (Covalent)
  - `GET /v1/{chainName}/address/{walletAddress}/transactions_v3/`
- **Capabilities**: wallet_intelligence · priority 9
- **Needs**: chain, wallet
- **Returns**: tx_hash, block_signed_at, from_address, to_address, value, fees_paid, log_events
- **Answers**: transactions
- **Never for**: balances, security
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche
- **Freshness**: live
- **Right for**: “recent transactions of 0x... on arbitrum”

## goplus_token_security

Token security scan for an EVM contract: honeypot, blacklist, mintable, taxes, holder flags

- **API**: GoPlus
  - `GET /api/v1/token_security/{chain_id}?contract_addresses={address}  (20+ chain ids: 1 eth, 56 bsc, 137 polygon, 42161 arbitrum, 8453 base, 10 optimism, 43114 avalanche, ...)`
- **Capabilities**: token_security · priority 10
- **Needs**: chain, address
- **Returns**: is_honeypot, is_mintable, owner_address, creator_address, buy_tax, sell_tax, is_blacklisted, is_whitelisted, is_proxy, is_open_source, is_anti_whale, cannot_sell_all, transfer_pausable, slippage_modifiable, can_take_back_ownership, hidden_owner, external_call, holder_count, lp_holder_count, trust_list
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
  - `GET /v2/IsHoneypot?address={address}[&chainID=][&pair=]  (chainID omitted: picks the chain with the most liquidity for that address)`
- **Capabilities**: token_security · priority 8
- **Needs**: chain, address
- **Returns**: honeypotResult.isHoneypot, simulationResult.buyTax, simulationResult.sellTax, simulationResult.transferTax, simulationSuccess, holderAnalysis, contractCode.openSource, contractCode.isProxy
- **Answers**: security
- **Never for**: boosts, price_change, volume
- **Coverage**: ethereum, base, arbitrum, optimism, bsc, polygon, avalanche
- **Freshness**: live (buy/sell simulation, not a static scan)
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
  - `getTokenLargestAccounts(pubkey, {commitment}) -- caps at the 20 largest token accounts, per Solana's own docs`
  - `getMultipleAccounts (owner lookup)`
- **Capabilities**: token_discovery, token_security · priority 7
- **Needs**: address
- **Returns**: address (token account), amount (raw, base-10 string), decimals, uiAmountString
- **Answers**: holders
- **Never for**: boosts, price_change, volume
- **Coverage**: solana (keyless failover)
- **Freshness**: live
- **Right for**: “largest holders of <mint>”

## solana_token_security

Jupiter Shield safety dossier for a Solana token by mint: verification, authorities, warnings

- **API**: Jupiter tokens v2 + Shield
  - `GET https://lite-api.jup.ag/tokens/v2/search?query={mint}  (confirmed live 2026-09: id, name, symbol, icon, decimals, holderCount, fdv, mcap, usdPrice, liquidity, isVerified, organicScore, organicScoreLabel, tags, audit.{mintAuthorityDisabled,freezeAuthorityDisabled,topHoldersPercentage,devMints}, stats24h.{priceChange,volumeChange,buyVolume,sellVolume,numBuys,numSells,numTraders,numNetBuyers})`
  - `GET /shield?mints={mint}  (warnings array per mint)`
- **Capabilities**: token_security · priority 10
- **Needs**: address
- **Returns**: isVerified, organicScore, holderCount, audit.mintAuthorityDisabled, audit.freezeAuthorityDisabled, audit.topHoldersPercentage, warnings
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
