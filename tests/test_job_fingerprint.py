"""A job's checkpoints carry the assumptions they were built under; a resume
under different assumptions drops the cache and restarts the plan."""
import asyncio

import pytest

from app import jobs
from app.settings import settings


@pytest.fixture(autouse=True)
def _reset():
    jobs.reset_for_test()
    saved = dict(jobs._handlers), dict(jobs._assumptions)
    yield
    jobs._handlers.clear(); jobs._handlers.update(saved[0])
    jobs._assumptions.clear(); jobs._assumptions.update(saved[1])


def test_the_fingerprint_covers_kind_spec_version_and_named_settings(monkeypatch):
    async def handler(job, ctx):
        return {"answer": "x"}
    jobs.register("fp", handler, version="1", settings_keys=("exit_alert_drop_pct",))
    a = jobs.fingerprint("fp", {"address": "x"})
    assert a == jobs.fingerprint("fp", {"address": "x"})
    assert a != jobs.fingerprint("fp", {"address": "y"})
    monkeypatch.setattr(settings, "exit_alert_drop_pct", 99.0)
    assert a != jobs.fingerprint("fp", {"address": "x"})
    jobs.register("fp", handler, version="2", settings_keys=("exit_alert_drop_pct",))
    monkeypatch.setattr(settings, "exit_alert_drop_pct", 20.0)
    assert a != jobs.fingerprint("fp", {"address": "x"})


def test_a_resume_under_changed_assumptions_drops_the_cache_and_restarts(monkeypatch):
    calls = {"paid": 0, "attempt": 0}

    async def handler(job, ctx):
        calls["attempt"] += 1
        await ctx.plan(["fetch", "judge"])
        if not ctx.done("0"):
            await ctx.step("0")
            async def paid():
                calls["paid"] += 1
                return {"n": calls["paid"]}
            await ctx.call("provider", paid, args=[1])
            await ctx.finish_step("0")
        if calls["attempt"] == 1:
            raise RuntimeError("crash after step 0")
        await ctx.step("1")
        await ctx.finish_step("1")
        return {"answer": "done"}

    jobs.register("fp", handler, version="1", settings_keys=("exit_alert_drop_pct",))
    job = asyncio.run(jobs.create("fp", {}, user_id="u1"))
    after_crash = asyncio.run(jobs.run(job))
    assert after_crash["plan"][0]["status"] == "done" and len(after_crash["operations"]) == 1
    # Same assumptions: the resume keeps the cache and repeats nothing.
    resumed = asyncio.run(jobs.run(after_crash))
    assert resumed["status"] == "succeeded" and calls["paid"] == 1

    # Different assumptions on the next job: the cache is dropped at the claim.
    calls.update({"paid": 0, "attempt": 0})
    job2 = asyncio.run(jobs.create("fp", {}, user_id="u1"))
    crashed = asyncio.run(jobs.run(job2))
    monkeypatch.setattr(settings, "exit_alert_drop_pct", 55.0)                # a deploy changed a setting the cache depends on
    resumed = asyncio.run(jobs.run(crashed))
    assert resumed["status"] == "succeeded" and calls["paid"] == 2               # fetched again under the new assumptions
    assert "reset" in [e["kind"] for e in asyncio.run(jobs.events(job2["id"]))]
    assert resumed["fingerprint"] == jobs.fingerprint("fp", {})
