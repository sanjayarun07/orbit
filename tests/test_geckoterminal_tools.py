from app import geckoterminal_tools
from app.provider_registry import get_provider_router


def _fake_payload(name):
    return {"data": [{
        "attributes": {
            "name": name,
            "base_token_price_usd": "0.0012",
            "volume_usd": {"h24": "123456"},
            "reserve_in_usd": "78900",
            "price_change_percentage": {"h24": "12.5"},
            "pool_created_at": "2026-09-13T00:00:00Z",
            "address": "0xpool",
        }
    }]}


def test_geckoterminal_picks_new_vs_trending_and_resolves_launchpad(monkeypatch):
    calls = []

    def fake_get(path):
        calls.append(path)
        return _fake_payload("SYNAPSE / ETH")

    monkeypatch.setattr(geckoterminal_tools, "_get", fake_get)

    out_new = geckoterminal_tools.geckoterminal_pools("show new pairs on Base")
    assert "/networks/base/new_pools" in calls[-1]
    assert "Newly-created pools on Base" in out_new
    assert "SYNAPSE / ETH" in out_new

    out_trend = geckoterminal_tools.geckoterminal_pools("trending tokens on solana")
    assert "/networks/solana/trending_pools" in calls[-1]
    assert "Trending pools on Solana" in out_trend

    # pump.fun resolves to the Solana network id.
    geckoterminal_tools.geckoterminal_pools("hottest gems on pump.fun")
    assert "/networks/solana/" in calls[-1]


def test_geckoterminal_requires_a_known_chain():
    import pytest
    with pytest.raises(ValueError):
        geckoterminal_tools.geckoterminal_pools("trending tokens")  # no chain/launchpad


def test_new_pairs_on_base_selects_geckoterminal():
    get_provider_router.cache_clear()
    names = [tool.name for tool in get_provider_router().candidates("Show new pairs on Base", "token_discovery")]
    assert names[0] == "geckoterminal_pools"


def test_trending_tokens_on_chain_selects_geckoterminal():
    get_provider_router.cache_clear()
    names = [tool.name for tool in get_provider_router().candidates("trending tokens on Base", "market_data")]
    assert names[0] == "geckoterminal_pools"
