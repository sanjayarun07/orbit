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


def test_pair_search_ranks_by_volume_and_drops_decoy_liquidity(monkeypatch):
    """Live: a $350M-liquidity "USELESS/USDC" pool with $3.99 of volume headed
    the table above the pools doing millions a day. Volume ranks; deep
    liquidity with no trading is a decoy once any pair shows real volume."""
    from app import dexscreener_tools

    def pair(dex, chain, liq, vol, quote="USDC"):
        return {"chainId": chain, "dexId": dex, "url": "#", "baseToken": {"symbol": "USELESS", "name": "Useless"}, "quoteToken": {"symbol": quote},
                "priceUsd": "0.24", "liquidity": {"usd": liq}, "volume": {"h24": vol}, "priceChange": {"h24": 21.6}}

    monkeypatch.setattr(dexscreener_tools, "_get", lambda path, params=None: {"pairs": [
        pair("orca", "solana", 350_600_000, 3.99), pair("raydium", "solana", 246_800_000, 199_300), pair("uniswap", "ethereum", 25_100_000, 0.01, "WETH"),
        pair("raydium", "solana", 4_800_000, 3_000_000, "SOL"), pair("meteora", "solana", 1_400_000, 1_100_000, "SOL"),
    ]})
    out = dexscreener_tools.dexscreener_pair_search("USELESS token")
    rows = [line for line in out.splitlines() if line.startswith("| [")]
    assert [r.split("|")[2].strip() for r in rows] == ["solana / raydium", "solana / meteora", "solana / raydium"]
    assert "orca" not in out and "ethereum" not in out
    # With no real volume anywhere (a brand-new listing) nothing is dropped.
    monkeypatch.setattr(dexscreener_tools, "_get", lambda path, params=None: {"pairs": [pair("orca", "solana", 2_000_000, 50.0)]})
    assert "orca" in dexscreener_tools.dexscreener_pair_search("USELESS token")
