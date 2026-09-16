from app.market_providers import BirdeyeProvider, BitqueryProvider, MarketSentimentProvider, MobulaProvider
from app.provider_registry import get_provider_router
from app.settings import settings


EVM_TOKEN = "0x1111111111111111111111111111111111111111"


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

    monkeypatch.setattr("app.market_providers.httpx.Client", Client)


def test_birdeye_provider_uses_key_and_chain_headers(monkeypatch):
    captured = {}
    _client(monkeypatch, {"data": {"name": "Token", "symbol": "TOK", "price": 1}}, captured)
    monkeypatch.setattr(settings, "birdeye_api_key", "bird-key")
    output = BirdeyeProvider().token_overview(f"Analyze {EVM_TOKEN} on Base")
    assert captured["headers"] == {"X-API-KEY": "bird-key", "x-chain": "base"}
    assert captured["params"]["address"] == EVM_TOKEN
    assert "Token (TOK)" in output


def test_mobula_provider_uses_v2_token_details(monkeypatch):
    captured = {}
    _client(monkeypatch, {"data": {"name": "Token", "symbol": "TOK", "priceUSD": 2}}, captured)
    monkeypatch.setattr(settings, "mobula_api_key", "mobula-key")
    output = MobulaProvider().token_details(f"Check {EVM_TOKEN} on Ethereum")
    assert captured["url"].endswith("/token/details")
    assert captured["headers"]["Authorization"] == "mobula-key"
    assert captured["params"]["blockchain"] == "ethereum"
    assert "$2.0000" in output


def test_bitquery_provider_posts_parameterized_graphql(monkeypatch):
    captured = {}
    payload = {
        "data": {
            "EVM": {
                "DEXTradeByTokens": [
                    {
                        "Block": {"Time": "2026-09-09T00:00:00Z"},
                        "Trade": {"PriceInUSD": 3, "Currency": {"Symbol": "TOK"}, "Dex": {"ProtocolName": "uniswap_v3"}},
                        "Transaction": {"Hash": "0xabcdef"},
                    }
                ]
            }
        }
    }
    _client(monkeypatch, payload, captured)
    monkeypatch.setattr(settings, "bitquery_api_key", "bitquery-key")
    output = BitqueryProvider().recent_dex_trades(f"Recent trades for {EVM_TOKEN} on Ethereum")
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer bitquery-key"
    assert captured["json"]["variables"] == {"network": "eth", "token": EVM_TOKEN}
    assert "uniswap_v3" in output


def test_bitquery_wallet_balances_posts_parameterized_graphql_and_filters_zero_rows(monkeypatch):
    captured = {}
    payload = {
        "data": {
            "EVM": {
                "BalanceUpdates": [
                    {"Currency": {"Symbol": "USDC", "SmartContract": "0xusdc"}, "balance": "1000.5"},
                    {"Currency": {"Symbol": "DUST", "SmartContract": "0xdust"}, "balance": "0"},
                ]
            }
        }
    }
    _client(monkeypatch, payload, captured)
    monkeypatch.setattr(settings, "bitquery_api_key", "bitquery-key")
    output = BitqueryProvider().wallet_balances(f"Show token balances and holdings for {EVM_TOKEN} on Ethereum")
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer bitquery-key"
    assert captured["json"]["variables"] == {"network": "eth", "address": EVM_TOKEN}
    assert "USDC" in output
    assert "DUST" not in output  # zero balance filtered out


def test_bitquery_wallet_balances_is_registered_for_wallet_intelligence(monkeypatch):
    monkeypatch.setattr(settings, "bitquery_api_key", "bitquery-key")
    get_provider_router.cache_clear()
    names = [tool.name for tool in get_provider_router().candidates(
        "Show token balances and holdings for 0x1111111111111111111111111111111111111111 on Ethereum",
        "wallet_intelligence",
    )]
    assert "bitquery_wallet_balances" in names


def test_market_sentiment_combines_fear_greed_and_altcoin_season(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **kwargs):
            calls.append(url)
            if "alternative.me" in url:
                return _Response({"data": [{"value": "72", "value_classification": "Greed"}]})
            return _Response({"data": {"altcoin_index": 82, "yearly_high": 90, "yearly_low": 10}})

    monkeypatch.setattr("app.market_providers.httpx.Client", Client)
    output = MarketSentimentProvider().snapshot("What's the fear and greed index?")
    assert len(calls) == 2
    assert "**Fear & Greed Index**: 72/100 -- Greed" in output
    assert "**Altcoin Season Index**: 82/100 -- Altcoin season" in output


def test_market_sentiment_is_registered_without_an_api_key(monkeypatch):
    get_provider_router.cache_clear()
    names = [tool.name for tool in get_provider_router().candidates(
        "What's the crypto fear and greed index?", "market_sentiment",
    )]
    assert "market_sentiment_snapshot" in names


_SOL_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def test_trending_tokens_query_selects_geckoterminal_not_narrative_metas():
    get_provider_router.cache_clear()
    names = [tool.name for tool in get_provider_router().candidates(
        "latest trending tokens on pump.fun", "token_discovery",
    )]
    # A token-level ask must reach a real per-chain token/pool list
    # (GeckoTerminal wins; DEX Screener boosted-tokens stays as failover), and
    # must NOT reach the narrative/meta tool (which answers a different
    # question).
    assert names[0] == "geckoterminal_pools"
    assert "dexscreener_boosted_tokens" in names  # failover still available
    assert "dexscreener_trending_metas" not in names


def test_top_holders_query_does_not_match_trending_tokens():
    # Regression: "top holders of BONK token" must NOT be read as "top ...
    # tokens" (a trending-token discovery query). The discovery word has to
    # modify tokens/coins directly, not merely co-occur with the word "token".
    from app.market_providers import TRENDING_TOKENS
    for holders in (
        "top holders of BONK token",
        "top holders of this token",
        "show the top 10 holders of the token",
        "who owns the most BONK token",
    ):
        assert not TRENDING_TOKENS.search(holders), holders
    for trending in ("top tokens on solana", "trending tokens on robinhood", "hottest new gems"):
        assert TRENDING_TOKENS.search(trending), trending


def test_trending_narratives_query_still_selects_metas():
    get_provider_router.cache_clear()
    names = [tool.name for tool in get_provider_router().candidates(
        "what narratives are trending right now?", "token_discovery",
    )]
    # No token word -> the narrative tool is still the right one.
    assert "dexscreener_trending_metas" in names
    assert "dexscreener_boosted_tokens" not in names


def test_solana_token_safety_selects_jupiter_shield_over_dexscreener_pairs():
    get_provider_router.cache_clear()
    candidates = get_provider_router().candidates(
        f"check token safety for {_SOL_MINT} on solana", "token_security",
    )
    names = [tool.name for tool in candidates]
    # Solana security must reach the Jupiter Shield tool, ranked above the
    # generic dexscreener pair lookup (which is not a safety assessment).
    assert names[0] == "solana_token_security"
    assert names.index("solana_token_security") < names.index("dexscreener_token_pairs")


def test_registry_contains_all_named_market_providers(monkeypatch):
    monkeypatch.setattr(settings, "birdeye_api_key", None)
    monkeypatch.setattr(settings, "mobula_api_key", None)
    monkeypatch.setattr(settings, "bitquery_api_key", None)
    get_provider_router.cache_clear()
    catalog = get_provider_router().catalog()
    providers = {item["provider"] for item in catalog}
    assert {"dexscreener", "birdeye", "mobula", "bitquery"} <= providers


def test_sui_address_is_not_truncated():
    address = "0x" + "a" * 64
    from app.market_providers import _address

    assert _address(f"Analyze {address} on Sui") == address


SOLANA_MINT = "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx"


def test_bitquery_top_holders_sorts_client_side_and_filters_zero_balances(monkeypatch):
    """Bitquery's own orderBy on an aggregated field was verified live NOT
    to actually sort the results -- this must be corrected client-side,
    not trust the API's row order.
    """
    captured = {}
    payload = {"data": {"Solana": {"BalanceUpdates": [
        {"BalanceUpdate": {"Account": {"Owner": "SmallSmallSmallSmallSmallSmall01"}, "Currency": {"Symbol": "STONK"}}, "balance": "100"},
        {"BalanceUpdate": {"Account": {"Owner": "BiggestBiggestBiggestBiggestBig02"}, "Currency": {"Symbol": "STONK"}}, "balance": "900000"},
        {"BalanceUpdate": {"Account": {"Owner": "ZeroZeroZeroZeroZeroZeroZeroZero03"}, "Currency": {"Symbol": "STONK"}}, "balance": "0"},
        {"BalanceUpdate": {"Account": {"Owner": "MidMidMidMidMidMidMidMidMidMid04"}, "Currency": {"Symbol": "STONK"}}, "balance": "5000"},
    ]}}}
    _client(monkeypatch, payload, captured)
    monkeypatch.setattr(settings, "bitquery_api_key", "bitquery-key")
    output = BitqueryProvider().token_top_holders(f"Top holders for {SOLANA_MINT} on Solana")
    assert captured["json"]["variables"] == {"token": SOLANA_MINT}
    assert output.index("Bigges") < output.index("MidMid") < output.index("SmallS")  # 6-char address prefix, per the output's own truncation
    assert "ZeroZero" not in output  # zero-balance row filtered out
    assert "pool" in output.lower() and "not pre-filtered" in output.lower()  # disclosed, not silently hidden


def test_bitquery_top_holders_rejects_non_solana_chain():
    import pytest

    with pytest.raises(ValueError):
        BitqueryProvider().token_top_holders(f"Top holders for {EVM_TOKEN} on Base")


def test_bitquery_top_holders_is_registered_under_token_discovery_and_security():
    get_provider_router.cache_clear()
    router = get_provider_router()
    catalog = {row["name"]: row for row in router.catalog()}
    assert set(catalog["bitquery_token_top_holders"]["capabilities"]) == {"token_discovery", "token_security"}
    assert catalog["bitquery_token_top_holders"]["chains"] == ["solana"]


def test_volume_and_boosts_regexes_recognize_everyday_phrasing():
    """Both patterns claimed coverage (docstring/comments) that the code did
    not actually implement: VOLUME_RANKED never had a standalone "turnover"
    branch, and TRENDING_TOKENS only matched "promoted tokens" (adjective
    before the noun), not "tokens are getting promoted" (predicate order,
    at least as common in real phrasing). Found live 2026-09-16 stress-
    testing slang/jargon prompts against the router."""
    from app.market_providers import TRENDING_TOKENS, VOLUME_RANKED
    for volume in (
        "which tokens got the most turnover today", "top coins today by $ traded",
        "highest volume coins", "most traded tokens today", "trending tokens by volume in 24hrs",
    ):
        assert VOLUME_RANKED.search(volume), volume
    for boosts in ("which tokens are getting promoted on dex screener", "tokens that are trending right now", "boosted tokens on solana"):
        assert TRENDING_TOKENS.search(boosts), boosts
    # The suppression in dexscreener_boosted_tokens's matcher (TRENDING_TOKENS
    # and not VOLUME_RANKED) must still hold: a pure boosts ask is not a volume ask.
    assert not VOLUME_RANKED.search("which tokens are getting promoted on dex screener")
