"""The use cases on top of the Tequity ledger: a pair's stored path over a
period, movers over a period, volume leaders, venue price alerts, movers
alerts by chat, and the brief's venue lines. No network."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import tasks, tasks_nl, tequity, tequity_ledger
from app.settings import settings

NOW = datetime.now(timezone.utc)


def _row(sym, base, price, change, vol, stock=False):
    return {"symbol": sym, "base_asset": base, "quote_asset": "USDC", "last_price": price, "price_change_percent": change, "quote_volume": vol, "is_stock": stock}


def _snap(rows):
    return {"success": True, "data": {"dex": "hyperliquid", "range": "1d", "tokens": rows, "count": len(rows)}, "timestamp": int(time.time() * 1000)}


@pytest.fixture(autouse=True)
def _feed(monkeypatch):
    tequity.reset_for_test(); tequity_ledger.reset_for_test(); tasks.reset()
    hl = tequity.MOVERS["hyperliquid"]
    tequity._store(hl, _snap([_row("TSLAUSDC", "TSLA", 400.0, 1.0, 5e6, True), _row("BTCUSDC", "BTC", 90000.0, -1.0, 2e9)]), time.time())
    asyncio.run(tequity_ledger.record(NOW - timedelta(hours=5)))
    tequity._store(hl, _snap([_row("TSLAUSDC", "TSLA", 440.0, 1.0, 6e6, True), _row("BTCUSDC", "BTC", 85500.0, -1.0, 2.1e9)]), time.time())
    asyncio.run(tequity_ledger.record(NOW - timedelta(minutes=30)))
    tequity._store(hl, _snap([_row("TSLAUSDC", "TSLA", 452.0, 2.5, 7e6, True), _row("BTCUSDC", "BTC", 85000.0, -1.2, 2.2e9)]), time.time())
    asyncio.run(tequity_ledger.record(NOW - timedelta(minutes=2)))
    yield
    tequity.reset_for_test(); tequity_ledger.reset_for_test(); tasks.reset()


def test_periods_and_bases_are_read_from_the_ask():
    assert tequity.period_start("how has TSLA moved on hyperliquid this week") <= NOW - timedelta(days=6, hours=23)
    assert tequity.period_start("movers on aster since this morning").hour == 0
    assert tequity.period_start("biggest losers on hl in the last 6 hours") >= NOW - timedelta(hours=6, minutes=1)
    assert tequity.period_start("top gainers on Aster today") is not None and tequity.period_start("top gainers on Aster") is None
    assert tequity.base_in("how has $tsla moved on hyperliquid this week") == "TSLA" and tequity.base_in("how has TSLA moved on hyperliquid this week") == "TSLA"
    assert tequity.base_in("movers on hyperliquid this week") is None and tequity.resolve_pair("hyperliquid", "tsla") == "TSLAUSDC"


def test_history_reads_stored_prices_and_says_where_the_ledger_begins():
    card = tequity.history("how has TSLA moved on hyperliquid this week")
    assert card.startswith("# TSLA on Hyperliquid since") and "| Change between those ticks | +13.00% |" in card and "3 ticks | $452.00 / $400.00 |" in card
    assert "later than the period asked for; the change is measured from there" in card and "Feed now: $452.00, +2.50%" in card
    assert tequity.history_matches("how has TSLA moved on hyperliquid this week") and not tequity.history_matches("how has TSLA moved this week")
    assert not tequity.history_matches("my TSLA position on hyperliquid this week")


def test_period_movers_and_volume_leaders_from_the_ledger():
    assert tequity.period_movers_matches("biggest movers on hyperliquid in the last 6 hours") and not tequity.period_movers_matches("top gainers on hyperliquid")
    card = tequity.period_movers("biggest movers on hyperliquid in the last 6 hours")
    assert "| 1 | TSLA/USDC | stock | $400.00 | $452.00 | +13.00% |" in card and "| 2 | BTC/USDC | crypto |" in card and "between two stored ticks" in card
    card = tequity.period_movers("tokenized stocks down the most on hyperliquid today")
    assert "Tokenized stocks down the most" in card and "TSLA" in card and "BTC" not in card
    assert tequity.volume_leaders_matches("most traded on hyperliquid this week")
    card = tequity.volume_leaders("most traded on hyperliquid this week")
    assert "| 1 | BTCUSDC | crypto |" in card and "| 2 | TSLAUSDC | stock |" in card


def test_venue_price_alerts_read_the_feed_and_are_parsed_by_chat():
    assert asyncio.run(tasks.price_for("TSLA", "hyperliquid")) == 452.0 and asyncio.run(tasks.price_for("NOPE", "hyperliquid")) is None
    m = tasks_nl._ALERT.match("alert me when TSLA on hyperliquid drops below 400")
    assert m and m.group("venue") == "hyperliquid" and m.group("sym") == "TSLA"
    spec = tasks.validate_spec("price_alert", {"symbol": "TSLA", "op": "<", "price": 400, "venue": "Hyperliquid"})
    assert spec["venue"] == "hyperliquid" and tasks._default_title("price_alert", spec) == "TSLA < $400 on hyperliquid"
    with pytest.raises(ValueError):
        tasks.validate_spec("price_alert", {"symbol": "TSLA", "op": "<", "price": 400, "venue": "binance"})
    fire, text, body = asyncio.run(tasks.evaluate({"kind": "price_alert", "spec": spec, "title": "t"}))
    assert not fire and "452" in text
    spec = tasks.validate_spec("price_alert", {"symbol": "TSLA", "op": ">", "price": 400, "venue": "hyperliquid"})
    assert asyncio.run(tasks.evaluate({"kind": "price_alert", "spec": spec, "title": "t"}))[0] is True


def test_movers_alert_fires_once_per_pair_per_window():
    spec = tasks.validate_spec("movers_alert", {"venue": "hyperliquid", "threshold_pct": 2, "window_minutes": 60, "stocks_only": True})
    assert spec["repeat"] is True and tasks._default_title("movers_alert", spec) == "Stocks moving over 2% on hyperliquid within 60 min"
    task = asyncio.run(tasks.create_task({"id": "u1", "email": "u@example.com"}, "movers_alert", spec, {"every_minutes": 5}))
    fire, text, body = asyncio.run(tasks.evaluate(task))
    assert fire and text.startswith("1 tokenized stock moved over 2% on hyperliquid within 60 min: TSLA +13.0% ($400 → $452)") and "not a recommendation" in body
    task = asyncio.run(tasks.get_task(task["id"]))
    assert "TSLAUSDC" in task["spec"]["last_fired"]
    fire, text, _ = asyncio.run(tasks.evaluate(task))                                   # same move inside the window: nothing new
    assert not fire and text == "1 over threshold, none new"
    with pytest.raises(ValueError):
        tasks.validate_spec("movers_alert", {"venue": "hyperliquid", "threshold_pct": 0})


def test_movers_alert_by_chat_needs_a_venue_and_reads_the_window():
    assert tasks_nl.is_task_control("tell me when any tokenized stock moves more than 10% on hyperliquid within an hour")
    reply = asyncio.run(tasks_nl.handle("tell me when any tokenized stock moves more than 10% on hyperliquid within an hour", {"id": "u2", "email": "u2@example.com"}))
    assert reply.startswith("Movers alert set: any tokenized stock on hyperliquid that moves more than 10% within 60 minutes")
    items = asyncio.run(tasks.list_tasks("u2", include_done=False))
    assert items[0]["kind"] == "movers_alert" and items[0]["spec"]["window_minutes"] == 60 and items[0]["spec"]["stocks_only"] is True
    reply = asyncio.run(tasks_nl.handle("alert me when any pair moves more than 5% in the last 30 minutes", {"id": "u2", "email": "u2@example.com"}))
    assert reply.startswith("Which venue: Aster or Hyperliquid?")


def test_the_brief_carries_a_venue_section():
    lines = asyncio.run(tasks._venue_brief_lines())
    assert lines[1] == "**Perp venues (24h, company feed)**" and lines[2].startswith("- **Hyperliquid**: TSLA +2.5%, BTC -1.2%; stocks: TSLA +2.5% · tokenized stocks 0% of 24h quote volume")
