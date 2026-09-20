"""Binance listing status from Binance itself (review 2026-09-20: "Binance"
in scope means the exchange, which no on-chain source establishes)."""
import pytest

from app import exchange_listings as ex
from app.provider_registry import get_provider_router

SYMBOLS = [
    {"symbol": "BONKUSDT", "status": "TRADING", "baseAsset": "BONK", "quoteAsset": "USDT"},
    {"symbol": "BONKTRY", "status": "TRADING", "baseAsset": "BONK", "quoteAsset": "TRY"},
    {"symbol": "1000PEPEUSDT", "status": "TRADING", "baseAsset": "1000PEPE", "quoteAsset": "USDT"},
    {"symbol": "OLDBREAK", "status": "BREAK", "baseAsset": "OLD", "quoteAsset": "USDT"},
]


@pytest.fixture(autouse=True)
def _symbols(monkeypatch):
    monkeypatch.setattr(ex, "_symbols", lambda: SYMBOLS)


def test_a_listed_token_shows_its_pairs_quote_first():
    card = ex.binance_listing("is BONK listed on Binance?")
    assert "**BONK is listed on Binance spot** with 2 trading pair(s)." in card
    assert card.index("| BONKUSDT | TRADING | USDT |") < card.index("| BONKTRY | TRADING | TRY |")


def test_a_thousand_prefixed_listing_still_counts():
    assert "**PEPE is listed on Binance spot**" in ex.binance_listing("which binance pairs trade PEPE")


def test_an_unlisted_token_says_so_and_names_what_is_not_checked():
    card = ex.binance_listing("is FARTCOIN listed on binance")
    assert "**FARTCOIN is not listed on Binance spot.**" in card and "Futures and Binance Alpha" in card


@pytest.mark.parametrize("text,claims", [("is BONK listed on Binance", True), ("binance pairs for WIF", True), ("price of BONK", False), ("new launches on base", False)])
def test_only_listing_questions_reach_it(text, claims):
    assert bool(ex.LISTING_ASK.search(text)) is claims


def test_it_is_the_first_tool_for_a_binance_listing_question():
    ranked = [t.name for t in get_provider_router()._ranked_union("is BONK listed on Binance", ("listing_events", "market_data"), (), None)]
    assert ranked and ranked[0] == "binance_spot_listing", ranked
