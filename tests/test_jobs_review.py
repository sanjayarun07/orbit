"""The review of 2026-09-22 on the job engine: seven findings, each pinned."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import jobs, sessions
from app.integrations import tradingview
from app.settings import settings


@pytest.fixture(autouse=True)
def _kinds():
    jobs.reset_for_test()
    saved = dict(jobs._handlers)
    yield
    jobs._handlers.clear()
    jobs._handlers.update(saved)
    jobs.reset_for_test()


# 1. a cancelled request detaches; an expired attachment is delivered anyway
def test_a_cancelled_attach_detaches_the_job(monkeypatch):
    async def handler(job, ctx):
        return {"answer": "x"}
    jobs.register("t", handler)
    monkeypatch.setattr(jobs, "_worker_running", True)
    job = asyncio.run(jobs.create("t", {}, user_id="u1"))

    async def scenario():
        task = asyncio.create_task(jobs.attach(job["id"], timeout=30))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await jobs.get(job["id"])
    assert asyncio.run(scenario())["attached"] is False


def test_a_settled_job_whose_request_vanished_is_still_delivered(monkeypatch):
    appended = []

    async def append_turn(session_id, role, content, metadata=None):
        appended.append(content)
    monkeypatch.setattr(sessions, "append_turn", append_turn)

    async def get_messages(session_id):
        return []
    monkeypatch.setattr(sessions, "get_messages", get_messages)

    async def handler(job, ctx):
        return {"answer": "late answer"}
    jobs.register("t", handler)
    job = asyncio.run(jobs.create("t", {}, user_id="u1", session_id="s"))
    asyncio.run(jobs.run(job))                                                     # attached=True: not delivered at settle
    assert appended == []
    with jobs._lock:
        jobs._memory[job["id"]]["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=settings.job_attach_seconds + 60)).isoformat()
    asyncio.run(jobs.maintain())                                                   # the attachment cannot still exist
    assert appended == ["late answer"] and asyncio.run(jobs.get(job["id"]))["delivered"] is True


# 2. concurrent operations keep each other's cache entries
def test_concurrent_operation_checkpoints_do_not_overwrite_each_other():
    async def handler(job, ctx):
        async def slow(v):
            await asyncio.sleep(0.01)
            return v
        await asyncio.gather(*[ctx.call(f"op{i}", lambda i=i: slow(i), args=[i]) for i in range(6)])
        return {"answer": "ok"}
    jobs.register("t", handler)
    out = asyncio.run(jobs.run(asyncio.run(jobs.create("t", {}, user_id="u1"))))
    assert len(out["operations"]) == 6, "six concurrent calls, six cache entries"


def test_the_database_write_merges_operations_and_appends_lists():
    seen = []

    class _Pool:
        async def fetchrow(self, sql, *args):
            seen.append(sql)
            return None
        async def acquire(self):
            raise AssertionError("not needed")
    async def pool():
        return _Pool()
    original = jobs._pool
    jobs._pool = pool
    try:
        asyncio.run(jobs.cas("0" * 32, {"status": "running"}, {}, merge={"operations": {"k": {"result": 1}}}, append={"evidence": [{"a": 1}]}))
    finally:
        jobs._pool = original
    assert "operations = COALESCE(operations, '{}'::jsonb) || $" in seen[0] and "evidence = COALESCE(evidence, '[]'::jsonb) || $" in seen[0]
    assert "operations = $" not in seen[0]


# 3. delivery is claimed before the write: one message however many deliverers
def test_two_concurrent_deliveries_append_one_message(monkeypatch):
    appended = []

    async def append_turn(session_id, role, content, metadata=None):
        await asyncio.sleep(0.01)
        appended.append(content)
    monkeypatch.setattr(sessions, "append_turn", append_turn)

    async def get_messages(session_id):
        return [{"job_id": "other"}]
    monkeypatch.setattr(sessions, "get_messages", get_messages)

    async def handler(job, ctx):
        return {"answer": "once"}
    jobs.register("t", handler)
    job = asyncio.run(jobs.create("t", {}, user_id="u1", session_id="s"))
    asyncio.run(jobs._set(job["id"], attached=False))
    asyncio.run(jobs.run(job))                                                     # settle delivers once
    row = asyncio.run(jobs.get(job["id"]))

    async def two_stale_deliverers():
        await asyncio.gather(jobs.deliver({**row, "delivered": False}), jobs.deliver({**row, "delivered": False}))
    asyncio.run(two_stale_deliverers())
    assert appended == ["once"]


def test_a_delivery_that_fails_releases_the_claim_for_a_retry(monkeypatch):
    calls = {"n": 0}

    async def append_turn(session_id, role, content, metadata=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("redis blinked")
    monkeypatch.setattr(sessions, "append_turn", append_turn)

    async def get_messages(session_id):
        return []
    monkeypatch.setattr(sessions, "get_messages", get_messages)

    async def handler(job, ctx):
        return {"answer": "x"}
    jobs.register("t", handler)
    job = asyncio.run(jobs.create("t", {}, user_id="u1", session_id="s"))
    asyncio.run(jobs._set(job["id"], attached=False))
    out = asyncio.run(jobs.run(job))
    assert out["delivered"] is False                                               # the claim was released
    asyncio.run(jobs.maintain())
    assert asyncio.run(jobs.get(job["id"]))["delivered"] is True and calls["n"] == 2


# 4. the scrub deletes events in the same transaction, before the rows lose their owner
def test_scrub_deletes_events_before_clearing_ownership_in_one_transaction():
    log = []

    class _Conn:
        async def execute(self, sql, *args):
            log.append(sql.strip()[:40])
            return "UPDATE 2"
        def transaction(self):
            class _Tx:
                async def __aenter__(self):
                    log.append("BEGIN")
                async def __aexit__(self, *a):
                    log.append("COMMIT")
            return _Tx()

    class _Pool:
        def acquire(self):
            class _Acq:
                async def __aenter__(self):
                    return _Conn()
                async def __aexit__(self, *a):
                    return False
            return _Acq()
    async def pool():
        return _Pool()
    original = jobs._pool
    jobs._pool = pool
    try:
        assert asyncio.run(jobs.scrub_user(str(uuid.uuid4()))) == 2
    finally:
        jobs._pool = original
    assert log[0] == "BEGIN" and log[-1] == "COMMIT" and len(log) == 4
    assert log[1].startswith("DELETE FROM job_events") and log[2].startswith("UPDATE jobs SET spec")


# 5. a configured database that is down fails closed; the caller can run ephemerally
def test_a_down_database_never_falls_back_to_memory_for_durable_jobs(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql://db/orbit")

    async def down():
        raise ConnectionError("no route")
    monkeypatch.setattr(jobs, "get_pg_pool", down)

    async def handler(job, ctx):
        await ctx.plan(["one"])
        await ctx.step("0")
        await ctx.call("x", lambda: 42, args=[])
        return {"answer": "ephemeral answer"}
    jobs.register("t", handler)
    with pytest.raises(jobs.StoreUnavailable):
        asyncio.run(jobs.create("t", {}, user_id="u1"))
    out = asyncio.run(jobs.run_ephemeral("t", {}, user_id="u1"))
    assert out["answer"] == "ephemeral answer" and out["ephemeral"] is True
    assert jobs._memory == {}                                                      # nothing kept


# 7. a job runs as its owner
def test_a_job_runs_with_its_owners_tradingview_binding(monkeypatch):
    bound = []

    async def bind_turn(user_id):
        bound.append(user_id)
        return tradingview.current_token.set("tok" if user_id else None)
    monkeypatch.setattr(tradingview, "bind_turn", bind_turn)
    seen = {}

    async def handler(job, ctx):
        seen["token"] = tradingview.current_token.get()
        return {"answer": "x"}
    jobs.register("t", handler)
    asyncio.run(jobs.run(asyncio.run(jobs.create("t", {}, user_id="owner-1"))))
    assert bound == ["owner-1"] and seen["token"] == "tok"
    assert tradingview.current_token.get() is None                                 # reset after the run
