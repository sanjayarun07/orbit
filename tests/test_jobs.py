"""Durable jobs (app/jobs.py, docs/durable-jobs-spec.md): the acceptance list.

- a crash mid-plan resumes at the next step with zero repeated provider calls
- two claimers, one job: exactly one runs it
- a lease lost during a call stops the handler at its next guard; state intact
- an approval that expires fails the step and asks again with a new plan
- an account deleted mid-run stops the job and keeps no words
- the export lists the job and its events; a detached job delivers once
"""
import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app import credits, jobs, sessions, turn_log
from app.settings import settings


@pytest.fixture(autouse=True)
def _kinds():
    jobs.reset_for_test()
    saved = dict(jobs._handlers)
    yield
    jobs._handlers.clear()
    jobs._handlers.update(saved)
    jobs.reset_for_test()


def _run(coro):
    return asyncio.run(coro)


# ---- 1. resume after a crash: cached operations are not repeated ----

def test_a_crash_mid_plan_resumes_without_repeating_paid_calls():
    calls = {"paid": 0, "attempt": 0}

    async def handler(job, ctx):
        calls["attempt"] += 1
        await ctx.plan(["fetch", "judge"])
        if not ctx.done("0"):
            await ctx.step("0")
            async def paid():
                calls["paid"] += 1
                return {"price": 1.23}
            got = await ctx.call("provider:price", paid, args={"symbol": "BONK"})
            await ctx.add_evidence({"kind": "price", "value": got["price"]})
            await ctx.finish_step("0")
        if calls["attempt"] == 1:
            raise RuntimeError("worker died here")               # the crash, after step 0 was checkpointed
        await ctx.step("1")
        cached = await ctx.call("provider:price", lambda: {"price": 999}, args={"symbol": "BONK"})   # must come from the cache
        await ctx.finish_step("1")
        return {"answer": f"verdict on {cached['price']}"}

    jobs.register("t", handler)
    job = _run(jobs.create("t", {"symbol": "BONK"}, user_id="u1"))
    after_crash = _run(jobs.run(job))
    assert after_crash["status"] == "queued" and after_crash["attempts"] == 1 and "worker died" in after_crash["error"]
    assert after_crash["plan"][0]["status"] == "done" and after_crash["evidence"] == [{"kind": "price", "value": 1.23}]
    resumed = _run(jobs.run(after_crash))
    assert resumed["status"] == "succeeded" and resumed["result"]["answer"] == "verdict on 1.23"
    assert calls == {"paid": 1, "attempt": 2}, "the paid call ran once across two attempts"
    kinds = [e["kind"] for e in _run(jobs.events(job["id"]))]
    assert kinds == ["created", "started", "step", "error", "resumed", "step", "settled"]


# ---- 2. two claimers, one job ----

def test_two_workers_claim_one_job_and_only_one_gets_it():
    async def handler(job, ctx):
        return {"answer": "ok"}
    jobs.register("t", handler)
    job = _run(jobs.create("t", {}, user_id="u1"))
    first = _run(jobs.claim(job))
    second = _run(jobs.claim(job))                                  # the same snapshot: the row moved on
    assert first is not None and second is None
    assert first[0]["status"] == "running" and first[0]["lease_id"] == first[1]


# ---- 3. a lease lost during a call ----

def test_a_lost_lease_stops_the_handler_at_its_next_guard_with_state_intact():
    async def handler(job, ctx):
        await ctx.checkpoint(state={"progress": "half"})
        # Another worker takes the row over (a lease that expired elsewhere).
        await jobs.cas(job["id"], {"status": "running"}, {"lease_id": "someone-else"})
        await ctx.guard()                                           # must raise
        raise AssertionError("the handler kept going without its lease")

    jobs.register("t", handler)
    job = _run(jobs.create("t", {}, user_id="u1"))
    out = _run(jobs.run(job))
    assert out["status"] == "running" and out["lease_id"] == "someone-else"      # the other worker's lease stands
    assert out["state"] == {"progress": "half"} and out["error"] is None


# ---- 4. approval expiry ----

def test_an_expired_approval_fails_the_step_and_asks_again_with_a_new_plan():
    asked = []

    async def handler(job, ctx):
        expired = (job.get("state") or {}).get("expired")
        approved = (job.get("state") or {}).get("approved")
        if approved:
            return {"answer": f"executed {approved}"}
        plan_id = f"plan-{len(asked) + 1}"
        asked.append(plan_id)
        await ctx.approve(plan_id)

    jobs.register("t", handler)
    job = _run(jobs.create("t", {}, user_id="u1"))
    paused = _run(jobs.run(job))
    assert paused["status"] == "waiting_approval" and paused["action_id"] == "plan-1"
    assert _run(jobs.expire_approval(job["id"], "plan-1"))["status"] == "queued"
    paused_again = _run(jobs.run(_run(jobs.get(job["id"]))))
    assert paused_again["status"] == "waiting_approval" and paused_again["action_id"] == "plan-2"    # a new plan, never the expired one
    assert _run(jobs.approved(job["id"], "plan-2"))["status"] == "queued"
    done = _run(jobs.run(_run(jobs.get(job["id"]))))
    assert done["status"] == "succeeded" and done["result"]["answer"] == "executed plan-2" and asked == ["plan-1", "plan-2"]


# ---- 5. deletion mid-run ----

def test_an_account_deleted_mid_run_stops_the_job_and_keeps_no_words():
    async def handler(job, ctx):
        await ctx.checkpoint(state={"private": "what the user asked"})
        turn_log.forget_user("u-gone")                              # the deletion lands while the job runs
        await ctx.guard()
        raise AssertionError("kept running for a deleted account")

    jobs.register("t", handler)
    job = _run(jobs.create("t", {"request": "my secret question"}, user_id="u-gone", session_id="s1"))
    out = _run(jobs.run(job))
    assert out["status"] == "cancelled" and out["user_id"] is None and out["spec"] == {} and out["state"] == {}
    assert _run(jobs.events(job["id"])) == []


# ---- 6. attach, detach, deliver once; export ----

def test_attach_runs_inline_without_a_worker_and_returns_the_settled_row():
    async def handler(job, ctx):
        await ctx.plan(["one"])
        await ctx.step("0")
        return {"answer": "done"}
    jobs.register("t", handler)
    job = _run(jobs.create("t", {}, user_id="u1"))
    seen = []
    row = _run(jobs.attach(job["id"], on_event=lambda e: seen.append(e["title"])))
    assert row["status"] == "succeeded" and seen == ["one"]


def test_a_detached_job_delivers_its_answer_to_the_conversation_and_charges_once(monkeypatch):
    charged = []

    async def charge_once(account_id, amount, reason, ref_type, ref_id, meta=None):
        charged.append((account_id, amount, ref_type, ref_id))
    monkeypatch.setattr(credits, "charge_once", charge_once)
    monkeypatch.setattr(settings, "credit_cost_deep_dive", 5)
    monkeypatch.setattr(jobs, "_worker_running", True)               # a worker exists elsewhere: attach waits, never runs inline

    async def handler(job, ctx):
        return {"answer": "the verdict"}
    jobs.register("deep_dive", handler)
    conversation = f"jobs-test-{uuid.uuid4().hex}"                 # sessions may live in a real Redis: a fresh id, cleared after
    job = _run(jobs.create("deep_dive", {}, user_id="u1", account_id="acct", session_id=conversation))

    async def scenario():
        try:
            row = await jobs.attach(job["id"], timeout=0.2)        # nobody runs it in time
            assert row["status"] == "queued" and row["attached"] is False
            settled = await jobs.run(row)                          # the worker gets to it later
            assert settled["status"] == "succeeded" and settled["delivered"] is True
            await jobs.maintain()                                  # idempotent: no second delivery
            return await sessions.get_messages(conversation)
        finally:
            await sessions.clear_history(conversation)
    messages = _run(scenario())
    assert [m["content"] for m in messages] == ["the verdict"] and messages[0]["job_id"] == job["id"]
    assert charged == [("acct", 5, "job", job["id"])]


def test_the_export_lists_the_persons_jobs_with_their_events_and_cancel_is_theirs_only():
    async def handler(job, ctx):
        return {"answer": "x"}
    jobs.register("t", handler)
    mine = _run(jobs.create("t", {"q": 1}, user_id="u1"))
    _run(jobs.create("t", {"q": 2}, user_id="u2"))
    listed = _run(jobs.list_for("u1", limit=None))
    assert [j["id"] for j in listed] == [mine["id"]] and jobs.public(listed[0])["spec"] == {"q": 1}
    assert _run(jobs.cancel(mine["id"], "u2")) is None
    assert _run(jobs.cancel(mine["id"], "u1"))["status"] == "cancelled"
