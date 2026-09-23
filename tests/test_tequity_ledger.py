"""The Tequity tick ledger: rows from a snapshot, a tick with one time
stamp, retention, and the reads the later use cases rest on."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import tequity, tequity_ledger
from app.settings import settings

T0 = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)


def _snap(rows, ts=1790159015.689):
    return {"data": {"success": True, "data": {"dex": "hyperliquid", "range": "1d", "tokens": rows, "count": len(rows)}}, "received_at": time.time(), "server_ts": ts}


def _row(sym, price, change, vol, stock=False):
    return {"symbol": sym, "base_asset": sym.replace("USDC", ""), "quote_asset": "USDC", "last_price": price, "price_change_percent": change, "quote_volume": vol, "is_stock": stock}


@pytest.fixture(autouse=True)
def _clean():
    tequity.reset_for_test(); tequity_ledger.reset_for_test()
    yield
    tequity.reset_for_test(); tequity_ledger.reset_for_test()


def test_a_snapshot_becomes_one_row_per_pair_and_trending_keeps_its_rank():
    rows = tequity_ledger.rows_from(tequity.MOVERS["hyperliquid"], _snap([_row("BTCUSDC", 85758.5, -0.16, 2.4e9), _row("CLUSDC", 89.4, -0.35, 2.9e8, True)]), T0)
    assert [(r["venue"], r["symbol"], r["is_stock"], r["rank"], r["taken_at"]) for r in rows] == [("hyperliquid", "BTCUSDC", False, None, T0), ("hyperliquid", "CLUSDC", True, None, T0)]
    assert rows[0]["server_ts"] == datetime.fromtimestamp(1790159015.689, timezone.utc)
    tr = tequity_ledger.rows_from(tequity.TRENDING, {"data": {"trending": [{**_row("BTCUSDC", 1, 0, 1), "dex": "hyperliquid"}, {**_row("AAPLUSDT", 1, 0, 1, True), "dex": "aster"}]}, "received_at": time.time(), "server_ts": None}, T0)
    assert [(r["venue"], r["rank"]) for r in tr] == [("hyperliquid", 1), ("aster", 2)]


def test_a_tick_records_every_fresh_channel_and_reads_back_whole():
    tequity._store(tequity.MOVERS["hyperliquid"], _snap([_row("BTCUSDC", 100.0, 0, 10), _row("CLUSDC", 50.0, 0, 5, True)])["data"], 1790159015.689)
    tequity._store(tequity.MOVERS["aster"], _snap([_row("ASTERUSDT", 0.7, 0, 1)])["data"], None)
    tequity._snapshots[tequity.MOVERS["aster"]]["received_at"] = time.time() - 999            # stale: not recorded
    assert asyncio.run(tequity_ledger.record(T0)) == 2
    assert asyncio.run(tequity_ledger.record(T0 + timedelta(hours=1))) == 2
    tick = asyncio.run(tequity_ledger.tick_at(tequity.MOVERS["hyperliquid"], T0 + timedelta(minutes=30)))
    assert sorted(r["symbol"] for r in tick) == ["BTCUSDC", "CLUSDC"] and {r["taken_at"] for r in tick} == {T0}
    st = asyncio.run(tequity_ledger.status())
    assert st[tequity.MOVERS["hyperliquid"]]["rows"] == 4 and st[tequity.MOVERS["hyperliquid"]]["ticks"] == 2 and tequity.MOVERS["aster"] not in st


def test_movers_between_reads_stored_prices_not_the_feed_figure():
    hl = tequity.MOVERS["hyperliquid"]
    tequity._store(hl, _snap([_row("BTCUSDC", 100.0, 5.0, 10), _row("CLUSDC", 50.0, 5.0, 5, True), _row("NEWUSDC", 1.0, 0, 1)])["data"], None)
    asyncio.run(tequity_ledger.record(T0))
    tequity._store(hl, _snap([_row("BTCUSDC", 90.0, 5.0, 12), _row("CLUSDC", 55.0, 5.0, 6, True), _row("LATEUSDC", 2.0, 0, 1)])["data"], None)
    asyncio.run(tequity_ledger.record(T0 + timedelta(hours=6)))
    out = asyncio.run(tequity_ledger.movers_between("hyperliquid", T0, T0 + timedelta(hours=6)))
    assert out["from"] == T0 and out["pairs"] == 2                                              # pairs present at both ends only
    assert [(r["symbol"], round(r["change_between_pct"], 1)) for r in out["gainers"]] == [("CLUSDC", 10.0), ("BTCUSDC", -10.0)]
    assert [r["symbol"] for r in asyncio.run(tequity_ledger.movers_between("hyperliquid", T0, T0 + timedelta(hours=6), stocks_only=True))["gainers"]] == ["CLUSDC"]
    hist = asyncio.run(tequity_ledger.history("hyperliquid", "BTCUSDC", T0 - timedelta(days=1), T0 + timedelta(days=1)))
    assert [h["last_price"] for h in hist] == [100.0, 90.0]


def test_volume_leaders_and_retention(monkeypatch):
    hl = tequity.MOVERS["hyperliquid"]
    now = tequity_ledger._now()
    for i, (btc, cl) in enumerate([(10, 5), (14, 7), (12, 30)]):
        tequity._store(hl, _snap([_row("BTCUSDC", 100.0, 0, btc), _row("CLUSDC", 50.0, 0, cl, True)])["data"], None)
        asyncio.run(tequity_ledger.record(now - timedelta(days=2) + timedelta(hours=i)))
    leaders = asyncio.run(tequity_ledger.volume_leaders("hyperliquid", 7))
    assert [(r["symbol"], r["ticks"], round(r["mean_volume"], 1)) for r in leaders] == [("CLUSDC", 3, 14.0), ("BTCUSDC", 3, 12.0)]
    assert [r["symbol"] for r in asyncio.run(tequity_ledger.volume_leaders("hyperliquid", 7, stocks_only=True))] == ["CLUSDC"]
    monkeypatch.setattr(settings, "tequity_retention_days", 1)
    assert asyncio.run(tequity_ledger.prune(now)) == 6 and asyncio.run(tequity_ledger.status()) == {}


def test_the_worker_is_off_without_the_feed_or_an_interval(monkeypatch):
    monkeypatch.setattr(settings, "tequity_record_interval_seconds", 0)
    assert not tequity_ledger.enabled()
    asyncio.run(tequity_ledger.worker())                                                        # returns at once
