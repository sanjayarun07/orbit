from app import dexscreener_tools


def test_trending_metas_are_sorted_and_cited(monkeypatch):
    monkeypatch.setattr(
        dexscreener_tools,
        "_get",
        lambda *_args: [
            {"name": "Small", "volume": 10, "liquidity": 5, "marketCap": 20, "tokenCount": 1, "marketCapChange": {"h24": -1}},
            {"name": "Large", "volume": 100, "liquidity": 50, "marketCap": 200, "tokenCount": 2, "marketCapChange": {"h24": 4}},
        ],
    )
    output = dexscreener_tools.dexscreener_trending_metas("trending crypto")
    assert output.index("Large") < output.index("Small")
    assert "metas/trending/v1" in output


def test_latest_profiles_filter_chain_and_label_limit(monkeypatch):
    monkeypatch.setattr(
        dexscreener_tools,
        "_get",
        lambda *_args: [
            {"chainId": "base", "tokenAddress": "0x1111111111111111111111111111111111111111", "url": "https://dexscreener.com/base/a"},
            {"chainId": "solana", "tokenAddress": "11111111111111111111111111111111", "url": "https://dexscreener.com/solana/a"},
        ],
    )
    output = dexscreener_tools.dexscreener_latest_profiles("new tokens on Base")
    assert "base" in output.lower()
    assert "solana/a" not in output
    assert "not a complete chronological list" in output


def test_latest_profiles_empty_chain_discloses_available_chains(monkeypatch):
    # "new pairs on Base" when the feed has no Base entries must not dead-end:
    # it should say which chains DO have data so the user can redirect.
    monkeypatch.setattr(
        dexscreener_tools,
        "_get",
        lambda *_args: [
            {"chainId": "solana", "tokenAddress": "11111111111111111111111111111111", "url": "u"},
            {"chainId": "robinhood", "tokenAddress": "0x2222222222222222222222222222222222222222", "url": "u"},
        ],
    )
    output = dexscreener_tools.dexscreener_latest_profiles("show new pairs on Base")
    assert "no Base tokens right now" in output
    assert "solana" in output and "robinhood" in output
