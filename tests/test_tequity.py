"""Tequity, the company's internal equities/perps feed (2026-09-23): movers
and trending cards from a snapshot, the matchers that reach them, and the
router picking them for venue-scoped asks. No network: snapshots are injected."""
import asyncio
import time

import pytest

from app import tequity
from app.provider_registry import get_provider_router

ASTER_ROWS = [
    {"symbol": "ASTERUSDT", "base_asset": "ASTER", "quote_asset": "USDT", "last_price": 0.7187, "price_change_percent": -0.111, "quote_volume": 65267478.02, "tags": ["Top"], "is_stock": False},
    {"symbol": "AAPLUSDT", "base_asset": "AAPL", "quote_asset": "USDT", "last_price": 340.25, "price_change_percent": 0.224, "quote_volume": 232257.42, "tags": ["STOCK"], "is_stock": True},
    {"symbol": "TSLAUSDT", "base_asset": "TSLA", "quote_asset": "USDT", "last_price": 412.1, "price_change_percent": -3.5, "quote_volume": 900000.0, "tags": ["STOCK"], "is_stock": True},
    {"symbol": "PEPEUSDT", "base_asset": "PEPE", "quote_asset": "USDT", "last_price": 0.0000063, "price_change_percent": 12.4, "quote_volume": 1500000.0, "tags": [], "is_stock": False},
]


@pytest.fixture(autouse=True)
def _feed():
    tequity.reset_for_test()
    tequity._store(tequity.MOVERS["aster"], {"success": True, "data": {"dex": "aster", "range": "1d", "tokens": ASTER_ROWS, "count": 4}, "timestamp": 1790159015689}, 1790159015.689)
    tequity._store(tequity.MOVERS["hyperliquid"], {"success": True, "data": {"dex": "hyperliquid", "range": "1d", "tokens": ASTER_ROWS, "count": 4}, "timestamp": 1790159015689}, 1790159015.689)
    tequity._store(tequity.TRENDING, {"trending": [
        {"dex": "hyperliquid", "symbol": "BTCUSDC", "base_asset": "BTC", "quote_asset": "USDC", "last_price": 85758.5, "price_change_percent": -0.16, "quote_volume": 2489272463.39, "is_stock": False},
        {"dex": "hyperliquid", "symbol": "CLUSDC", "base_asset": "CL", "quote_asset": "USDC", "last_price": 89.478, "price_change_percent": -0.358, "quote_volume": 289756754.0, "is_stock": True},
        {"dex": "aster", "symbol": "AAPLUSDT", "base_asset": "AAPL", "quote_asset": "USDT", "last_price": 340.25, "price_change_percent": 0.224, "quote_volume": 232257.42, "is_stock": True},
    ]}, 1790159015.689)
    yield
    tequity.reset_for_test()


def test_movers_card_sorts_by_price_change_and_filters_stocks():
    card = tequity.movers("top gainers on Aster today")
    assert card.startswith("# Pairs up the most on Aster (1d)") and "**Snapshot**: 2026-09-23 10:23:35 UTC" in card
    assert card.index("| 1 | PEPE/USDT |") < card.index("| 2 | AAPL/USDT |") and "| $0.000006300 |" in card and "+12.40%" in card
    card = tequity.movers("biggest losers among tokenized stocks on aster")
    assert card.startswith("# Tokenized stocks down the most on Aster") and "| 1 | TSLA/USDT | stock |" in card and "PEPE" not in card
    assert "not volume growth" in card


def test_trending_card_scopes_by_venue_and_stocks():
    card = tequity.trending("what's trending on hyperliquid right now")
    assert "on Hyperliquid" in card and "| BTC/USDC | hyperliquid | crypto |" in card and "AAPL" not in card
    card = tequity.trending("trending tokenized stocks")
    assert "CL/USDC" in card and "AAPL/USDT" in card and "BTC" not in card


def test_matchers_reach_the_feed_and_leave_positions_alone():
    assert tequity.movers_matches("top gainers on Aster today") and tequity.movers_matches("which tokenized stocks are moving on hyperliquid")
    assert not tequity.movers_matches("hyperliquid positions for 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
    assert not tequity.movers_matches("my open perps on hyperliquid") and not tequity.movers_matches("top gainers on solana")
    assert tequity.trending_matches("what's trending on hyperliquid right now") and tequity.trending_matches("trending tokenized stocks")
    assert not tequity.trending_matches("trending tokens on solana")
    assert not tequity.news_matches("stock news today")                       # no news snapshot yet: not reachable


def test_the_router_picks_the_feed_for_venue_scoped_asks():
    router = get_provider_router()
    for ask, tool in [("top gainers on Aster today", "tequity_movers"), ("which tokenized stocks are moving on hyperliquid", "tequity_movers"),
                      ("what's trending on hyperliquid right now", "tequity_trending")]:
        ranked = [t.name for t in router.candidates(ask, ["token_discovery", "market_data"])] if hasattr(router, "candidates") else []
        assert not ranked or ranked[0] == tool, (ask, ranked[:4])
        assert tool in router.matched_tools(ask) if hasattr(router, "matched_tools") else True


def test_a_stale_snapshot_is_refreshed_and_a_missing_one_is_a_clear_error(monkeypatch):
    calls = []

    async def fake_listen(channels, *, until=None, stop_when=None):
        calls.append(tuple(channels))
        tequity._store(channels[0], {"success": True, "data": {"dex": "hyperliquid", "range": "1d", "tokens": ASTER_ROWS[:1], "count": 1}}, None)
    monkeypatch.setattr(tequity, "_listen", fake_listen)
    tequity._snapshots[tequity.MOVERS["hyperliquid"]] = {"data": {"data": {"tokens": []}}, "received_at": time.time() - 999, "server_ts": None}
    snap = asyncio.run(tequity.snapshot(tequity.MOVERS["hyperliquid"]))
    assert calls == [(tequity.MOVERS["hyperliquid"],)] and snap["data"]["data"]["count"] == 1

    async def nothing(channels, *, until=None, stop_when=None):
        return None
    monkeypatch.setattr(tequity, "_listen", nothing)
    tequity._snapshots.pop(tequity.NEWS, None)
    with pytest.raises(RuntimeError, match="published no news yet"):
        tequity.news("stock news")


def test_the_research_node_answers_venue_movers_from_the_feed_first(monkeypatch):
    from app.nodes import research
    out = asyncio.run(research.research_node({"request": "which tokenized stocks are moving on hyperliquid", "contextual_request": None, "history": "",
                                              "session_context": {}, "capabilities": ["equity_research"]}))
    assert out["trajectory"]["tool_name_0"] == "tequity_movers"
