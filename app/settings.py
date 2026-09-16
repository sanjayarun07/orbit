from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    model: str = "openai/gpt-4.1-mini"
    openai_api_key: str | None = None
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
    intent_classifier_cache_entries: int = 512
    # MCP server (app/mcp_server.py) mounted at /mcp for Claude / ChatGPT / any
    # MCP host. When MCP_API_KEY is set, /mcp requires `Authorization: Bearer`.
    # PUBLIC_BASE_URL builds the hand-off links an MCP host shows the user to
    # connect a wallet and confirm a quote in the web UI.
    mcp_api_key: str | None = None
    public_base_url: str = "http://localhost:8000"
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
    mobula_base_url: str = "https://api.mobula.io/api/2"
    mobula_requests_per_minute: int = 60
    # Left at 0.0: Mobula bills in credits (1 per chain for most reads, 10
    # for DeFi positions) with no published credit-to-dollar rate for paid
    # tiers (only the 10k-credit/month free tier is public) -- same
    # reasoning as Birdeye above.
    mobula_request_cost_usd: float = 0.0
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
    portfolio_max_priced_holdings: int = 300
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
    max_concurrent_chat_requests: int = 32
    max_concurrent_llm_requests: int = 16
    chat_requests_per_minute: int = 60
    auth_requests_per_minute: int = 30
    chat_execution_timeout_seconds: float = 120.0
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


settings = Settings()
