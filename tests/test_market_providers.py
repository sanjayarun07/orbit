from app.market_providers import BirdeyeProvider, BitqueryProvider, MobulaProvider
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
