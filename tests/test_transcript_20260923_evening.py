"""The evening transcript of 2026-09-23: the market is the crypto market,
"BTC fell sharply" is a why-moving ask, a venue typed a letter off is still
the venue, and a venue pair has a quote tool."""
import time

from app import listed_asset, tequity, why_moving
from app.routing import subject_probe


def test_the_market_is_the_crypto_market_unless_stocks_are_named():
    assert why_moving.market_ask("Why market is falling?") == "down" and why_moving.market_ask("why is the crypto market up today") == "up"
    assert why_moving.market_ask("why is the stock market down") is None and why_moving.market_ask("why is SOL down") is None


def test_a_statement_about_a_move_is_a_why_moving_ask(monkeypatch):
    from app import symbol_registry
    listed = {"BTC": [{"id": "bitcoin", "name": "Bitcoin", "symbol": "BTC", "rank": 1}],
              "SOL": [{"id": "solana", "name": "Solana", "symbol": "SOL", "rank": 6}],
              "WENT": [{"id": "went-coin", "name": "Went", "symbol": "WENT", "rank": None}]}
    monkeypatch.setattr(symbol_registry, "listed", lambda sym: listed.get(sym.upper(), []))
    assert why_moving.match("no just now BTC fell sharply") == ("BTC", "down") and why_moving.match("SOL is pumping hard") == ("SOL", "up")
    assert why_moving.match("btc fell sharply") == ("BTC", "down")
    assert why_moving.match("USDC is down") is None and why_moving.match("the market fell") is None
    # A ranking ask is not a statement about a coin called WENT (live episode, 2026-09-23).
    assert why_moving.match("Which Base tokens went up the most today?") is None
    assert why_moving.match("tokens in the Base ecosystem up the most today") is None


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


def test_the_why_moving_narrative_keeps_its_sources_when_cut():
    # A long news day trimmed the citations off the card (live episode, 2026-09-23).
    news = "\n".join(f"line {i} of the narrative about the move [1]" for i in range(80)) + "\n\nSources:\n[1] https://example.com/a\n[2] https://example.com/b"
    cut = why_moving._trim(news, 600)
    assert cut.endswith("Sources:\n[1] https://example.com/a\n[2] https://example.com/b") and "…" in cut and len(cut) < len(news)
    assert why_moving._trim("short\n\nSources:\n[1] x", 600) == "short\n\nSources:\n[1] x" and why_moving._trim("plain", 5) == "plain"


def test_a_short_venue_name_is_matched_exactly_only():
    # "after", "faster" and "master" reached the Aster tools (review 2026-09-23).
    from app import contracts
    assert [tequity.fuzzy_venue(w) for w in ("after", "faster", "master")] == [None, None, None]
    assert tequity.fuzzy_venue("aster") == "aster" and tequity.fuzzy_venue("hyperloquid") == "hyperliquid"
    assert contracts.plan_by_rules("top crypto gainers after the crash").venue is None
    assert not tequity.quote_matches("BTC price after crash") and tequity.quote_matches("chk hyperloquid btc price")
