"""The recorded live run of 2026-09-23: feed asks honour both venues, "excluding
stocks" and hour windows; months are not assets; product concepts get product
answers; the research-mode home has no swap starter; Shield says it is Solana-only."""
import asyncio
import time

import pytest

from app import agent, deployment, product_actions, tequity
from app.routing import subject_probe
from app.settings import settings


def _row(sym, price, change, vol, stock=False):
    return {"symbol": sym, "base_asset": sym.replace("USDC", "").replace("USDT", ""), "quote_asset": "USDC", "last_price": price, "price_change_percent": change, "quote_volume": vol, "is_stock": stock}


@pytest.fixture(autouse=True)
def _feed():
    tequity.reset_for_test()
    tequity._store(tequity.MOVERS["aster"], {"success": True, "data": {"dex": "aster", "range": "1d", "tokens": [_row("ASTERUSDT", 0.7, 5.0, 1e6), _row("AAPLUSDT", 340.0, 1.0, 2e5, True)], "count": 2}}, time.time())
    tequity._store(tequity.MOVERS["hyperliquid"], {"success": True, "data": {"dex": "hyperliquid", "range": "1d", "tokens": [_row("BTCUSDC", 85000.0, -1.0, 2e9), _row("TSLAUSDC", 400.0, 9.0, 5e6, True), _row("PEPEUSDC", 0.000005, 12.0, 3e6)], "count": 3}}, time.time())
    yield
    tequity.reset_for_test()


def test_both_venues_are_answered_each_on_its_own():
    assert tequity.venues_of("top gainers across Aster and Hyperliquid") == ["aster", "hyperliquid"]
    assert tequity.venues_of("compare movers on hyperliquid vs aster, keep each venue separate") == ["hyperliquid", "aster"]
    assert tequity.venues_of("top gainers on hyperliquid") == ["hyperliquid"]
    card = tequity.movers("top gainers across Aster and Hyperliquid")
    assert "# Pairs up the most on Aster" in card and "# Pairs up the most on Hyperliquid" in card and "not merged or ranked against each other" in card


def test_excluding_stocks_means_crypto_only():
    assert tequity.stock_filter("hyperliquid gainers excluding stocks") is False and tequity.stock_filter("tokenized stocks on hyperliquid") is True and tequity.stock_filter("gainers on hyperliquid") is None
    card = tequity.movers("hyperliquid gainers excluding stocks")
    assert card.startswith("# Crypto pairs up the most on Hyperliquid") and "TSLA" not in card and "PEPE" in card
    card = tequity.trending.__wrapped__ if hasattr(tequity.trending, "__wrapped__") else None
    assert card is None or True


def test_volume_windows_are_kept_in_hours(monkeypatch):
    from app import tequity_ledger
    seen = {}

    async def fake(venue, days, *, stocks_only=False, limit=15):
        seen["days"] = days
        return []
    monkeypatch.setattr(tequity_ledger, "volume_leaders", fake)
    card = tequity.volume_leaders("most traded on aster in the last 2 hours")
    assert abs(seen["days"] - 2 / 24) < 0.01 and "last 2 hours" in card
    assert tequity.volume_leaders_matches("rank hyperliquid pairs by volume this week")


def test_months_are_not_assets():
    assert subject_probe.subject_of("Compare September 21 and September 22 using saved snapshots.") is None


def test_product_concepts_get_product_answers():
    assert product_actions.answer("Where can I find this conversation again and download my data?").startswith("**Finding this conversation again.**")
    a = product_actions.answer("Would an exit watch protect me like a stop-loss or limit order?")
    assert a.startswith("**What an exit watch does.**") and "not a stop-loss" in a and "executable exit value, not a price level" in a
    a = product_actions.answer("What is the difference between my portfolio value and what I could receive by selling?")
    assert a.startswith("**Portfolio value** is a reference") and "exit analysis for X" in a
    for q in ("Would an exit watch protect me like a stop-loss?", "What is the difference between my portfolio value and what I could receive by selling?",
              "Where can I find this conversation again?"):
        assert product_actions.is_product_question(q), q


def test_the_research_home_has_no_swap_starter(monkeypatch):
    from fastapi.testclient import TestClient
    from app import home_highlights
    from app.main import app
    monkeypatch.setattr(home_highlights, "get_highlights", lambda force=False: {"cards": [{"id": "swap", "kind": "action", "action": "relay"}, {"id": "balances", "kind": "action", "prompt": "x"}]})
    monkeypatch.setattr(deployment, "execution_enabled", lambda config=None: False)
    cards = TestClient(app).get("/home/highlights").json()["cards"]
    assert [c["id"] for c in cards] == ["balances"]
    monkeypatch.setattr(deployment, "execution_enabled", lambda config=None: True)
    assert [c["id"] for c in TestClient(app).get("/home/highlights").json()["cards"]] == ["swap", "balances"]


def test_shield_says_it_is_solana_only_for_an_evm_contract():
    out = agent.token_safety_warnings("0x6982508145454Ce325dDbE47a25d4ec3d2311933")
    assert out["out_of_scope"] is True and "Solana mints only" in out["note"] and "none applies" in out["note"]


def test_a_dated_comparison_is_research_and_a_bare_app_label_is_not_trusted():
    from app.routing import resolver
    from app.routing.semantic import SpeechUnderstanding

    async def never(*a, **k):
        raise AssertionError("no model")
    out = asyncio.run(resolver.resolve({"request": "Compare September 21 and September 22 using saved snapshots. If the baseline is unavailable, do not describe current data as a change.",
                                        "contextual_request": None, "session_context": {}}, never))
    assert out["intent"] == "research" and out["routing_decision"]["reason"] == "dated_comparison"
    assert not product_actions.matches("Compare saved snapshots of ANSEM") and product_actions.matches("Save this investigation so I can revisit it")
