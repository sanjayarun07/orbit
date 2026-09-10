from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    model: str = "openai/gpt-4.1-mini"
    openai_api_key: str | None = None
    intent_embedding_enabled: bool = True
    intent_embedding_model: str = "text-embedding-3-small"
    intent_embedding_threshold: float = 0.78
    intent_embedding_margin: float = 0.08
    intent_embedding_timeout_seconds: float = 8.0
    intent_embedding_cache_entries: int = 512
    intent_model_confidence_threshold: float = 0.90
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
    birdeye_request_cost_usd: float = 0.0
    mobula_api_key: str | None = None
    mobula_base_url: str = "https://api.mobula.io/api/2"
    mobula_requests_per_minute: int = 60
    mobula_request_cost_usd: float = 0.0
    bitquery_api_key: str | None = None
    bitquery_graphql_url: str = "https://streaming.bitquery.io/eap"
    bitquery_requests_per_minute: int = 30
    bitquery_request_cost_usd: float = 0.0
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
    live_trading: bool = False
    max_trade_usd: float = 25.0
    max_slippage_bps: int = 100
    max_price_impact_pct: float = 5.0
    plan_ttl_seconds: int = 120
    memory_plan_max_entries: int = 1000
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
    mcp_call_timeout_seconds: float = 45.0
    mcp_result_max_chars: int = 12000
    mcp_cache_ttl_seconds: int = 60
    mcp_cache_max_entries: int = 512
    mcp_env_allowlist: str = "NANSEN_API_KEY"
    expose_tool_trajectory: bool = False

    # In production, set false so missing shared infrastructure fails visibly
    # rather than creating isolated per-worker sessions and trade plans.
    allow_memory_fallback: bool = True


settings = Settings()
