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
        def acquire(self):
            pool = self
            class _Acq:
                async def __aenter__(self):
                    return pool
                async def __aexit__(self, *a):
                    return False
            return _Acq()
        def transaction(self):
            class _Tx:
                async def __aenter__(self):
                    pass
                async def __aexit__(self, *a):
                    return False
            return _Tx()

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
def test_existing_feedback_is_backfilled_with_the_app_principal_id():
    from app import db
    assert "SET principal_id = 'user:' || s.user_id::text FROM user_chat_sessions s WHERE f.principal_id IS NULL" in db._PLANS_TABLE_SQL
    assert "SET principal_id = 'user:' || principal_id WHERE principal_id ~" in db._PLANS_TABLE_SQL     # rows migrated bare are repaired


def test_deletion_removes_ratings_on_the_conversations_the_person_owned(monkeypatch):
    async def turn(session_id, revision):
        return {"trajectory": {}}
    monkeypatch.setattr(feedback, "_turn", turn)

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(feedback, "_load", nothing)
    asyncio.run(feedback.rate("old-session", 1, "down", "from before ownership existed", "acct", principal_id=None))
    asyncio.run(feedback.rate("other", 1, "up", "someone else", "acct", principal_id="user:other"))
    assert asyncio.run(feedback.scrub_principal("user:me", ["old-session"])) == 1
    assert [v["comment"] for v in feedback._memory.values()] == ["someone else"]


# 1 (fourth review). the insert and the scrub take the same per-user lock inside a transaction
class _Conn:
    def __init__(self, log):
        self.log = log
    async def execute(self, sql, *args):
        self.log.append(sql.split("(")[0].strip()[:40])
        return "UPDATE 1"
    def transaction(self):
        log = self.log
        class _Tx:
            async def __aenter__(self):
                log.append("BEGIN")
            async def __aexit__(self, *a):
                log.append("COMMIT")
        return _Tx()


class _Pool:
    def __init__(self):
        self.log = []
    def acquire(self):
        conn = _Conn(self.log)
        class _Acq:
            async def __aenter__(self):
                return conn
            async def __aexit__(self, *a):
                return False
        return _Acq()
    async def execute(self, sql, *args):
        self.log.append("UNLOCKED " + sql.split("(")[0].strip()[:30])
        return "UPDATE 1"


def test_the_log_insert_for_a_user_runs_locked_in_a_transaction():
    pool = _Pool()
    row = turn_log.build(message="m", status="ok", latency_ms=1, identity=SimpleNamespace(kind="user", account_id="a", user={"id": "u-1"}, api_key=None, signed_in=True))
    asyncio.run(turn_log._insert(pool, row))
    assert pool.log == ["BEGIN", "SELECT pg_advisory_xact_lock", "INSERT INTO chat_turns", "COMMIT"]
    anon = turn_log.build(message="m", status="ok", latency_ms=1)
    pool.log.clear()
    asyncio.run(turn_log._insert(pool, anon))
    assert pool.log == ["UNLOCKED INSERT INTO chat_turns"]                        # no owner, nothing to serialise with


def test_the_scrub_takes_the_same_lock_then_marks_then_scrubs():
    pool = _Pool()
    assert asyncio.run(turn_log._scrub(pool, "u-1")) == 1
    assert pool.log == ["BEGIN", "SELECT pg_advisory_xact_lock", "INSERT INTO deleted_users", "UPDATE chat_turns SET message = '[deleted]'"[:40], "COMMIT"]


# 5 (fifth review). a failed write keeps no private content in memory when Postgres is the store,
# and every scrub sweeps this process's memory as well as the database
class _FailingPool:
    async def execute(self, *a, **k):
        raise RuntimeError("db down")
    async def fetch(self, *a, **k):
        return []
    def acquire(self):
        pool = self
        class _Acq:
            async def __aenter__(self):
                return pool
            async def __aexit__(self, *a):
                return False
        return _Acq()
    def transaction(self):
        class _Tx:
            async def __aenter__(self):
                pass
            async def __aexit__(self, *a):
                return False
        return _Tx()


def _configured(monkeypatch, module, pool):
    monkeypatch.setattr(settings, "database_url", "postgresql://db/orbit")
    async def get():
        return pool
    monkeypatch.setattr(module, "_pool" if hasattr(module, "_pool") else "get_pg_pool", get)


def test_a_turn_that_could_not_be_persisted_keeps_no_words_in_memory(monkeypatch):
    _configured(monkeypatch, turn_log, _FailingPool())
    identity = SimpleNamespace(kind="user", account_id="a", user={"id": "u-out"}, api_key=None, signed_in=True)
    row = asyncio.run(turn_log.record(message="my private question", status="ok", latency_ms=7, identity=identity, session_id="s"))
    assert row["message"] == "[not persisted]" and row["user_id"] is None and row["session_id"] is None and row["latency_ms"] == 7
    assert turn_log._memory[0]["message"] == "[not persisted]"


def test_the_scrub_sweeps_memory_copies_even_when_the_database_answers(monkeypatch):
    identity = SimpleNamespace(kind="user", account_id="a", user={"id": "u-mem"}, api_key=None, signed_in=True)
    asyncio.run(turn_log.record(message="kept while memory was the store", status="ok", latency_ms=1, identity=identity))   # memory-only mode
    assert turn_log._memory[0]["message"].startswith("kept")
    _configured(monkeypatch, turn_log, _Pool())                                              # the database is back
    assert asyncio.run(turn_log.scrub_user("u-mem")) == 2                                    # one database row (fake) + one memory row
    assert turn_log._memory[0]["message"] == "[deleted]" and turn_log._memory[0]["user_id"] is None


def test_a_receipt_that_could_not_be_persisted_belongs_to_nobody(monkeypatch):
    from app import decision_records
    from app.signals import Subject
    _configured(monkeypatch, decision_records, _FailingPool())
    row = asyncio.run(decision_records.record(kind="deep_dive", subject=Subject(kind="token", id="Y" * 32, chain="solana"), signals=[], verdict="v", user_id="u-r", session_id="s"))
    assert row["user_id"] is None and row["session_id"] is None
    monkeypatch.setattr(settings, "database_url", None)
    asyncio.run(decision_records.record(kind="deep_dive", subject=Subject(kind="token", id="Y" * 32, chain="solana"), signals=[], verdict="v", user_id="u-r"))
    assert decision_records.forget_user("u-r") == 1 and asyncio.run(decision_records.list_for("u-r")) == []


def test_a_rating_saved_while_the_database_was_down_carries_no_comment_or_owner(monkeypatch):
    async def turn(session_id, revision):
        return {"trajectory": {}}
    monkeypatch.setattr(feedback, "_turn", turn)
    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(feedback, "_load", nothing)
    monkeypatch.setattr(settings, "database_url", "postgresql://db/orbit")               # configured, pool None = down
    asyncio.run(feedback.rate("s-down", 1, "down", "my private comment", "acct", principal_id="user:me"))
    saved = feedback._memory[("s-down", 1)]
    assert saved["rating"] == "down" and saved["comment"] is None and saved["principal_id"] is None
    _configured(monkeypatch, feedback, _Pool())                                          # the database is back at deletion time
    assert asyncio.run(feedback.scrub_principal("user:me", ["s-down"])) == 2 and ("s-down", 1) not in feedback._memory


def test_a_fact_is_not_remembered_in_memory_while_the_store_is_down(monkeypatch):
    from app import user_memory
    monkeypatch.setattr(settings, "database_url", "postgresql://db/orbit")
    asyncio.run(user_memory._insert("u-f", {"id": "f1", "fact": "holds 3 SOL", "kind": "holding", "confidence": 0.9, "embedding": [0.0]}))
    assert asyncio.run(user_memory.list_facts("u-f")) == []
    monkeypatch.setattr(settings, "database_url", None)
    asyncio.run(user_memory._insert("u-f", {"id": "f2", "fact": "holds 3 SOL", "kind": "holding", "confidence": 0.9, "embedding": [0.0]}))
    _configured(monkeypatch, user_memory, _Pool())                                       # the database is back at deletion time
    assert asyncio.run(user_memory.clear("u-f")) == 2                                    # one database row (fake) + the memory copy
    assert all(f.get("deleted_at") for f in user_memory._facts["u-f"])
