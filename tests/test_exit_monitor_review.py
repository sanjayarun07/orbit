"""The review of 2026-09-22 on the exit monitor and delivery: seven findings
and the no-route alert, each pinned."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import exit_controls, exit_monitor, jobs, sessions, tasks
from app.settings import settings

WALLET = "7GK7tZ1yD1mS3sJt7dcqJ2k5aZm7gJ8yq1uZ6R9WWEFm"
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def _accounts(amount: int, decimals: int = 5):
    return {"value": [{"account": {"data": {"parsed": {"info": {"mint": MINT, "tokenAmount": {"amount": str(amount), "decimals": decimals}}}}}}]}


def _sim(price: float, impact: float):
    from types import SimpleNamespace

    async def simulate_swap(input_mint, output_mint, amount):
        tokens = amount / 1e5
        out = tokens * price * (1 - impact / 100)
        return {"input_token": SimpleNamespace(symbol="BONK", usd_price=price, decimals=5), "output_token": SimpleNamespace(symbol="USDC", decimals=6),
                "input_amount": tokens, "input_value_usd": tokens * price, "output_amount": out, "output_value_usd": out, "price_impact_pct": impact,
                "quote": {"otherAmountThreshold": str(int(out * 0.995 * 1e6)), "slippageBps": 50, "routePlan": [{"swapInfo": {"label": "Raydium"}}]}}
    return simulate_swap


def _failing(reason="Could not find any route"):
    async def simulate_swap(input_mint, output_mint, amount):
        raise RuntimeError(reason)
    return simulate_swap


@pytest.fixture(autouse=True)
def _stores():
    jobs.reset_for_test()
    exit_monitor.reset_for_test()
    yield


@pytest.fixture
def chain(monkeypatch):
    async def accounts(wallet):
        return _accounts(1_000_000_00000)
    async def none(wallet):
        return {"value": []}
    monkeypatch.setattr(exit_monitor, "get_token_accounts", accounts)
    monkeypatch.setattr(exit_monitor, "_token_accounts_2022", none)
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.00002, 1.5))
    monkeypatch.setattr(exit_controls, "_resolved", {})


def _watch(user="u1"):
    pos = asyncio.run(exit_monitor.position_of(WALLET, MINT))
    rows = asyncio.run(exit_monitor.quote_exit(MINT, pos["quantity_raw"]))
    return asyncio.run(exit_monitor.watch(user, WALLET, MINT, "BONK", pos, rows))


# 1. an answer returned by the attached request is acknowledged, never delivered or charged again
def test_an_answer_returned_to_the_attached_request_is_not_delivered_again(monkeypatch):
    appended, charged = [], []

    async def append_turn(session_id, role, content, metadata=None):
        appended.append(content)
    async def get_messages(session_id):
        return []
    async def charge_once(*a, **k):
        charged.append(a)
    monkeypatch.setattr(sessions, "append_turn", append_turn)
    monkeypatch.setattr(sessions, "get_messages", get_messages)
    from app import credits
    monkeypatch.setattr(credits, "charge_once", charge_once)

    async def handler(job, ctx):
        return {"answer": "returned inline"}
    jobs.register("t", handler)
    job = asyncio.run(jobs.create("t", {}, user_id="u1", account_id="acct", session_id="s"))
    row = asyncio.run(jobs.attach(job["id"]))                       # inline: the request gets the answer
    assert row["status"] == "succeeded" and row["delivered"] is True
    with jobs._lock:
        jobs._memory[job["id"]]["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    asyncio.run(jobs.maintain())
    assert appended == [] and charged == []


def test_a_request_cancelled_after_the_settle_hands_the_answer_to_delivery(monkeypatch):
    async def handler(job, ctx):
        return {"answer": "x"}
    jobs.register("t", handler)
    monkeypatch.setattr(jobs, "_worker_running", True)
    job = asyncio.run(jobs.create("t", {}, user_id="u1", session_id="s"))

    async def scenario():
        settled = await jobs.run(job)                                 # attached, but the request never returned it
        assert settled["delivered"] is False
        await jobs._detach_unacknowledged(job["id"])                  # what attach() does on CancelledError
        return await jobs.get(job["id"])
    row = asyncio.run(scenario())
    assert row["attached"] is False and row["delivered"] is False


# 2. a delivery claim that lapses is retried; the answer is never lost
def test_a_crash_after_the_delivery_claim_does_not_lose_the_answer(monkeypatch):
    appended = []
    calls = {"n": 0}

    async def append_turn(session_id, role, content, metadata=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.CancelledError()                            # the process died mid-delivery
        appended.append(content)
    async def get_messages(session_id):
        return []
    monkeypatch.setattr(sessions, "append_turn", append_turn)
    monkeypatch.setattr(sessions, "get_messages", get_messages)

    async def handler(job, ctx):
        return {"answer": "keep me"}
    jobs.register("t", handler)
    job = asyncio.run(jobs.create("t", {}, user_id="u1", session_id="s"))
    asyncio.run(jobs._set(job["id"], attached=False))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(jobs.run(job))
    row = asyncio.run(jobs.get(job["id"]))
    assert row["delivered"] is False and row["delivering_until"] is not None          # claimed, not acknowledged
    asyncio.run(jobs.maintain())                                                     # claim still live: no retry yet
    assert appended == []
    with jobs._lock:
        jobs._memory[job["id"]]["delivering_until"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    asyncio.run(jobs.maintain())                                                     # claim lapsed: retried
    assert appended == ["keep me"] and asyncio.run(jobs.get(job["id"]))["delivered"] is True


# 3. an RPC failure is unavailable, not zero
def test_a_failed_balance_read_is_unavailable_not_a_zero(monkeypatch, chain):
    async def boom(wallet):
        raise RuntimeError("429 Too Many Requests")
    monkeypatch.setattr(exit_monitor, "get_token_accounts", boom)
    with pytest.raises(exit_monitor.BalanceUnavailable):
        asyncio.run(exit_monitor.position_of(WALLET, MINT))

    async def search(query):
        return [{"id": MINT, "symbol": "BONK", "name": "Bonk", "tags": ["verified"]}]
    monkeypatch.setattr(exit_controls.jupiter, "search_tokens", search)
    reply = asyncio.run(exit_controls.handle("exit analysis for BONK", {"id": "u1"}, WALLET))
    assert "couldn't read" in reply and "not a zero" in reply and "holds no" not in reply


def test_the_monitor_keeps_the_last_known_quantity_when_the_chain_is_unreadable(monkeypatch, chain):
    watched = _watch()
    async def boom(wallet):
        raise RuntimeError("timeout")
    monkeypatch.setattr(exit_monitor, "get_token_accounts", boom)
    out = asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))
    assert out["status"] == "scheduled" and "balance unavailable" in out["result"]["answer"]
    assert asyncio.run(exit_monitor.get(watched["id"]))["quantity_raw"] == 1_000_000_00000
    assert asyncio.run(exit_monitor.history(watched["id"]))[0]["quotes"][0]["error"].startswith("balance unavailable")


# 4. a size change re-baselines instead of alerting
def test_halving_the_position_is_a_new_baseline_not_a_50pct_deterioration(monkeypatch, chain):
    watched = _watch()
    async def half(wallet):
        return _accounts(500_000_00000)
    monkeypatch.setattr(exit_monitor, "get_token_accounts", half)
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))
    assert asyncio.run(tasks.inbox("u1")) == []
    row = asyncio.run(exit_monitor.get(watched["id"]))
    assert row["entry"]["quantity_raw"] == 500_000_00000 and "position changed" in row["entry"]["reason"]


# 5. a failed entry quote leaves the monitor pending until the first valid one
def test_a_failed_entry_quote_is_pending_and_the_first_valid_quote_becomes_the_baseline(monkeypatch, chain):
    monkeypatch.setattr(exit_monitor, "simulate_swap", _failing("HTTP 503 upstream"))
    watched = _watch()
    assert watched["entry"]["baseline"] is False
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.00002, 1.5))
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))
    row = asyncio.run(exit_monitor.get(watched["id"]))
    assert row["entry"]["baseline"] is True and row["entry"]["reason"] == "first valid quote"
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.0000002, 1.5))     # -99%
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))
    inbox = asyncio.run(tasks.inbox("u1"))
    assert len(inbox) == 1 and "deteriorated 99%" in inbox[0]["title"]


# 6. a registration whose job cannot be created is not created
def test_a_watch_whose_job_cannot_be_created_leaves_nothing_behind(monkeypatch, chain):
    async def no_store(*a, **k):
        raise jobs.StoreUnavailable("down")
    monkeypatch.setattr(jobs, "create", no_store)
    with pytest.raises(jobs.StoreUnavailable):
        _watch()
    assert asyncio.run(exit_monitor.list_for("u1")) == []


# 7. a retried alert keeps one inbox item
def test_a_retry_after_a_failed_checkpoint_does_not_duplicate_the_alert(monkeypatch, chain):
    watched = _watch()
    monkeypatch.setattr(exit_monitor, "simulate_swap", _sim(0.00001, 3.0))       # -50%
    original = exit_monitor._update
    calls = {"n": 0}

    async def flaky_update(position_id, **fields):
        if "last_alert" in fields and fields["last_alert"] is not None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db blinked after the notification")
        return await original(position_id, **fields)
    monkeypatch.setattr(exit_monitor, "_update", flaky_update)
    first = asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))          # attempt fails after notifying
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))                  # retried
    inbox = asyncio.run(tasks.inbox("u1"))
    assert len(inbox) == 1 and calls["n"] == 2


# the no-route alert
def test_a_full_exit_that_can_no_longer_be_quoted_raises_the_no_route_alert(monkeypatch, chain):
    watched = _watch()
    async def partial(input_mint, output_mint, amount):
        if amount >= 1_000_000_00000:
            raise RuntimeError("Could not find any route")
        return await _sim(0.00002, 1.5)(input_mint, output_mint, amount)
    monkeypatch.setattr(exit_monitor, "simulate_swap", partial)
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))
    inbox = asyncio.run(tasks.inbox("u1"))
    assert len(inbox) == 1 and inbox[0]["title"] == "Exit for BONK: no route"
    assert "cannot be quoted now: no route" in inbox[0]["body"] and "50% $9.85" in inbox[0]["body"]
    monkeypatch.setattr(exit_monitor, "simulate_swap", _failing("HTTP 429 rate limited"))     # a provider gap is not a market fact
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))
    assert len(asyncio.run(tasks.inbox("u1"))) == 1
