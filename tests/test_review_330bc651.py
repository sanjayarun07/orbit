"""Review of 330bc651 and the recheck of 2910d5e8: stale snapshots are not
current, the recorder never records without proof of leadership and never
twice per bucket, reminders keep a negation, a lowercase name switches the
subject, and a compound ask keeps its note, constraints and instructions."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import composition, tasks_nl, tequity, tequity_ledger
from app.context_entities import resolve_contextual_request
from app.routing import subject_probe
from app.settings import settings


@pytest.fixture(autouse=True)
def _clean():
    tequity.reset_for_test(); tequity_ledger.reset_for_test()
    yield
    tequity.reset_for_test(); tequity_ledger.reset_for_test()


def test_a_stale_snapshot_is_unavailable_after_a_failed_refresh(monkeypatch):
    hl = tequity.MOVERS["hyperliquid"]
    tequity._store(hl, {"success": True, "data": {"dex": "hyperliquid", "tokens": [{"symbol": "BTCUSDC", "last_price": 1, "price_change_percent": 0, "quote_volume": 1}]}}, None)
    tequity._snapshots[hl]["received_at"] = time.time() - 86400
    assert asyncio.run(tequity.snapshot(hl)) is None                                    # the stub refresh brought nothing
    with pytest.raises(RuntimeError, match="no current hyperliquid movers snapshot; the last snapshot arrived 1440 min ago"):
        tequity.movers("top gainers on hyperliquid")
    assert asyncio.run(tequity_ledger.record()) == 0                                     # and it is not recorded either


def test_the_recorder_skips_a_bucket_already_recorded_and_a_redis_it_cannot_reach(monkeypatch):
    hl = tequity.MOVERS["hyperliquid"]
    tequity._store(hl, {"success": True, "data": {"dex": "hyperliquid", "tokens": [{"symbol": "BTCUSDC", "last_price": 1, "price_change_percent": 0, "quote_volume": 1}]}}, None)
    now = datetime.now(timezone.utc)
    assert asyncio.run(tequity_ledger.record(now)) == 1
    assert asyncio.run(tequity_ledger.record(now + timedelta(seconds=30))) == 0          # inside the same bucket: a second recorder's tick is dropped
    assert asyncio.run(tequity_ledger.record(now + timedelta(seconds=200))) == 1

    class Broken:
        async def set(self, *a, **k): raise ConnectionError("redis down")
    async def broken():
        return Broken()
    from app import db
    monkeypatch.setattr(db, "get_redis", broken)
    assert asyncio.run(tequity_ledger.is_leader()) is False


def test_reminder_channel_keeps_negations_and_ordinary_words():
    f = tasks_nl._reminder_fields
    assert f("in 3 minutes to review research. Do not email me.") == ("in 3 minutes to review research", None, "inapp")
    assert f("in 3 minutes to email Alice") == ("in 3 minutes to email Alice", None, "inapp")
    assert f("in 3 minutes to check my inbox") == ("in 3 minutes to check my inbox", None, "inapp")
    assert f("in 3 minutes to review my email security") == ("in 3 minutes to review my email security", None, "inapp")
    assert f("tomorrow at 9am to check SOL by email") == ("tomorrow at 9am to check SOL", None, "email")
    assert f("in 3 minutes to review my research. Name it Persona E2E disposable reminder. Use the in-app inbox only.") == ("in 3 minutes to review my research", "Persona E2E disposable reminder", "inapp")


def test_a_lowercase_name_is_a_subject_switch():
    assert subject_probe.has_own_subject("What is the price of pepe?") and subject_probe.has_own_subject("is bonk safe") and subject_probe.has_own_subject("wif holders")
    assert not subject_probe.has_own_subject("Were early buys bundled or sniped?") and not subject_probe.has_own_subject("what is the price now")
    focus = {"focus": {"kind": "token", "label": "ANSEM", "address": "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", "chain": "solana"}}
    assert resolve_contextual_request("What is the price of pepe?", "user: ANSEM", focus) == "What is the price of pepe?"


def test_compound_asks_keep_their_note_constraints_and_instructions():
    clauses, note = composition.plan_asks("Find USDC yield on Base without exposing me to another volatile token. Explain how the yield can change.")
    assert clauses[1] == "Explain how the yield can change (context: Find USDC yield on Base without exposing me to another volatile token)"
    req = ("List funding rounds by date, amount, instrument, investors and primary source. Mark database-only claims.\n"
           "Resolved from conversation context: this continues the discussion about EigenLayer (a protocol).")
    clauses, note = composition.plan_asks(req)
    assert len(clauses) == 1 and clauses[0].startswith("List funding rounds") and "Resolved from conversation context" in clauses[0]
    assert "Mark database-only claims" in note and not any(c.startswith("Resolved") or c.startswith("Mark") for c in clauses)
    clauses, note = composition.plan_asks("What changed in ANSEM holders between September 21 and September 22? Compare saved snapshots. If snapshots are unavailable, say so.")
    assert clauses == ["What changed in ANSEM holders between September 21 and September 22",
                       "Compare saved snapshots (context: What changed in ANSEM holders between September 21 and September 22)"] and "say so" in note
    assert composition.plan_asks("price and volume of BONK") == (["price and volume of BONK"], "")
