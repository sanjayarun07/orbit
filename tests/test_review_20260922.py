"""The 2026-09-22 review: ten findings, each pinned."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import exchange_listings, holder_snapshots as hs, mobula_client, polymarket_odds, turn_log
from app.nodes import research
from app.settings import settings
from app.signals import Subject

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
_BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def _row(taken_at, i):
    return {"id": f"r{i}", "taken_at": taken_at, "top10_pct": float(i), "top50_pct": None, "holders_count": None, "dev_pct": None, "sniper_pct": None,
            "bundler_pct": None, "insider_pct": None, "lp_burned_pct": None, "lp_locked_pct": None, "lp_unlocked_pct": None, "price_usd": None,
            "liquidity_usd": None, "market_cap_usd": None, "top_holders": [], "flags": {"missing": [], "failed": []}}


# 1. history is the newest rows
def test_history_returns_the_newest_rows_oldest_first():
    s = Subject(kind="token", id=_BONK, chain="solana", symbol="BONK")
    for i in range(300):
        asyncio.run(hs.store(s.key, _row(NOW - timedelta(hours=300 - i), i)))
    rows = asyncio.run(hs.history(s.key, limit=200))
    assert len(rows) == 200 and rows[0]["id"] == "r100" and rows[-1]["id"] == "r299"       # the newest 200, ascending
    assert asyncio.run(hs.history(s.key, limit=2))[-1]["id"] == "r299"


# 2. compound clauses do not share a sink
def test_compound_clauses_keep_their_own_resolution_and_the_first_names_the_turn(monkeypatch):
    async def inner(state, sink):
        clause = state["request"]
        sym = "BONK" if "BONK" in clause else "WIF"
        sink["resolved_token"] = {"symbol": sym, "address": sym * 8, "chain": "solana"}
        sink["resolution_note"] = f"_Read **{sym}**._"
        await asyncio.sleep(0.02 if sym == "BONK" else 0.0)                                  # WIF finishes first
        return {"answer": f"# {sym}\nprice", "trajectory": {"tool_name_0": "birdeye_token_overview"}}

    async def synth(request, cards, trajectory, advice=False):
        return cards
    monkeypatch.setattr(research, "_research_node", inner)
    monkeypatch.setattr(research, "_web_context_part", lambda state: None)
    monkeypatch.setattr(research.composition, "synthesize", synth)
    out = asyncio.run(research.research_node({"request": "price of BONK. price of WIF", "capabilities": ["market_data"], "chains": [], "session_context": {}}))
    assert out["resolved_token"]["symbol"] == "BONK"                                          # the first clause, not the first to finish
    assert out["answer"].startswith("_Read **BONK**._\n_Read **WIF**._")                      # both notes, in clause order


# 3. the first snapshot is off the critical path
def test_deep_dive_schedules_its_snapshot_instead_of_waiting(monkeypatch):
    calls = []

    async def slow_track(subject, source, *, days, launched_at=None):
        calls.append("track")
        await asyncio.sleep(0.05)

    async def slow_snapshot(subject):
        calls.append("snapshot")
        await asyncio.sleep(0.05)
    monkeypatch.setattr(hs, "track", slow_track)
    monkeypatch.setattr(hs, "snapshot", slow_snapshot)

    async def run():
        hs.schedule(Subject(kind="token", id=_BONK, chain="solana", symbol="BONK"), "deep_dive", 14, take_now=True)
        assert calls == []                                                                     # nothing ran inline
        await hs.drain_background()
        assert calls == ["track", "snapshot"]
    asyncio.run(run())


# 4. the turn log never holds the response
def test_a_hung_store_does_not_hold_the_turn(monkeypatch):
    class HungPool:
        async def execute(self, *a, **k):
            await asyncio.sleep(60)

    async def hung_pool():
        return HungPool()
    monkeypatch.setattr(turn_log, "_pool", hung_pool)
    monkeypatch.setattr(turn_log, "STORE_TIMEOUT_SECONDS", 0.05)

    async def no_pool():
        return None

    async def run():
        started = asyncio.get_event_loop().time()
        row = await turn_log.record(message="x", status="ok", latency_ms=1)
        assert asyncio.get_event_loop().time() - started < 1.0 and row is not None
        monkeypatch.setattr(turn_log, "_pool", no_pool)
        assert (await turn_log.list_turns(days=1))[0]["message"] == "x"                       # kept in memory
    asyncio.run(run())


# 5. the Mobula allowance is shared by the workers
def test_the_mobula_bucket_is_a_share_of_the_workers(monkeypatch):
    monkeypatch.setattr(settings, "mobula_requests_per_minute", 60)
    monkeypatch.setattr(settings, "mobula_burst", 60)
    monkeypatch.setattr(settings, "uvicorn_workers", 2)
    assert mobula_client._rate() == 30.0 and mobula_client._capacity() == 30.0
    monkeypatch.setattr(settings, "uvicorn_workers", 1)
    assert mobula_client._rate() == 60.0 and mobula_client._capacity() == 60.0


# 7. a lowercase Binance question
def test_binance_questions_name_the_ticker_in_any_case():
    assert exchange_listings.listing_symbol("is bonk listed on binance") == "BONK"
    assert exchange_listings.listing_symbol("is BONK listed on Binance") == "BONK"
    assert exchange_listings.listing_symbol("which binance pairs trade $wif") == "WIF"
    assert exchange_listings.listing_symbol("does binance list pepe") == "PEPE"
    assert exchange_listings.listing_symbol("what is listed on binance") is None


# 8. Polymarket takes itself out when unreachable and says so in readiness
def test_polymarket_marks_itself_down_on_a_transport_failure(monkeypatch):
    monkeypatch.setattr(settings, "polymarket_enabled", True)
    import httpx

    class Client:
        def __init__(self, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, *a, **k):
            raise httpx.ConnectError("nodename nor servname provided")
    monkeypatch.setattr(polymarket_odds.httpx, "Client", Client)
    assert polymarket_odds.enabled()
    with pytest.raises(httpx.ConnectError):
        polymarket_odds.markets_for("BTC")
    assert not polymarket_odds.enabled()
    st = polymarket_odds.status()
    assert st["ok"] is False and "unreachable" in st["detail"]


# 9. deletion scrubs the person's words from the turn log
def test_scrub_user_removes_words_and_owner_but_keeps_the_row():
    asyncio.run(turn_log.record(message="my seed is safe", status="ok", latency_ms=5, identity=SimpleNamespace(kind="user", account_id="a1", user={"id": "u-9"}, api_key=None, signed_in=True),
                                session_id="s1", response=None))
    assert asyncio.run(turn_log.scrub_user("u-9")) == 1
    row = asyncio.run(turn_log.list_turns(days=1))[0]
    assert row["message"] == "[deleted]" and row["answer"] is None and row["user_id"] is None and row["status"] == "ok"
    assert asyncio.run(turn_log.list_turns(days=1, user_id="u-9")) == []


# 10. the memory summary counts every row in the period, not a 1,000 sample
def test_summary_counts_the_whole_period():
    for i in range(1200):
        asyncio.run(turn_log.record(message=f"m{i}", status="error" if i % 4 == 0 else "ok", http_status=504 if i % 4 == 0 else 200, latency_ms=i))
    s = asyncio.run(turn_log.summary(days=1))
    assert s["turns"] == 1200 and s["errors"] == 300 and s["errors_by_status"] == {"504": 300}
