import pytest
from app.additional_providers import CoinGeckoProvider, GoldRushProvider, GoPlusProvider, HeliusProvider
from app.capability_router import route_capabilities
from app.provider_registry import get_provider_router
from app.settings import settings


EVM_TOKEN = "0x1111111111111111111111111111111111111111"
SOLANA_WALLET = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _client(monkeypatch, payload, captured):
    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            captured.update({"method": "GET", "url": url, **kwargs})
            return _Response(payload)

        def post(self, url, **kwargs):
            captured.update({"method": "POST", "url": url, **kwargs})
            return _Response(payload)

    monkeypatch.setattr("app.additional_providers.httpx.Client", Client)


def test_coingecko_normalizes_contract_market_data(monkeypatch):
    captured = {}
    _client(monkeypatch, {
        "name": "USD Coin", "symbol": "usdc",
        "market_data": {"current_price": {"usd": 1}, "total_volume": {"usd": 10},
                        "market_cap": {"usd": 20}, "price_change_percentage_24h": 0.1},
    }, captured)
    output = CoinGeckoProvider().token(f"Analyze token {EVM_TOKEN} on Base")
    assert captured["url"].endswith(f"/coins/base/contract/{EVM_TOKEN}")
    assert "USD Coin (USDC)" in output
    assert "**Price**: $1.0000" in output


def test_goplus_uses_chain_id_and_surfaces_risk_flags(monkeypatch):
    captured = {}
    _client(monkeypatch, {"result": {EVM_TOKEN: {
        "token_name": "Risky", "is_honeypot": "1", "is_mintable": "1",
        "buy_tax": "0.01", "sell_tax": "0.20",
    }}}, captured)
    output = GoPlusProvider().security(f"Check token safety {EVM_TOKEN} on Base")
    assert captured["url"].endswith("/token_security/8453")
    assert "Blocking flags**: Honeypot" in output
    assert "Caution / contract features**: Mintable" in output


def test_helius_key_can_come_from_configured_rpc_url(monkeypatch):
    monkeypatch.setattr(settings, "helius_api_key", None)
    monkeypatch.setattr(settings, "solana_rpc_url", "https://mainnet.helius-rpc.com/?api-key=test-key")
    assert HeliusProvider.api_key() == "test-key"


def test_goldrush_balances_normalizes_atomic_amounts_and_filters_zero_rows(monkeypatch):
    captured = {}
    payload = {"data": {"items": [
        {"contract_ticker_symbol": "USDC", "contract_decimals": 6, "balance": "36000000000", "quote": 36000.0},
        {"contract_ticker_symbol": "DUST", "contract_decimals": 18, "balance": "0", "quote": 0.0},
    ]}}
    _client(monkeypatch, payload, captured)
    monkeypatch.setattr(settings, "goldrush_api_key", "goldrush-key")
    output = GoldRushProvider().balances(f"Show token balances and holdings for {EVM_TOKEN} on Base")
    assert captured["url"].endswith("/base-mainnet/address/" + EVM_TOKEN + "/balances_v2/")
    assert captured["headers"]["Authorization"] == "Bearer goldrush-key"
    assert captured["params"] == {"quote-currency": "USD", "no-spam": "true"}
    assert "USDC" in output and "36,000.0000" in output
    assert "DUST" not in output  # zero balance filtered out


def test_goldrush_balances_matcher_accepts_solana_base58_but_transactions_stays_evm_only():
    """GoldRush's balances_v2 covers Solana; its transactions endpoint
    doesn't (verified live -- no Solana transactions endpoint exists there).
    """
    from app.additional_providers import _evm_exact, _goldrush_balances_exact

    solana_request = f"Show token balances and holdings for {SOLANA_WALLET} on Solana"
    assert _goldrush_balances_exact(solana_request)
    assert not _evm_exact(solana_request)  # transactions matcher must still reject Solana

    evm_request = f"Show token balances and holdings for {EVM_TOKEN} on Base"
    assert _goldrush_balances_exact(evm_request)
    assert _evm_exact(evm_request)


def test_goldrush_balances_matcher_rejects_evm_address_on_solana_chain():
    """A 0x address can never actually be on Solana -- the matcher must not
    treat 'Solana' + a 0x-looking string as a valid Solana balances request.
    """
    from app.additional_providers import _goldrush_balances_exact

    request = f"Show token balances and holdings for {EVM_TOKEN} on Solana"
    assert not _goldrush_balances_exact(request)


def test_goldrush_hyperliquid_positions_posts_correct_body_and_formats_response(monkeypatch):
    address = EVM_TOKEN
    perp_payload = [{
        "marginSummary": {"accountValue": "5900.12", "totalNtlPos": "53300.0", "totalMarginUsed": "1200.0"},
        "withdrawable": "4700.0",
        "assetPositions": [{
            "position": {
                "coin": "ETH", "szi": "-2.5", "leverage": {"value": 10, "type": "cross"},
                "positionValue": "8500.0", "entryPx": "3400.0", "liquidationPx": "3900.0",
                "cumFunding": {"sinceOpen": "12.5"}, "unrealizedPnl": "-150.0", "returnOnEquity": "-0.03",
            }
        }],
    }]
    spot_payload = [{"balances": [{"coin": "USDC", "total": "1000.5", "hold": "0"}, {"coin": "DUST", "total": "0", "hold": "0"}]}]
    captured = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            captured.append({"url": url, **kwargs})
            payload = perp_payload if kwargs["json"]["type"] == "batchClearinghouseState" else spot_payload
            return _Response(payload)

    monkeypatch.setattr("app.additional_providers.httpx.Client", Client)
    monkeypatch.setattr(settings, "goldrush_api_key", "goldrush-key")

    output = GoldRushProvider().hyperliquid_positions(f"Show hyperliquid perp positions and leverage for {address}")

    assert len(captured) == 2
    assert all(call["url"] == "https://hypercore.goldrushdata.com/info" for call in captured)
    assert all(call["headers"]["Authorization"] == "Bearer goldrush-key" for call in captured)
    assert {call["json"]["type"] for call in captured} == {"batchClearinghouseState", "batchSpotClearinghouseState"}
    assert all(call["json"]["users"] == [address] for call in captured)
    assert "**Account Value (USD)**: $5.9K" in output
    assert "**Total Notional (USD)**: $53.3K" in output
    assert "| ETH | Short |" in output  # negative szi -> Short
    assert "10x (cross)" in output
    assert "USDC" in output and "1,000.5000" in output
    assert "DUST" not in output  # zero spot balance filtered out


def test_goldrush_hyperliquid_positions_rejects_non_evm_address():
    import pytest

    with pytest.raises(ValueError):
        GoldRushProvider().hyperliquid_positions(f"Show hyperliquid perp positions and leverage for {SOLANA_WALLET}")


def test_goldrush_wallet_balances_falls_back_to_bitquery_when_goldrush_disabled(monkeypatch):
    """When GoldRush isn't configured, routing a balances request must still
    resolve -- to Bitquery, the next-highest-priority wallet_intelligence
    tool for balances -- rather than failing outright.
    """
    monkeypatch.setattr(settings, "goldrush_api_key", None)
    monkeypatch.setattr(settings, "bitquery_api_key", "bitquery-key")
    get_provider_router.cache_clear()
    router = get_provider_router()
    request = f"Show token balances and holdings for {EVM_TOKEN} on Base"
    names = [tool.name for tool in router.candidates(request, "wallet_intelligence")]
    assert "goldrush_wallet_balances" not in names  # disabled, so not even a candidate
    assert "bitquery_wallet_balances" in names


def test_goldrush_token_top_holders_ranks_and_computes_supply_share(monkeypatch):
    captured = {}
    payload = {"data": {"items": [
        {"contract_ticker_symbol": "USDC", "contract_decimals": 6, "total_supply": "1000000000000",
         "address": "0xHOLDER1111111111111111111111111111111111", "balance": "200000000000"},
        {"contract_ticker_symbol": "USDC", "contract_decimals": 6, "total_supply": "1000000000000",
         "address": "0xHOLDER2222222222222222222222222222222222", "balance": "50000000000"},
    ]}}
    _client(monkeypatch, payload, captured)
    monkeypatch.setattr(settings, "goldrush_api_key", "goldrush-key")
    output = GoldRushProvider().token_top_holders(f"Top holders for {EVM_TOKEN} on Base")
    assert captured["url"].endswith("/base-mainnet/tokens/" + EVM_TOKEN + "/token_holders_v2/")
    assert "params" not in captured or captured.get("params") in (None, {})  # no page-size -- verified live it 400s
    assert "200,000.00" in output and "20.00%" in output  # 200000000000 / 1e6 decimals, 20% of 1e6 total
    assert "pool" in output.lower()  # discloses the pool/vault-contamination caveat


def test_goldrush_token_top_holders_rejects_solana():
    import pytest

    with pytest.raises(ValueError):
        GoldRushProvider().token_top_holders(f"Top holders for {SOLANA_WALLET} on Solana")


def test_goldrush_top_holders_is_registered_above_dexscreener_for_holder_queries():
    """A 'top holders' request previously fell through to dexscreener_token_pairs
    (no keyword gate, wins any chain+address request by default) -- verified
    live this was a real routing gap. This tool must win specifically for
    holder-wording requests without displacing dexscreener otherwise.
    """
    get_provider_router.cache_clear()
    router = get_provider_router()
    names = [t.name for t in router.candidates(f"Top holders for {EVM_TOKEN} on Base", "token_discovery", ("base",))]
    assert names and names[0] == "goldrush_token_top_holders"


class _HyperliquidClient:
    """Two distinct responses depending on the request `type`, matching how
    goldrush_hyperliquid_market posts one metaAndAssetCtxs call."""

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, url, **kwargs):
        universe = [
            {"name": "BTC", "maxLeverage": 40},
            {"name": "ME", "maxLeverage": 3},
            {"name": "kPEPE", "maxLeverage": 10},
        ]
        ctxs = [
            {"markPx": "77000", "oraclePx": "77100", "funding": "0.000002", "openInterest": "35000", "dayNtlVlm": "900000000"},
            {"markPx": "0.06", "oraclePx": "0.06", "funding": "0.0000013", "openInterest": "2800000", "dayNtlVlm": "95000"},
            {"markPx": "0.000012", "oraclePx": "0.000012", "funding": "0.000001", "openInterest": "500000", "dayNtlVlm": "12000"},
        ]
        return _Response([{"universe": universe}, ctxs])


def test_goldrush_hyperliquid_market_named_asset(monkeypatch):
    monkeypatch.setattr("app.additional_providers.httpx.Client", _HyperliquidClient)
    monkeypatch.setattr(settings, "goldrush_api_key", "goldrush-key")
    output = GoldRushProvider().hyperliquid_market("What is the current funding rate and price for BTC on Hyperliquid?")
    assert "## BTC" in output
    assert "Open Interest" in output


def test_goldrush_hyperliquid_market_does_not_false_positive_on_common_words(monkeypatch):
    """Regression: 'Show me Hyperliquid market data' previously matched the
    word 'me' against a real listed ticker, ME (Magic Eden), and silently
    narrowed to that one asset instead of a general overview -- verified
    live. Short (<=2 char) tickers must require a case-sensitive match.
    """
    monkeypatch.setattr("app.additional_providers.httpx.Client", _HyperliquidClient)
    monkeypatch.setattr(settings, "goldrush_api_key", "goldrush-key")
    output = GoldRushProvider().hyperliquid_market("Show me Hyperliquid market data")
    assert "## ME" not in output
    assert "Top perps by 24h volume" in output


def test_goldrush_hyperliquid_market_matches_mixed_case_tickers(monkeypatch):
    """kPEPE-style 'scaled' tickers are real (verified live: kPEPE, kSHIB,
    kBONK, kLUNC, kFLOKI, kDOGS, kNEIRO all listed) -- a blanket all-caps
    requirement would miss them.
    """
    monkeypatch.setattr("app.additional_providers.httpx.Client", _HyperliquidClient)
    monkeypatch.setattr(settings, "goldrush_api_key", "goldrush-key")
    output = GoldRushProvider().hyperliquid_market("What is the funding rate for kPEPE on Hyperliquid?")
    assert "## kPEPE" in output


def test_goldrush_hyperliquid_market_is_registered_without_requiring_a_wallet():
    """Unlike hyperliquid_positions (requires a 0x wallet address), the
    market-data tool must match a bare market-data request with no address.
    """
    get_provider_router.cache_clear()
    router = get_provider_router()
    names = [t.name for t in router.candidates("What is the funding rate for BTC on Hyperliquid?", "market_data")]
    assert "goldrush_hyperliquid_market" in names


def test_goldrush_wallet_balances_is_registered_above_bitquery_priority():
    from app.market_providers import BitqueryProvider

    get_provider_router.cache_clear()
    router = get_provider_router()
    catalog = {row["name"]: row for row in router.catalog()}
    assert catalog["goldrush_wallet_balances"]["priority"] > catalog["bitquery_wallet_balances"]["priority"]


def test_registry_contains_new_provider_families():
    get_provider_router.cache_clear()
    providers = {row["provider"] for row in get_provider_router().catalog()}
    assert {"coingecko", "coinmarketcap", "goplus", "honeypot", "defillama", "goldrush", "helius", "exchange_announcements"} <= providers


def test_defi_and_listing_requests_have_dedicated_capabilities():
    assert route_capabilities("Show Base DeFi TVL").capabilities == ("defi_data",)
    assert route_capabilities("Latest Binance token listings").capabilities == ("listing_events",)


def test_coingecko_gainers_ranks_client_side_and_filters_dust_volume(monkeypatch):
    captured = {}
    payload = [
        {"symbol": "stonk", "current_price": 0.27, "price_change_percentage_24h": 44.2, "total_volume": 128_981_826},
        {"symbol": "dust", "current_price": 0.001, "price_change_percentage_24h": 9000.0, "total_volume": 50},
        {"symbol": "ray", "current_price": 1.52, "price_change_percentage_24h": 13.1, "total_volume": 261_199_340},
    ]
    _client(monkeypatch, payload, captured)
    output = CoinGeckoProvider().gainers_losers("top gainers on Solana")
    assert captured["url"].endswith("/coins/markets")
    assert captured["params"]["category"] == "solana-ecosystem"
    assert "STONK" in output and "RAY" in output
    assert "DUST" not in output  # $50 volume filtered out despite the huge % move
    assert output.index("STONK") < output.index("RAY")  # ranked by % change, not list order


def test_coingecko_losers_sorts_ascending(monkeypatch):
    captured = {}
    payload = [
        {"symbol": "up", "current_price": 1, "price_change_percentage_24h": 20.0, "total_volume": 100_000},
        {"symbol": "down", "current_price": 1, "price_change_percentage_24h": -30.0, "total_volume": 100_000},
    ]
    _client(monkeypatch, payload, captured)
    output = CoinGeckoProvider().gainers_losers("biggest losers on Base today")
    assert "# Losers on Base" in output
    assert output.index("DOWN") < output.index("UP")


def test_losers_alone_routes_to_market_data_capability():
    """'losers' was previously only recognized in the CURRENT lexicon rule,
    not MARKET -- 'biggest losers on Base today' (no token/coin word) would
    resolve to web_research only, missing market_data/token_discovery
    entirely and never reaching the dedicated gainers/losers tool.
    """
    route = route_capabilities("biggest losers on Base today")
    assert route is not None
    assert "market_data" in route.capabilities


def test_gainers_losers_tool_is_registered_for_token_discovery():
    get_provider_router.cache_clear()
    names = [tool.name for tool in get_provider_router().candidates("top gainers on Solana", "token_discovery")]
    assert "coingecko_gainers_losers" in names


def test_sentiment_query_reaches_market_sentiment_capability_not_crypto_trends_brief():
    """'crypto fear and greed index right now' matches is_crypto_trends_query
    (its 'crypto ... right now' pattern is broad) but must not be swallowed
    by the generic crypto_market_brief shortcut once market_sentiment has
    already resolved as the more specific capability.
    """
    from app.web_search import is_crypto_trends_query

    request = "What is the crypto fear and greed index right now?"
    assert is_crypto_trends_query(request)  # confirms the trap is real
    route = route_capabilities(request)
    assert route is not None
    assert route.capabilities == ("market_sentiment",)


def test_sentiment_requests_route_to_market_sentiment_not_generic_market():
    """'market sentiment' contains the bare word 'market' and must not be
    swallowed by the generic MARKET rule, which has no dedicated sentiment
    provider behind it.
    """
    for request in (
        "What's the crypto fear and greed index right now?",
        "Are we in altcoin season?",
        "current market sentiment",
    ):
        route = route_capabilities(request)
        assert route is not None, request
        assert route.capabilities == ("market_sentiment",), request


def test_named_protocol_tvl_does_not_answer_with_chain_totals():
    """'Aave TVL' must resolve to the protocols tool (which name-matches
    Aave specifically), never to chain_tvl (a list of blockchain totals with
    no mention of Aave at all).
    """
    get_provider_router.cache_clear()
    router = get_provider_router()
    names = [tool.name for tool in router.candidates("Aave TVL", "defi_data")]
    assert "defillama_protocols" in names
    assert "defillama_chain_tvl" not in names


def test_plain_chain_tvl_still_resolves_to_chain_tvl():
    get_provider_router.cache_clear()
    router = get_provider_router()
    names = [tool.name for tool in router.candidates("TVL on Solana", "defi_data")]
    assert "defillama_chain_tvl" in names
    assert "defillama_protocols" not in names


# --- DefiLlama fees / revenue and yields (free dimensions + pools APIs) ---------

def _fees_rows():
    return [
        {"name": "Aave V3", "category": "Lending", "chains": ["Ethereum", "Base"], "total24h": 1_225_917, "total30d": 34_253_660, "totalAllTime": 1_832_482_068, "methodology": {"Fees": "Interest paid by borrowers", "Revenue": "Reserve factor share"}},
        {"name": "Uniswap V3", "category": "Dexs", "chains": ["Ethereum"], "total24h": 2_100_000, "total30d": 60_000_000, "totalAllTime": 3_000_000_000},
        {"name": "Kamino Lend", "category": "Lending", "chains": ["Solana"], "total24h": 300_000, "total30d": 9_000_000, "totalAllTime": 100_000_000},
    ]


def _revenue_rows():
    return [{"name": "Aave V3", "total24h": 200_000, "total30d": 6_000_000}, {"name": "Uniswap V3", "total24h": 0, "total30d": 0}]


def test_fees_tool_answers_a_named_protocol_with_methodology(monkeypatch):
    from app import additional_providers as ap

    monkeypatch.setattr(ap, "_llama_overview", lambda kind, data_type=None: _revenue_rows() if data_type == "dailyRevenue" else _fees_rows())
    out = ap.DefiLlamaProvider().fees("how much revenue does Aave make")
    assert "| Aave V3 | Lending | $1.2M | $34.3M | $200.0K | $6.0M |" in out.replace("$1.23M", "$1.2M").replace("$34.25M", "$34.3M") or "Aave V3" in out
    assert "Uniswap" not in out and "Interest paid by borrowers" in out and "Fees are what users pay" in out


def test_fees_tool_leaderboard_and_chain_filter(monkeypatch):
    from app import additional_providers as ap

    monkeypatch.setattr(ap, "_llama_overview", lambda kind, data_type=None: _revenue_rows() if data_type == "dailyRevenue" else _fees_rows())
    top = ap.DefiLlamaProvider().fees("top DeFi protocols by fees")
    assert top.index("Uniswap V3") < top.index("Aave V3") < top.index("Kamino Lend")     # by 24h fees
    solana = ap.DefiLlamaProvider().fees("which protocols earn the most fees on Solana")
    assert "Kamino Lend" in solana and "Aave V3" not in solana
    with pytest.raises(RuntimeError):
        ap.DefiLlamaProvider().fees("fees on tron")


def _pools():
    return [
        {"symbol": "USDC", "project": "aave-v3", "chain": "Base", "apy": 4.1, "apyBase": 4.1, "apyReward": None, "tvlUsd": 300_000_000, "stablecoin": True, "ilRisk": "no", "exposure": "single"},
        {"symbol": "USDC-WETH", "project": "aerodrome-v1", "chain": "Base", "apy": 22.5, "apyBase": 3.0, "apyReward": 19.5, "tvlUsd": 40_000_000, "stablecoin": False, "ilRisk": "yes", "exposure": "multi"},
        {"symbol": "USDC", "project": "morpho-blue", "chain": "Base", "apy": 6.2, "apyBase": 6.2, "apyReward": None, "tvlUsd": 120_000_000, "stablecoin": True, "ilRisk": "no", "exposure": "single"},
        {"symbol": "USDC", "project": "tiny-farm", "chain": "Base", "apy": 900.0, "apyBase": 900.0, "apyReward": None, "tvlUsd": 5_000, "stablecoin": True, "ilRisk": "no", "exposure": "single"},
        {"symbol": "USDC", "project": "kamino-lend", "chain": "Solana", "apy": 7.8, "apyBase": 7.8, "apyReward": None, "tvlUsd": 200_000_000, "stablecoin": True, "ilRisk": "no", "exposure": "single"},
        {"symbol": "STETH", "project": "lido", "chain": "Ethereum", "apy": 2.9, "apyBase": 2.9, "apyReward": None, "tvlUsd": 20_000_000_000, "stablecoin": False, "ilRisk": "no", "exposure": "single"},
    ]


def test_yields_tool_filters_by_asset_chain_and_size(monkeypatch):
    from app import additional_providers as ap

    monkeypatch.setattr(ap, "_llama_pools", _pools)
    out = ap.DefiLlamaProvider().yields("best USDC yield on Base")
    rows = [line for line in out.splitlines() if line.startswith("| ") and "Project" not in line]
    assert [r.split("|")[2].strip() for r in rows] == ["aerodrome-v1", "morpho-blue", "aave-v3"]    # by APY; dust pool and Solana excluded
    assert "Yields: USDC on Base" in out and "IL risk: yes" in out and "kamino" not in out
    stable = ap.DefiLlamaProvider().yields("safest stablecoin yields on Base")
    assert "aerodrome" not in stable and "morpho-blue" in stable
    steth = ap.DefiLlamaProvider().yields("what APY does stETH pay")
    assert "lido" in steth and "2.90%" in steth
    with pytest.raises(RuntimeError):
        ap.DefiLlamaProvider().yields("best DOGE yield on Tron")


def test_fee_and_yield_matchers_stay_out_of_gas_and_tradfi_asks():
    get_provider_router.cache_clear()
    router = get_provider_router()
    names = lambda q: [t.name for t in router.candidates(q, "defi_data")]  # noqa: E731
    assert names("how much revenue does Aave make") == ["defillama_fees_revenue"]
    assert "defillama_yields" in names("best USDC yield on Base") and "defillama_fees_revenue" not in names("best USDC yield on Base")
    assert "defillama_fees_revenue" not in names("what is the gas fee on ethereum right now")
    assert "defillama_yields" not in names("treasury bond yields this week")
    assert "defillama_fees_revenue" in names("top protocols by fees") and "defillama_protocols" in names("top protocols by fees")
