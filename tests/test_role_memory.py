import asyncio
import time
from types import SimpleNamespace

import pytest

from app import role_memory
from app.settings import settings


@pytest.fixture(autouse=True)
def _mem(monkeypatch):
    monkeypatch.setattr(settings, "role_memory_path", None)  # in-process only
    role_memory.reset_for_test()


_ADDR = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def _store(**over):
    kw = dict(role="analysis.reasoning", chain="solana", address=_ADDR, symbol="BONK",
              thesis="Medium-confidence hold.", flip_variable="a large unlock", price_at_decision=1.0)
    kw.update(over)
    return role_memory.store_case(**kw)


def test_recall_empty_until_reflected():
    _store()
    assert role_memory.recall_lessons("solana", _ADDR) == []  # pending, no lesson yet


def test_age_gate_for_reflection():
    cid = _store()
    # Just stored -> too fresh to reflect.
    assert role_memory.due_for_reflection("solana", _ADDR) == []
    # Backdate 25h -> now due.
    role_memory._load()[0].decided_at = time.time() - 25 * 3600
    due = role_memory.due_for_reflection("solana", _ADDR)
    assert [c.id for c in due] == [cid]


def test_record_reflection_then_recall():
    cid = _store()
    role_memory.record_reflection(cid, outcome_pct=-18.0, lesson="Weight upcoming unlocks more heavily.")
    lessons = role_memory.recall_lessons("solana", _ADDR)
    assert lessons == ["Weight upcoming unlocks more heavily."]
    # A different asset does not see this lesson.
    assert role_memory.recall_lessons("ethereum", "0x6982508145454ce325ddbe47a25d4ec3d2311933") == []


def test_mark_stale_retires_old_pending():
    _store()
    role_memory._load()[0].decided_at = time.time() - 800 * 3600  # older than max age
    assert role_memory.mark_stale() == 1
    assert role_memory._load()[0].status == "reflected"


def test_reflect_due_decisions_produces_lesson(monkeypatch):
    from app.nodes import research, runtime
    cid = _store(price_at_decision=1.0)
    role_memory._load()[0].decided_at = time.time() - 30 * 3600  # due

    captured = {}
    async def fake_call_lm(program, **kw):
        captured.update(kw)
        return SimpleNamespace(lesson="Do not treat absent safety flags as safe.")
    monkeypatch.setattr(runtime, "_call_lm", fake_call_lm)

    # price_now = 0.8 -> realized -20%
    asyncio.run(research._reflect_due_decisions("solana", _ADDR, 0.8))
    assert "over" in captured["realized_move"] and "-20" in captured["realized_move"]
    assert role_memory.recall_lessons("solana", _ADDR) == ["Do not treat absent safety flags as safe."]
    # Reflected once -> no longer due.
    assert role_memory.due_for_reflection("solana", _ADDR) == []


def test_store_dedups_recent_pending_for_same_asset():
    a = _store()
    b = _store()  # same asset, minutes apart -> one decision
    assert a == b and len(role_memory._load()) == 1
    # A different asset is its own decision.
    c = _store(address="0x6982508145454ce325ddbe47a25d4ec3d2311933", chain="ethereum", symbol="PEPE")
    assert c != a and len(role_memory._load()) == 2


def test_persistence_round_trips_to_file(monkeypatch, tmp_path):
    path = tmp_path / "rm.json"
    monkeypatch.setattr(settings, "role_memory_path", str(path))
    role_memory.reset_for_test()
    cid = _store()
    role_memory.record_reflection(cid, -5.0, "A durable lesson.")
    role_memory.reset_for_test()  # drop in-process; force reload from disk
    assert role_memory.recall_lessons("solana", _ADDR) == ["A durable lesson."]
