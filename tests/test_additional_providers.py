from app.additional_providers import CoinGeckoProvider, GoPlusProvider, HeliusProvider
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


def test_registry_contains_new_provider_families():
    get_provider_router.cache_clear()
    providers = {row["provider"] for row in get_provider_router().catalog()}
    assert {"coingecko", "coinmarketcap", "goplus", "honeypot", "defillama", "goldrush", "helius", "exchange_announcements"} <= providers


def test_defi_and_listing_requests_have_dedicated_capabilities():
    assert route_capabilities("Show Base DeFi TVL").capabilities == ("defi_data",)
    assert route_capabilities("Latest Binance token listings").capabilities == ("listing_events",)
