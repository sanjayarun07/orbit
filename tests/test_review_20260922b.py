"""The second review of 2026-09-22: five findings pinned (the sixth restates a scope decision)."""
import asyncio
from types import SimpleNamespace

import pytest

from app import execution_policy, feedback, mobula_client, polymarket_odds, turn_log
from app.settings import settings


# 2. feedback belongs to the person, not the shared billing account
def test_a_team_member_exports_only_their_own_ratings(monkeypatch):
    async def turn(session_id, revision):
        return {"trajectory": {}}
    monkeypatch.setattr(feedback, "_turn", turn)

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(feedback, "_load", nothing)
    asyncio.run(feedback.rate("s1", 1, "down", "owner's comment", "acct-shared", principal_id="owner"))
    asyncio.run(feedback.rate("s2", 1, "up", "member's comment", "acct-shared", principal_id="member"))
    mine = asyncio.run(feedback.list_for_principal("member"))
    assert [r["comment"] for r in mine] == ["member's comment"]
    assert [r["comment"] for r in asyncio.run(feedback.list_for_principal("owner"))] == ["owner's comment"]
    assert asyncio.run(feedback.scrub_principal("member")) == 1 and asyncio.run(feedback.list_for_principal("member")) == []


# 3. deletion: pending writes drained, late writes scrubbed, a failed scrub stops the deletion
def test_a_log_write_landing_after_deletion_carries_no_words():
    turn_log.forget_user("u-late")
    identity = SimpleNamespace(kind="user", account_id="a", user={"id": "u-late"}, api_key=None, signed_in=True)
    row = asyncio.run(turn_log.record(message="my private question", status="ok", latency_ms=3, identity=identity, session_id="s"))
    assert row["message"] == "[deleted]" and row["user_id"] is None and row["session_id"] is None


def test_a_scrub_the_store_cannot_do_raises_instead_of_returning_zero(monkeypatch):
    class BrokenPool:
        async def execute(self, *a, **k):
            raise RuntimeError("db down")

    async def broken():
        return BrokenPool()
    monkeypatch.setattr(turn_log, "_pool", broken)
    with pytest.raises(RuntimeError):
        asyncio.run(turn_log.scrub_user("u-1"))


def test_deletion_drains_the_background_log_before_scrubbing(monkeypatch):
    order = []

    async def slow_log():
        await asyncio.sleep(0.02)
        order.append("log")

    async def run():
        execution_policy.background(slow_log())
        await execution_policy.drain_background()
        order.append("scrub")
    asyncio.run(run())
    assert order == ["log", "scrub"]


# 4. the Mobula allowance is shared across replicas through Redis
def test_shared_counter_refuses_past_the_deployment_allowance(monkeypatch):
    class FakeRedis:
        """The script's semantics: refuse without counting past the limit."""
        def __init__(self):
            self.counts = {}
        def eval(self, script, numkeys, key, limit):
            n = self.counts.get(key, 0)
            if n >= int(limit):
                return -1
            self.counts[key] = n + 1
            return n + 1
    fake = FakeRedis()
    monkeypatch.setattr(settings, "mobula_shared_limit", True)
    monkeypatch.setattr(settings, "redis_url", "redis://fake")
    monkeypatch.setattr(settings, "mobula_requests_per_minute", 4)
    mobula_client._shared.reset()
    monkeypatch.setattr(mobula_client._shared, "_redis", lambda: fake)
    assert [mobula_client._shared.take(background=True) for _ in range(4)] == [True, True, False, False]   # half for background
    assert list(fake.counts.values()) == [2]                                                               # refusals did not count
    assert [mobula_client._shared.take(background=False) for _ in range(3)] == [True, True, False]        # users still get the rest
    assert list(fake.counts.values()) == [4]


def test_shared_counter_allows_when_redis_is_down(monkeypatch):
    class Dead:
        def eval(self, *a):
            raise ConnectionError("no redis")
    monkeypatch.setattr(settings, "mobula_shared_limit", True)
    monkeypatch.setattr(settings, "redis_url", "redis://fake")
    mobula_client._shared.reset()
    monkeypatch.setattr(mobula_client._shared, "_redis", lambda: Dead())
    assert mobula_client._shared.take(background=False) is True


# 5. the readiness probe is cached both ways
def test_polymarket_probe_caches_success(monkeypatch):
    monkeypatch.setattr(settings, "polymarket_enabled", True)
    calls = []
    monkeypatch.setattr(polymarket_odds, "_search", lambda q, timeout=12.0: calls.append(timeout) or {"events": []})
    assert polymarket_odds.status()["ok"] and polymarket_odds.status()["ok"]
    assert calls == [polymarket_odds.PROBE_TIMEOUT]                                   # one probe, short timeout


# 6. the export returns everything
def test_turn_listing_is_uncapped_for_the_export():
    for i in range(1100):
        asyncio.run(turn_log.record(message=f"m{i}", status="ok", latency_ms=1, identity=SimpleNamespace(kind="user", account_id="a", user={"id": "u-x"}, api_key=None, signed_in=True)))
    assert len(asyncio.run(turn_log.list_turns(days=1, user_id="u-x", limit=None))) == 1100
    assert len(asyncio.run(turn_log.list_turns(days=1, user_id="u-x"))) == 100


# 2 (third review). a thumbs-down comment copied into the flag note goes with the deletion
def test_scrub_clears_the_flag_note_too():
    identity = SimpleNamespace(kind="user", account_id="a", user={"id": "u-fn"}, api_key=None, signed_in=True)
    asyncio.run(turn_log.record(message="q", status="ok", latency_ms=1, identity=identity, session_id="s-fn", revision=2))
    asyncio.run(turn_log.flag_by_revision("s-fn", 2, "user rated down: my private comment"))
    assert "private" in asyncio.run(turn_log.list_turns(days=1))[0]["flag_note"]
    asyncio.run(turn_log.scrub_user("u-fn"))
    row = asyncio.run(turn_log.list_turns(days=1))[0]
    assert row["flag_note"] is None and row["message"] == "[deleted]"


# 1 (third review). the marker is consulted by other stores too
def test_a_receipt_for_a_deleted_account_is_not_kept():
    from app import decision_records
    from app.signals import Subject
    turn_log.forget_user("u-gone")
    out = asyncio.run(decision_records.record(kind="deep_dive", subject=Subject(kind="token", id="X" * 32, chain="solana"), signals=[], verdict="v", user_id="u-gone"))
    assert out is None and asyncio.run(decision_records.list_for("u-gone")) == []


# 3 (third review). the backfill and the ownership-based delete exist in the schema and the scrub
def test_existing_feedback_is_backfilled_from_conversation_ownership():
    from app import db
    assert "UPDATE chat_feedback f SET principal_id = s.user_id::text FROM user_chat_sessions s WHERE f.principal_id IS NULL" in db._PLANS_TABLE_SQL
    import inspect
    assert "session_id IN" in inspect.getsource(feedback.scrub_principal)
