"""Review of 07190a22: alerts need coverage, cooldowns commit after delivery,
history's live line comes from the validated snapshot, leadership fails closed
when Redis is configured, "24 days" is days, constraints travel with a carried
ticker, and the SQL stock ranking filters before it limits."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import composition, tasks, tequity, tequity_ledger
from app.settings import settings

NOW = datetime.now(timezone.utc)
FIXED = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _row(sym, price, change=0.0, vol=1e6, stock=False):
    return {"symbol": sym, "base_asset": sym.replace("USDC", ""), "quote_asset": "USDC", "last_price": price, "price_change_percent": change, "quote_volume": vol, "is_stock": stock}


def _snap(rows):
    return {"success": True, "data": {"dex": "hyperliquid", "range": "1d", "tokens": rows, "count": len(rows)}}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    tequity.reset_for_test(); tequity_ledger.reset_for_test(); tasks.reset()
    monkeypatch.setattr(settings, "tequity_record_interval_seconds", 300)
    yield
    tequity.reset_for_test(); tequity_ledger.reset_for_test(); tasks.reset()


def _record(rows, when):
    tequity._store(tequity.MOVERS["hyperliquid"], _snap(rows), None)
    asyncio.run(tequity_ledger.record(when))


def test_a_one_hour_alert_needs_a_baseline_near_the_window_start():
    _record([_row("TSLAUSDC", 100.0, stock=True)], NOW - timedelta(hours=5))
    _record([_row("TSLAUSDC", 120.0, stock=True)], NOW - timedelta(minutes=30))
    _record([_row("TSLAUSDC", 120.0, stock=True)], NOW - timedelta(minutes=2))
    spec = tasks.validate_spec("movers_alert", {"venue": "hyperliquid", "threshold_pct": 10, "window_minutes": 60, "stocks_only": True})
    task = asyncio.run(tasks.create_task({"id": "u1"}, "movers_alert", spec, {"every_minutes": 5}))
    fire, text, _ = asyncio.run(tasks.evaluate(task))
    assert not fire and text.startswith("skipped: insufficient coverage (baseline tick") and "limit 10 min" in text
    # with a baseline close to the window start the same move fires
    _record([_row("TSLAUSDC", 100.0, stock=True)], NOW - timedelta(minutes=62))
    fire, text, _ = asyncio.run(tasks.evaluate(task))
    assert fire and "TSLA +20.0%" in text


def test_the_cooldown_commits_only_after_the_inbox_row_exists(monkeypatch):
    _record([_row("TSLAUSDC", 100.0, stock=True)], NOW - timedelta(minutes=61))
    _record([_row("TSLAUSDC", 120.0, stock=True)], NOW - timedelta(minutes=1))
    spec = tasks.validate_spec("movers_alert", {"venue": "hyperliquid", "threshold_pct": 10, "window_minutes": 60, "stocks_only": True})
    task = asyncio.run(tasks.create_task({"id": "u1", "email": "u@example.com"}, "movers_alert", spec, {"every_minutes": 5}))
    attempts = []

    async def failing_notify(*a, **k):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("inbox write failed")
        return {"id": "n1"}, True
    monkeypatch.setattr(tasks, "notify", failing_notify)
    monkeypatch.setattr(settings, "task_retry_limit", 5)
    claimed = {**asyncio.run(tasks.get_task(task["id"])), "occurrence": "occ-1"}
    with pytest.raises(RuntimeError):
        asyncio.run(tasks._run_claimed(claimed, {"id": "u1", "email": "u@example.com"}))
    assert asyncio.run(tasks.get_task(task["id"]))["spec"].get("last_fired", {}) == {}      # nothing delivered: nothing suppressed
    out = asyncio.run(tasks._run_claimed({**asyncio.run(tasks.get_task(task["id"])), "occurrence": "occ-1"}, {"id": "u1", "email": "u@example.com"}))
    assert out["fired"] is True and len(attempts) == 2
    assert "TSLAUSDC" in asyncio.run(tasks.get_task(task["id"]))["spec"]["last_fired"]


def test_history_shows_no_feed_now_line_from_a_stale_snapshot():
    _record([_row("TSLAUSDC", 100.0, stock=True)], NOW - timedelta(hours=2))
    _record([_row("TSLAUSDC", 110.0, stock=True)], NOW - timedelta(minutes=5))
    tequity._snapshots[tequity.MOVERS["hyperliquid"]]["received_at"] = time.time() - 86400
    card = tequity.history("how has TSLA moved on hyperliquid today")
    assert "| Change between those ticks | +10.00% |" in card and "Feed now" not in card


def test_leadership_fails_closed_when_redis_is_configured_but_broken(monkeypatch):
    from app import db

    async def boom():
        raise RuntimeError("Redis is unavailable and memory fallback is disabled")
    monkeypatch.setattr(db, "get_redis", boom)
    monkeypatch.setattr(settings, "redis_url", "redis://configured")
    assert asyncio.run(tequity_ledger.is_leader()) is False

    async def none():
        return None
    monkeypatch.setattr(db, "get_redis", none)
    assert asyncio.run(tequity_ledger.is_leader()) is False
    monkeypatch.setattr(settings, "redis_url", None)
    assert asyncio.run(tequity_ledger.is_leader()) is True


def test_twenty_four_days_is_days():
    assert tequity.period_start("movers on hyperliquid in the last 24 days", FIXED) == FIXED - timedelta(days=24)
    assert tequity.period_start("movers on hyperliquid in the last 24 hours", FIXED) == FIXED - timedelta(hours=24)
    assert tequity.period_start("movers on hyperliquid last 24h", FIXED) == FIXED - timedelta(hours=24)
    assert tequity.period_start("how has TSLA moved on hyperliquid since September 24", FIXED) == datetime(2025, 9, 24, tzinfo=timezone.utc)


def test_constraints_travel_with_a_carried_ticker():
    clauses, _ = composition.plan_asks("Find BONK pools on Solana with at least $1000000 liquidity. Explain how yields change.")
    assert clauses[1] == "Explain how yields change (context: Find BONK pools on Solana with at least $1000000 liquidity)"


def test_sql_stock_ranking_filters_before_it_limits(monkeypatch):
    captured = {}

    class Pool:
        async def fetch(self, sql, *args):
            captured["sql"], captured["args"] = sql, args
            return []
    async def pool():
        return Pool()
    monkeypatch.setattr(tequity_ledger, "_pool", pool)
    asyncio.run(tequity_ledger.volume_leaders("hyperliquid", 7, stocks_only=True, limit=15))
    assert "($4::bool = FALSE OR is_stock)" in captured["sql"] and "GROUP BY symbol" in captured["sql"]
    assert captured["args"][2] == 15 and captured["args"][3] is True
    assert captured["sql"].index("is_stock)") < captured["sql"].index("LIMIT")
