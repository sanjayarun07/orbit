"""The evening transcript of 2026-09-23: the market is the crypto market,
"BTC fell sharply" is a why-moving ask, a venue typed a letter off is still
the venue, and a venue pair has a quote tool."""
import time

from app import listed_asset, tequity, why_moving
from app.routing import subject_probe


def test_the_market_is_the_crypto_market_unless_stocks_are_named():
    assert why_moving.market_ask("Why market is falling?") == "down" and why_moving.market_ask("why is the crypto market up today") == "up"
    assert why_moving.market_ask("why is the stock market down") is None and why_moving.market_ask("why is SOL down") is None


def test_a_statement_about_a_move_is_a_why_moving_ask():
    assert why_moving.match("no just now BTC fell sharply") == ("BTC", "down") and why_moving.match("SOL is pumping hard") == ("SOL", "up")
    assert why_moving.match("USDC is down") is None and why_moving.match("the market fell") is None


def test_a_venue_typed_a_letter_off_is_the_venue_not_a_token():
    assert tequity.fuzzy_venue("hyperloquid") == "hyperliquid" and tequity.fuzzy_venue("astr") is None and tequity.fuzzy_venue("hyper") is None
    assert tequity._dex_of("chk hyperloquid btc price") == "hyperliquid" and subject_probe.subject_of("chk hyperloquid btc price") is None
    assert not listed_asset.matches("hyperliquid btc price") and not listed_asset.matches("aster btc price")


def test_a_venue_pair_quote_comes_from_the_snapshot():
    tequity.reset_for_test()
    tequity._store(tequity.MOVERS["hyperliquid"], {"success": True, "data": {"dex": "hyperliquid", "range": "1d", "tokens": [
        {"symbol": "BTCUSDC", "base_asset": "BTC", "quote_asset": "USDC", "last_price": 86168.5, "price_change_percent": -1.2, "quote_volume": 2.5e9, "is_stock": False}]}}, time.time())
    assert tequity.quote_matches("hyperliquid btc price") and tequity.quote_matches("chk hyperloquid btc price") and not tequity.quote_matches("top gainers on hyperliquid")
    card = tequity.quote("hyperliquid btc price")
    assert card.startswith("# BTC on Hyperliquid") and "| Last price | $86,168.50 |" in card and "| 24h price change | -1.20% |" in card and "not a global reference price" in card
    try:
        tequity.quote("hyperliquid zzz price")
    except RuntimeError as exc:
        assert "ZZZ is not listed on hyperliquid" in str(exc)
    tequity.reset_for_test()
