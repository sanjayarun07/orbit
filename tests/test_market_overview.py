from app import market_overview
from app.market_overview import crypto_market_overview
from app.nodes.research import _MARKET_OVERVIEW, TRENDING_TOKENS


def _fake_sources():
    return {
        market_overview._GLOBAL_URL: {"data": {
            "total_market_cap": {"usd": 2.69e12},
            "market_cap_change_percentage_24h_usd": -0.16,
            "market_cap_percentage": {"btc": 58.6, "eth": 11.4},
        }},
        market_overview._SIMPLE_URL: {
            "bitcoin": {"usd": 78600, "usd_24h_change": 1.84},
            "ethereum": {"usd": 2500, "usd_24h_change": 0.81},
            "solana": {"usd": 102.39, "usd_24h_change": 1.81},
        },
        market_overview._MARKETS_URL: [
            {"symbol": "br", "price_change_percentage_24h": 77.84},
            {"symbol": "npc", "price_change_percentage_24h": 12.82},
            {"symbol": "mid", "price_change_percentage_24h": 0.5},
            {"symbol": "ff", "price_change_percentage_24h": -13.27},
            {"symbol": "stonk", "price_change_percentage_24h": -13.72},
        ],
        market_overview._TRENDING_URL: {"coins": [
            {"item": {"symbol": "lsk"}}, {"item": {"symbol": "fil"}}, {"item": {"symbol": "pengu"}},
        ]},
        market_overview._FNG_URL: {"data": [{"value": "57", "value_classification": "Greed"}]},
        market_overview._CHAINS_TVL_URL: [
            {"name": "Ethereum", "tvl": 50.07e9}, {"name": "Solana", "tvl": 5.89e9}, {"name": "Base", "tvl": 5.65e9},
        ],
        market_overview._DEX_TOTAL_URL: {"total24h": 7.12e9, "change_1d": 8.20},
    }


def test_card_composes_all_sections(monkeypatch):
    monkeypatch.setattr(market_overview, "_cache", {})
    data = _fake_sources()
    monkeypatch.setattr(market_overview, "_get_json", lambda url: data[url])
    out = crypto_market_overview("crypto market overview")
    assert "# Crypto Market Overview" in out
    # Core quotes
    assert "BTC" in out and "$78.6K" in out and "+1.84%" in out
    assert "SOL" in out and "$102.39" in out
    assert "Total market cap" in out and "$2.69T" in out
    # Market pulse
    assert "Fear & Greed**: 57 · Greed" in out
    assert "BTC dominance**: 58.6%" in out and "ETH**: 11.4%" in out
    assert "DEX volume (24h)**: $7.12B" in out
    assert "Ethereum $50.07B" in out
    # Movers: highest gainer first, biggest loser first
    assert "Gainers**: BR +77.84%" in out
    assert "Losers**: STONK -13.72%" in out
    # Trending + sources
    assert "LSK, FIL, PENGU" in out
    assert "[CoinGecko prices](" in out and "[DeFiLlama DEX volume](" in out
    assert "[Alternative.me sentiment](" in out and "**Retrieved**" in out


def test_card_degrades_when_all_sources_fail(monkeypatch):
    monkeypatch.setattr(market_overview, "_cache", {})
    def boom(url):
        raise RuntimeError("down")
    monkeypatch.setattr(market_overview, "_get_json", boom)
    out = crypto_market_overview("crypto market overview")
    assert "temporarily unavailable" in out


def test_card_renders_with_partial_sources(monkeypatch):
    # Only core quotes available; other sources fail -> card still renders quotes.
    monkeypatch.setattr(market_overview, "_cache", {})
    data = _fake_sources()
    def partial(url):
        if url in (market_overview._SIMPLE_URL, market_overview._GLOBAL_URL):
            return data[url]
        raise RuntimeError("down")
    monkeypatch.setattr(market_overview, "_get_json", partial)
    out = crypto_market_overview("crypto market overview")
    assert "## Core quotes" in out and "BTC" in out
    assert "## Top movers" not in out   # markets source failed


def test_overview_regex_matches_broad_asks_not_token_or_trending():
    for hit in ("crypto market overview", "how's the market today", "market snapshot",
                "what is happening in crypto", "state of the crypto market"):
        assert _MARKET_OVERVIEW.search(hit), hit
    # Must NOT hijack a ranked-list "trending tokens" ask.
    assert TRENDING_TOKENS.search("trending tokens on solana")
    assert not _MARKET_OVERVIEW.search("top holders of BONK")
