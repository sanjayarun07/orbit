from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    model: str = "openai/gpt-4.1-mini"
    openai_api_key: str | None = None
    typesafe_api_key: str | None = None            # Jev routing backend, harness-only until it earns a place
    # Closed beta: no payments, every signed-in account on the free Beta plan.
    # Billing reports itself unconfigured whatever Stripe settings exist, the
    # UI shows no prices or purchase controls, and the monthly allowance is
    # the beta one. Off by default so nothing changes for existing setups.
    closed_beta: bool = False
    closed_beta_monthly_credits: int = 5000
    # What answers the routing classification seam: speech_model (default),
    # jev, or jev_fallthrough (Jev when confident, else the speech model).
    # See app/routing/backends.py. A production instance on a jev backend
    # without TYPESAFE_API_KEY refuses to boot.
    routing_backend: str = "speech_model"
    # LLM-hop resilience (app/nodes/runtime.py `_call_lm`). Without an explicit
    # timeout litellm waits up to 600s on a wedged provider call; retries below
    # are litellm's own exponential-backoff attempts against the SAME model, and
    # the fallback model is tried once when the primary exhausts them with a
    # transient/provider error (set it to a DIFFERENT provider you have keys for
    # -- e.g. "anthropic/claude-haiku-4-5" -- so a full OpenAI outage still
    # answers; leave empty to disable cross-provider failover).
    llm_request_timeout_seconds: float = 60.0
    llm_num_retries: int = 2
    llm_fallback_model: str | None = None
    intent_embedding_enabled: bool = True
    intent_embedding_model: str = "text-embedding-3-small"
    intent_embedding_threshold: float = 0.78
    intent_embedding_margin: float = 0.08
    intent_embedding_timeout_seconds: float = 8.0
    intent_embedding_cache_entries: int = 512
    intent_model_confidence_threshold: float = 0.90
    # The speech model decides every keyword-topical request (app/routing/
    # resolver.py), so its latency is on the hot path: point it at a faster
    # tier than the answer model (e.g. "openai/gpt-4.1-nano"); empty = `model`.
    intent_model: str | None = None
    # The synthesis tier: the programs that WRITE an answer from evidence the
    # tools already gathered (equity brief, knowledge-base answer, token deep
    # dive). Unset, they run on `model`. Set to try a domain model on exactly
    # that job -- e.g. "hosted_vllm/DMind-3-mini" -- without touching routing
    # or the tool loops; a transient failure falls back to `model`.
    synthesis_model: str | None = None
    intent_classifier_cache_entries: int = 512
    # MCP server (app/mcp_server.py) mounted at /mcp for Claude / ChatGPT / any
    # MCP host. When MCP_API_KEY is set, /mcp requires `Authorization: Bearer`.
    # PUBLIC_BASE_URL builds the hand-off links an MCP host shows the user to
    # connect a wallet and confirm a quote in the web UI.
    mcp_api_key: str | None = None
    public_base_url: str = "http://localhost:8000"
    # Telegram bot (app/telegram/). Off until TELEGRAM_BOT_TOKEN is set: no
    # route is registered, no webhook is claimed, nothing changes.
    #
    # A Telegram user is a way in to an ordinary Orbit account, created on
    # their first message and keyed on their numeric Telegram id, so plans,
    # credits, quotas, conversation retention and deletion are the same code
    # paths the web uses. "Anonymous" here means no sign-in screen, not no
    # account -- a signed-out browser visitor is scratch space that expires in
    # two hours, and a Telegram chat is permanent and personal.
    telegram_bot_token: str | None = None
    # The path segment and the header Telegram echoes back, both checked. Left
    # empty, one is derived from the bot token so a deployment that forgets to
    # set it is not thereby open: the URL is still unguessable.
    telegram_webhook_secret: str | None = None
    # PUBLIC_BASE_URL + this path is what setWebhook is pointed at on startup.
    # Empty disables the automatic claim (set the webhook by hand, or run two
    # deployments against one bot without them fighting over it).
    telegram_set_webhook_on_start: bool = True
    # How long the bot waits for a turn before telling the user it is still
    # working. Telegram shows "typing..." for 5 s per call, so the progress
    # message is what actually reassures them.
    telegram_turn_timeout_seconds: float = 180.0
    # One edit per this many seconds while a turn runs: Telegram rate-limits
    # edits per chat, and a deep dive emits far more status lines than that.
    telegram_progress_interval_seconds: float = 1.5
    # A web -> Telegram link token from Profile, consumed by /start.
    telegram_link_ttl_seconds: int = 15 * 60
    # TradingView connector (app/integrations/tradingview.py): each user links
    # their own TradingView account (OAuth 2.1, Essential plan or higher) so
    # research can read live quotes, technicals, fundamentals, news and
    # calendars from TradingView's MCP server on their behalf. Orbit registers
    # itself as an OAuth client at first use; the redirect is
    # PUBLIC_BASE_URL + /integrations/tradingview/callback.
    tradingview_enabled: bool = True
    # Related questions under an answer (app/followups.py): model-written,
    # kept only when grounded in the answer; off means the section never shows.
    followups_enabled: bool = True
    # Cross-conversation memory (app/user_memory.py): durable facts about a
    # signed-in user, extracted after a turn and recalled before the next.
    user_memory_enabled: bool = True
    # Every research answer is checked against the question before it is
    # returned (app/answer_gate.py); a wrong-subject or empty answer is
    # replaced by the web's answer or a question. Costs a model call per turn.
    answer_gate_enabled: bool = True
    # The trading desk (team mode) on its own for advice-shaped asks about an
    # asset ("should I buy X", "thoughts on Y"); factual lookups, security
    # checks and plain swaps stay on the single path. The session's team_mode
    # (chat command / MCP) still forces the desk for everything.
    team_desk_auto: bool = True
    # Fetch slow reference data (DefiLlama's emissions index) at startup.
    warm_caches_on_start: bool = True
    tradingview_mcp_url: str = "https://mcp.tradingview.com/mcp"
    # x402 pay-per-message on POST /chat (app/x402_gate.py). Off by default;
    # when on, an unpaid request gets HTTP 402 with the payment requirements
    # and the browser pays in USDC from the connected EVM wallet. The default
    # network and facilitator are Base Sepolia testnet; for Base mainnet set
    # eip155:8453 and a facilitator that supports it.
    x402_enabled: bool = False
    x402_pay_to: str | None = None
    x402_network: str = "eip155:84532"
    x402_price: str = "$0.01"
    x402_description: str = "One Orbit chat turn"
    x402_max_timeout_seconds: int = 300
    x402_facilitator_url: str = "https://x402.org/facilitator"
    x402_sync_facilitator_on_start: bool = True
    # Outcome-scored tools (app/tool_outcomes.py): a smoothed grounded-success
    # rate per tool, learned from real turns, adds weight*(rate - prior) to
    # ProviderRouter._score. An unobserved tool sits at the prior (0 adjustment).
    provider_outcome_weight: float = 3.0
    tool_outcome_prior_rate: float = 0.8
    tool_outcome_prior_weight: float = 5.0
    tool_outcome_window_days: int = 14
    tool_outcome_refresh_seconds: int = 300
    # One thumbs up/down on an answer counts as this many automatic outcomes
    # for every tool that contributed to it (app/feedback.py).
    tool_feedback_weight: float = 2.0
    # Welcome-screen highlights (app/home_highlights.py): one news/market build per window.
    home_highlights_ttl_seconds: int = 1800
    # Per-user tasks (app/tasks.py): worker cadence, alert re-check interval, brief cost.
    task_worker_interval_seconds: int = 30
    task_alert_check_minutes: int = 5
    credit_cost_brief: int = 1
    # X / KOL sentiment (app/social_sentiment.py): X API v2 when a bearer token is
    # set, Perplexity web search over X posts otherwise. Event calendar cache.
    x_bearer_token: str | None = None
    # LunarCrush API v4 (https://lunarcrush.com/developers/api): measured social
    # metrics (galaxy score, alt rank, sentiment, interactions, top creators/posts).
    # Preferred over X API and Perplexity for the sentiment tool when set.
    lunarcrush_api_key: str | None = None
    # TwitterAPI.io (user decision 2026-09-21): the tweet source for the X
    # sentiment analyst (app/x_tweets.py, app/sentiment_analyst.py). Tweets
    # are cached by id; a re-ask pays only for the ones posted since.
    twitterapi_io_key: str | None = None
    twitterapi_io_base_url: str = "https://api.twitterapi.io"
    # Live 2026-09-21: with min_faves:2 a page holds about ten tweets and one
    # page took over twenty seconds, so a 100-tweet sample is minutes. Forty
    # tweets in two to four pages keeps a chat turn short; each page has its
    # own timeout.
    x_tweets_default_sample: int = 40
    x_tweets_cache_hours: float = 24.0
    twitterapi_io_timeout_seconds: float = 12.0
    # Reddit's official API (free script app at reddit.com/prefs/apps): the
    # keyless JSON and RSS paths answered HTML block pages live (2026-09-22).
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "Orbit/1.0 (crypto research; by u/orbit-copilot)"
    reddit_subreddits: str = "CryptoCurrency,solana,memecoins,SatoshiStreetBets,CryptoMoonShots,altcoin,ethtrader,Bitcoin,CryptoMarkets"
    # Polymarket's Gamma API (free, no key). The developer network's resolver
    # returned no address for it (2026-09-22); the host can be pinned by IP
    # when a deployment's DNS blocks it.
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_enabled: bool = True
    # --- Knowledge service (app/knowledge) ---
    knowledge_embedding_provider: str = "openai"      # openai | hashing
    knowledge_embedding_model: str = "text-embedding-3-small"
    knowledge_embedding_dim: int = 1024               # matches the schema; Qwen-1024 compatible
    knowledge_registry_limit: int = 50                # protocols to bootstrap / keep fresh
    knowledge_docs_page_budget: int = 40              # pages per protocol docs site
    knowledge_ingest_enabled: bool = False            # background worker; run manually via admin first
    knowledge_ingest_interval_seconds: int = 300
    knowledge_ingest_batch: int = 5
    knowledge_reranker: str = "heuristic"             # heuristic | cross-encoder | llm
    knowledge_reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    github_token: str | None = None
    social_sentiment_ttl_seconds: int = 900
    event_calendar_ttl_seconds: int = 21600
    # Per-capability tool-selection semantic fallback (app/routing/tool_semantic.py) --
    # a second chance for a description-bearing tool whose regex `matches`
    # gate returned False, not a replacement for the intent-classification
    # embedding settings above. threshold=0.45 is calibrated against REAL
    # request text, not a guess -- live-tested (2026-09-13,
    # text-embedding-3-small) twice: an initial pass using clean natural-
    # language phrasing alone scored correct matches at 0.62-0.69, but a
    # second pass using actual production-shaped text (a real hex contract/
    # mint address embedded inline, as every real request has) scored
    # correct matches meaningfully lower (0.46-0.56) -- raw addresses dilute
    # the semantic signal an embedding model picks up. 0.45 is calibrated
    # against that second, realistic pass, sitting just above the nearest
    # wrong-sibling scores observed (0.42, 0.44) in the same gated
    # capability. Biased toward a missed fallback (safe: falls through to
    # existing behavior) over a false positive (routes to the wrong
    # specialized tool with wrong data). Re-run the calibration script (see
    # the plan) with real address-bearing request text -- not clean
    # phrasing alone -- before trusting this number again if the pilot
    # descriptions change.
    tool_embedding_enabled: bool = True
    tool_embedding_model: str = "text-embedding-3-small"
    tool_embedding_threshold: float = 0.45
    tool_embedding_timeout_seconds: float = 8.0
    tool_embedding_cache_entries: int = 512
    tool_embedding_cost_usd: float = 0.0
    # Model tool arbitration (app/routing/tool_selector.py): OFF by default
    # (flag-gated pilot). When several tools already pass the regex gate and
    # the chain/health/quota filters, this reads their app.tool_catalog specs
    # (what the API returns, what it can never answer) and picks among THEM --
    # it never introduces a tool that didn't already independently qualify, so
    # a bad pick only costs one extra attempt before the router falls through
    # to the next candidate exactly as it does today. Point tool_selector_model
    # at a hosted small/cheap model (falls back to intent_model, then model)
    # -- e.g. a Qwen served via OpenRouter/DashScope/Together/Fireworks through
    # LiteLLM -- to pilot it without touching the answer model.
    llm_tool_selection_enabled: bool = False
    tool_selector_model: str | None = None
    # A self-hosted / OpenAI-compatible endpoint for tool_selector_model, e.g. a
    # vLLM-served Qwen instance on your own infrastructure -- LiteLLM (which
    # dspy.LM wraps) reaches it via these two, not the OpenAI/Anthropic default
    # base URLs. Leave both empty to use a normal hosted provider (OpenAI, etc)
    # exactly as the other model tiers do. For vLLM specifically, prefix the
    # model name with "hosted_vllm/" (e.g. tool_selector_model=
    # "hosted_vllm/Qwen3-VL-32B-Instruct-FP8") -- LiteLLM's dedicated vLLM
    # provider, not "openai/", so generation kwargs and tool-calling pass
    # through correctly; tool_selector_api_base then points at that server's
    # OpenAI-compatible root (usually ending in /v1).
    tool_selector_api_base: str | None = None
    tool_selector_api_key: str | None = None
    tool_selector_timeout_seconds: float = 6.0
    tool_selector_min_candidates: int = 2       # fewer than this: nothing to arbitrate, skip the call
    tool_selector_max_candidates: int = 6        # shown to the model; the rest keep their deterministic order behind them
    tool_selector_cache_entries: int = 512
    tool_selector_cost_usd: float = 0.0
    perplexity_api_key: str | None = None
    perplexity_agent_url: str = "https://api.perplexity.ai/v1/agent"
    perplexity_model: str = "perplexity/sonar"
    perplexity_timeout_seconds: float = 45.0
    perplexity_cache_ttl_seconds: int = 60
    perplexity_cache_max_entries: int = 512
    perplexity_web_search_cost_usd: float = 0.005
    perplexity_fetch_url_cost_usd: float = 0.0005
    perplexity_people_search_cost_usd: float = 0.005
    perplexity_finance_search_cost_usd: float = 0.005
    perplexity_requests_per_minute: int = 60
    openai_web_search_cost_usd: float = 0.01
    openai_web_search_model: str = "gpt-4.1-mini"
    openai_web_search_requests_per_minute: int = 60
    provider_cost_weight: float = 100.0
    provider_health_weight: float = 4.0
    provider_semantic_cache_threshold: float = 0.92
    provider_cache_max_entries: int = 1024
    provider_request_timeout_seconds: float = 45.0
    provider_circuit_failure_threshold: int = 3
    provider_circuit_cooldown_seconds: int = 60
    birdeye_api_key: str | None = None
    birdeye_base_url: str = "https://public-api.birdeye.so"
    birdeye_requests_per_minute: int = 60
    # Left at 0.0: Birdeye's production API is a paid add-on with no public
    # per-request price (their pricing page only advertises a flat add-on
    # fee and points to sales for API terms) -- an invented number would be
    # less honest than leaving this unbound. Set it from your actual
    # contract if you have one.
    birdeye_request_cost_usd: float = 0.0
    mobula_api_key: str | None = None
    mobula_base_url: str = "https://api.mobula.io/api/2"   # every Mobula call derives its host from this (app/mobula_client.py)
    mobula_requests_per_minute: int = 60
    # The Mobula bucket is per process. With several uvicorn workers each one
    # takes its share of the minute, so the container as a whole never
    # exceeds the allowance (review, 2026-09-22: two workers doubled it).
    uvicorn_workers: int = 1
    # Across replicas the bucket cannot see its siblings, so when Redis is
    # configured every Mobula call also counts against one shared per-minute
    # counter (the provider's real limit); Redis down means the local bucket
    # alone, never a blocked turn (review, 2026-09-22).
    mobula_shared_limit: bool = True
    # The bucket never holds more than this many tokens: the per-minute rate
    # is a sustained rate, not a burst (a 60-request burst drew 429s live).
    # The full minute fits one deep-dive (about forty calls: security, trades,
    # first buyers, and a bundle check's twenty-six) in a single burst; at
    # thirty the bundle check was refused by our own budget (live, 2026-09-21).
    # Background work only draws while the bucket is at least half full, so
    # the ledger never takes more than thirty before waiting for refill, and
    # a 429 from Mobula still pauses everyone.
    mobula_burst: int = 60
    # After a 429 or 5xx from Mobula, every caller pauses this long (or the
    # Retry-After header, when sent); background callers are refused outright.
    mobula_cooldown_seconds: float = 20.0
    # How long the wallet-portfolio intercept waits for Mobula before the
    # per-chain balances answer instead.
    mobula_portfolio_timeout_seconds: float = 20.0
    # Left at 0.0: Mobula bills in credits (1 per chain for most reads, 10
    # for DeFi positions) with no published credit-to-dollar rate for paid
    # tiers (only the 10k-credit/month free tier is public) -- same
    # reasoning as Birdeye above.
    mobula_request_cost_usd: float = 0.0
    # The holder snapshot ledger (app/holder_snapshots.py): periodic rows of
    # a tracked meme token's structure, recorded in the background so history
    # exists later. Three Mobula calls per row through the budgeted door.
    holder_snapshots_enabled: bool = True
    holder_snapshot_tick_seconds: int = 60
    holder_snapshot_fresh_minutes: int = 10        # while a token is under a day old
    holder_snapshot_interval_minutes: int = 60     # afterwards
    holder_snapshot_max_per_tick: int = 5           # three calls each: a quarter of the minute rate
    holder_snapshot_pulse_chains: str = "solana,base,bsc"
    holder_snapshot_min_holders: int = 10          # a launch with fewer holders is not tracked yet
    # Durable jobs (app/jobs.py, docs/durable-jobs-spec.md): work that outlives
    # a request. A chat turn stays attached to its job this long; past it the
    # answer is appended to the conversation when the job settles.
    jobs_enabled: bool = True
    job_attach_seconds: float = 90.0
    job_lease_seconds: float = 60.0
    job_max_attempts: int = 3
    job_volatile_max_age_seconds: float = 60.0     # cached price-class results older than this are refetched on resume
    # The exit monitor (app/exit_monitor.py): a position's exact-size exit
    # quotes recorded on a schedule; an alert when the full-exit quote falls.
    exit_monitor_interval_minutes: int = 15
    exit_alert_drop_pct: float = 20.0                # quoted full-exit proceeds down this much vs entry or the last alert
    exit_alert_cooldown_hours: float = 6.0
    exit_positions_per_user: int = 10
    # Tequity (app/tequity.py): the company's internal equities/perps websocket.
    # Movers on Aster and Hyperliquid, a cross-venue trending list, news and
    # stats. Not a secret; wss only (ws:// is rejected upstream).
    tequity_enabled: bool = True
    tequity_ws_url: str | None = "wss://tequity-dn.i5.xyz/ws"
    # The tick ledger (app/tequity_ledger.py): one row per pair per tick,
    # recorded by the lease holder every interval; rows older than the
    # retention are pruned daily. 0 disables recording.
    tequity_record_interval_seconds: int = 300
    tequity_retention_days: int = 30
    bitquery_api_key: str | None = None
    bitquery_graphql_url: str = "https://streaming.bitquery.io/eap"
    bitquery_requests_per_minute: int = 30
    # Estimated from Bitquery's published points pricing: dataset:realtime
    # costs 5 points/cube (docs.bitquery.io/docs/ide/points), and the
    # Personal plan is $49/mo for 100k points -> ~$0.00049/point -> ~$0.00245
    # per call. A real, sourced estimate for budget-triggering purposes, not
    # a reconciled bill -- recalibrate against your actual plan/invoices.
    bitquery_request_cost_usd: float = 0.00245
    coingecko_api_key: str | None = None
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    coingecko_requests_per_minute: int = 30
    coinmarketcap_api_key: str | None = None
    coinmarketcap_base_url: str = "https://pro-api.coinmarketcap.com"
    coinmarketcap_requests_per_minute: int = 30
    goplus_access_token: str | None = None
    goplus_base_url: str = "https://api.gopluslabs.io/api/v1"
    goplus_requests_per_minute: int = 30
    honeypot_base_url: str = "https://api.honeypot.is/v2"
    honeypot_requests_per_minute: int = 30
    defillama_base_url: str = "https://api.llama.fi"
    defillama_yields_base_url: str = "https://yields.llama.fi"
    defillama_requests_per_minute: int = 60
    goldrush_api_key: str | None = None
    goldrush_base_url: str = "https://api.covalenthq.com/v1"
    goldrush_requests_per_minute: int = 30
    helius_api_key: str | None = None
    helius_requests_per_minute: int = 60
    lifi_api_key: str | None = None
    lifi_base_url: str = "https://li.quest/v1"
    lifi_enabled: bool = True
    reown_project_id: str | None = None
    # RPCs used only to verify EVM login signatures. Smart wallets return
    # ERC-1271/ERC-6492 signatures that must be checked on their active chain.
    evm_auth_rpc_urls: dict[int, str] = {
        1: "https://ethereum-rpc.publicnode.com",
        10: "https://mainnet.optimism.io",
        56: "https://bsc-dataseed.binance.org",
        137: "https://polygon-bor-rpc.publicnode.com",
        8453: "https://mainnet.base.org",
        42161: "https://arb1.arbitrum.io/rpc",
        43114: "https://api.avax.network/ext/bc/C/rpc",
    }
    clickhouse_host: str | None = None
    clickhouse_port: int = 8443
    clickhouse_username: str = "default"
    clickhouse_password: str | None = None
    clickhouse_database: str = "default"
    clickhouse_secure: bool = True
    dune_api_key: str | None = None
    dune_base_url: str = "https://api.dune.com/api/v1"
    dune_requests_per_minute: int = 10
    dune_query_performance: str = "small"
    dune_poll_interval_seconds: float = 2.0
    dune_poll_timeout_seconds: float = 120.0
    # Round-trip detection thresholds for the wash-trading detector -- kept
    # as settings (not hardcoded) so they can be tuned against known cases
    # without a redeploy-and-edit-code cycle.
    wash_trading_round_trip_max_gap_seconds: float = 30.0
    wash_trading_round_trip_size_tolerance_pct: float = 0.05
    rootdata_api_key: str | None = None
    rootdata_base_url: str = "https://api.rootdata.com/open"
    rootdata_requests_per_minute: int = 60
    rootdata_map_cache_ttl_seconds: int = 43_200
    rootdata_request_cost_usd: float = 0.0
    admin_api_key: str | None = None
    provider_overrides_path: str = "provider-overrides.json"
    # Public browser identifiers for optional Privy embedded-wallet onboarding.
    # Never place PRIVY_APP_SECRET or an authorization-key private key here.
    privy_app_id: str | None = None
    privy_client_id: str | None = None
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    jupiter_base_url: str = "https://api.jup.ag/swap/v1"
    jupiter_api_root: str = "https://api.jup.ag"
    jupiter_api_key: str | None = None
    # Server-side Relay credential. Never return this from /config/public or
    # embed it in the browser bundle; production usage should go via a proxy.
    relay_api_key: str | None = None
    solana_private_key: str | None = None
    # Readiness probe (/readyz): per-check timeout, so a wedged datastore
    # fails the probe fast instead of hanging the load balancer's check.
    readiness_check_timeout_seconds: float = 3.0
    # --- Deployment mode (app/deployment.py) ---------------------------------
    # ENVIRONMENT drives the startup configuration audit: "production" turns
    # development conveniences (exposed magic links, memory fallback, an
    # unauthenticated /mcp) into refusals to start.
    environment: str = "development"
    # "research" refuses every money-moving entry point across all providers
    # (Jupiter, Relay, LI.FI); "execution" allows them under the trade policy
    # below. Unset infers the mode from live_trading, so existing deployments
    # keep their behaviour; declaring both contradictorily fails at startup.
    deployment_mode: str | None = None
    # May the SERVER hold a key and sign for the user? Separate from the mode
    # on purpose: a wallet-only product should never sign on anyone's behalf.
    allow_custodial_signing: bool = False
    # WHO may use the server-held key, as a comma-separated list of user ids.
    # Enabling custodial signing says the server may sign; this says for whom.
    # A plan is bound to the account that asked for it, and execution checks
    # that the plan's wallet is the server's -- neither proves that this
    # account is entitled to spend from that wallet. Without this list, any
    # signed-in account could address a plan to the server signer and confirm
    # it. Empty means nobody, which the startup audit treats as an error when
    # custodial signing is on.
    custodial_signing_principals: str = ""
    live_trading: bool = False
    max_trade_usd: float = 25.0
    max_slippage_bps: int = 100
    max_price_impact_pct: float = 5.0
    plan_ttl_seconds: int = 120
    # Portfolio snapshots are bounded so a wallet with thousands of token
    # accounts cannot exhaust a chat turn: at most this many holdings are
    # priced (largest balances first), within this many seconds; the rest is
    # returned unpriced with partial=true. The risk node's own snapshot fetch
    # has a shorter guard so a quote is never blocked on pricing.
    portfolio_max_priced_holdings: int = 1200   # Jupiter prices 100 mints a call; a 1,500-token wallet is 15 calls, and its largest positions by VALUE are rarely its largest by count
    portfolio_snapshot_timeout_seconds: float = 25.0
    risk_snapshot_timeout_seconds: float = 20.0
    memory_plan_max_entries: int = 1000
    # Chat history retention. A signed-out visitor's conversation is scratch
    # space that expires quickly; a signed-in account's conversations are the
    # "history" the product promises, so every turn they own pushes the
    # expiry out again.
    chat_history_ttl_seconds: int = 2 * 60 * 60
    chat_history_signed_in_ttl_seconds: int = 30 * 24 * 60 * 60
    memory_session_max_entries: int = 1000

    # Persistent memory/state. Both are optional: if unset or unreachable,
    # plans.py and sessions.py fall back to in-process memory automatically.
    database_url: str | None = "postgresql://localhost/solana_dspy_poc"
    redis_url: str | None = "redis://localhost:6379/0"
    postgres_pool_min_size: int = 1
    postgres_pool_max_size: int = 10

    # Tracing. No-op unless both keys are set.
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # Optional external MCP servers (mcpServers-schema JSON file); unset disables.
    mcp_config_path: str | None = None
    nansen_api_key: str | None = None

    # Capacity and token controls. Defaults are deliberately conservative so a
    # single instance remains responsive under load without overwhelming model,
    # RPC, or MCP providers.
    # --- Public cost admission (R06) ---------------------------------------
    # The trial account is keyed on a browser-supplied device id so that two
    # people behind one NAT do not share credits. That id is caller-controlled,
    # so on its own it is a resettable boundary: rotate it and the trial
    # renews. These are the server-controlled budgets layered on top.
    checkout_pending_seconds: int = 1800            # a started subscription checkout blocks another for this long
    task_retry_limit: int = 3                       # attempts at one occurrence before it is refunded and dropped
    trial_accounts_per_ip_per_day: int = 3          # new trial grants one IP may mint in 24h
    chat_requests_per_minute_per_ip: int = 120      # ceiling per IP regardless of device ids
    knowledge_search_max_query_chars: int = 400
    knowledge_search_daily_budget: int = 5000       # embedder/reranker calls from the public route, per day, whole deployment
    max_concurrent_knowledge_searches: int = 8
    max_concurrent_chat_requests: int = 32
    max_concurrent_llm_requests: int = 16
    chat_requests_per_minute: int = 60
    auth_requests_per_minute: int = 30
    chat_execution_timeout_seconds: float = 120.0
    # /chat/stream writes an SSE comment when nothing else has been sent for
    # this long, so proxies and phones see a live connection during a slow tool.
    stream_keepalive_seconds: float = 15.0
    request_queue_timeout_seconds: float = 10.0
    rpc_requests_per_minute: int = 120
    rpc_max_batch_size: int = 10
    rpc_max_body_bytes: int = 65536
    trusted_proxy_hosts: str = ""
    max_context_messages: int = 8
    max_context_chars: int = 6000
    max_mcp_tools_per_request: int = 5
    max_concurrent_mcp_calls: int = 8
    mcp_connections_per_server: int = 2
    # Testing showed 45s was mostly providing headroom to already-failing
    # calls, not ones that need that long to succeed (successful Nansen calls
    # were consistently single-digit seconds). 20s still gives a legitimately
    # slower analytics query room while bounding worst-case chat latency.
    mcp_call_timeout_seconds: float = 20.0
    mcp_result_max_chars: int = 12000
    mcp_cache_ttl_seconds: int = 60
    mcp_cache_max_entries: int = 512
    # Per-tool overrides for slow-changing data (raw MCP tool names, not the
    # wrapped mcp_<server>_<tool> name) -- entity labels and portfolio
    # composition don't change second-to-second the way prices do, so one
    # global TTL either wastes cache hits on slow data or risks staleness
    # on fast data. Anything not listed keeps mcp_cache_ttl_seconds.
    mcp_tool_cache_ttl_seconds: dict[str, int] = {
        "address_labels": 3600,
        "address_portfolio": 300,
        "address_transactions": 120,
        "token_current_top_holders": 300,
        "token_quant_scores": 300,
        "token_info": 300,
    }
    mcp_env_allowlist: str = "NANSEN_API_KEY"
    expose_tool_trajectory: bool = False
    # Step-7 answer validator (app/answer_validator.py): data older than this (per
    # the provider's own timestamp) is flagged as a freshness warning. Advisory only.
    answer_freshness_warn_minutes: float = 30.0
    # Enrich the Solana token security/identity dossier with a short web-sourced
    # "latest context" section (issuer confirmation, recent incidents/depeg news)
    # when Perplexity is configured. Adds one cached, budget-gated web call.
    token_security_web_context: bool = True
    # Role-memory reflection loop (app/role_memory.py): where deep-dive decisions +
    # their reflected lessons persist. None -> in-process only (resets on restart).
    role_memory_path: str | None = "data/role_memory.json"
    # Cross-source consistency: a headline metric reported by two providers for the
    # SAME token that differs by more than this percent is flagged. Advisory only.
    answer_consistency_tolerance_pct: float = 25.0
    # Per-chat-turn cap on external provider/MCP calls and their tracked
    # cost -- see app/call_budget.py. The cost ceiling is real, wired
    # infrastructure but every *_request_cost_usd setting defaults to 0.0,
    # so it isn't meaningfully binding until real per-call costs are
    # populated; the call-count cap is what actually does work today.
    max_external_calls_per_turn: int = 12
    max_paid_data_cost_usd_per_turn: float = 0.50

    # In production, set false so missing shared infrastructure fails visibly
    # rather than creating isolated per-worker sessions and trade plans.
    allow_memory_fallback: bool = True

    # --- Accounts, credits and billing (app/accounts.py, app/credits.py) ---
    product_name: str = "Orbit"
    # Resend transactional email (magic links, receipts). Unset -> no email is
    # sent; with dev_expose_magic_links the sign-in link is returned to the UI
    # instead, which is how local development signs in.
    resend_api_key: str | None = None
    email_from: str = "Orbit <no-reply@example.com>"
    magic_link_ttl_minutes: int = 15
    dev_expose_magic_links: bool = True
    # Credits per finished chat turn, by what the turn did (see credits.turn_cost).
    credit_cost_chat_turn: int = 1
    credit_cost_tool_turn: int = 2
    credit_cost_deep_dive: int = 5
    credit_cost_trade_turn: int = 3
    credit_cost_team_turn: int = 5
    # Stripe (Phase 2). Test-mode keys only in development.
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_price_pro: str | None = None
    stripe_price_max: str | None = None
    stripe_price_pack_500: str | None = None
    stripe_price_pack_2000: str | None = None
    stripe_price_pack_10000: str | None = None
    # Stripe's "crypto" payment method (USDC) needs to be enabled on the Stripe
    # account for the entity; off until it is, so Checkout never errors.
    stripe_crypto_enabled: bool = False
    stripe_tax_enabled: bool = False

    def __repr_args__(self):
        """Never print a credential, even inside a stack trace.

        Settings is a field on nothing, but it is reachable from plenty of
        assertion failures and exception reprs -- a failing test that compares
        against `settings.something` renders the whole object, and that output
        goes to CI logs. Masking here covers every such path at once, including
        ones nobody thought about, which is the only way this stays true as
        fields are added.
        """
        for name, value in super().__repr_args__():
            yield name, _mask_secret(str(name), value)


_SECRET_NAME_HINTS = ("key", "secret", "token", "password", "private", "credential")


def _mask_secret(name: str, value):
    if not isinstance(value, str) or not value:
        return value
    lowered = name.lower()
    if any(hint in lowered for hint in _SECRET_NAME_HINTS):
        return f"<{name} set, {len(value)} chars>"
    # A connection string carries its password inline: postgres://user:pw@host.
    if "://" in value and "@" in value.split("://", 1)[1]:
        return f"<{name} set, credentials in URL>"
    return value


settings = Settings()
