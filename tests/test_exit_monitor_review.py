"""The review of 2026-09-22 on the exit monitor and delivery: seven findings
and the no-route alert, each pinned."""
import asyncio
import uuid
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
    assert row["status"] == "succeeded" and row["delivered"] is False
    asyncio.run(jobs.acknowledge(job["id"]))                        # what the chat commit does
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


# ---- the third review (2026-09-22) ----

def test_attach_does_not_acknowledge_and_the_chat_commit_does(monkeypatch):
    """The turn may still fail between attach and its commit; only a
    committed conversation acknowledges the job."""
    from fastapi.testclient import TestClient
    from app import execution_policy, main
    from app.graph import AgentRun
    from tests.conftest import sign_in

    async def handler(job, ctx):
        return {"answer": "the verdict"}
    jobs.register("t", handler)
    job = asyncio.run(jobs.create("t", {}, user_id="u1", session_id="s"))
    row = asyncio.run(jobs.attach(job["id"]))
    assert row["status"] == "succeeded" and row["delivered"] is False              # attach never acknowledges

    client = TestClient(main.app)
    sign_in(client)

    async def fake_run_agent(*a, **k):
        return AgentRun(answer="the verdict", trajectory={"tool_name_0": "token_deep_dive"}, trade_plan=None, intent="research",
                        capabilities=["token_discovery"], job_id=job["id"], job_attached=True)
    monkeypatch.setattr(execution_policy, "run_agent", fake_run_agent)

    async def failing_commit(*a, **k):
        raise RuntimeError("history store down")
    monkeypatch.setattr(execution_policy, "commit_turn", failing_commit)
    r = client.post("/chat", json={"message": "deep dive on BONK"})
    assert r.status_code >= 500 and asyncio.run(jobs.get(job["id"]))["delivered"] is False   # not acknowledged: maintenance will deliver

    async def ok_commit(*a, **k):
        return None
    monkeypatch.setattr(execution_policy, "commit_turn", ok_commit)
    r = client.post("/chat", json={"message": "deep dive on BONK"})
    assert r.status_code == 200 and r.json().get("job_id") is None                  # attached: the browser has nothing to poll
    assert asyncio.run(jobs.get(job["id"]))["delivered"] is True


def test_a_failed_history_write_leaves_no_position_and_no_live_job(monkeypatch, chain):
    async def boom(position_id, quantity_raw, rows):
        raise RuntimeError("quotes table unavailable")
    monkeypatch.setattr(exit_monitor, "record", boom)
    with pytest.raises(RuntimeError):
        _watch()
    assert asyncio.run(exit_monitor.list_for("u1")) == []
    assert all(j["status"] == "cancelled" for j in asyncio.run(jobs.list_for("u1")))


def test_a_stale_active_position_is_repaired_by_the_next_watch(monkeypatch, chain):
    watched = _watch()
    asyncio.run(jobs.cancel(watched["job_id"], "u1"))                               # the monitor is gone, the row says active

    async def search(query):
        return [{"id": MINT, "symbol": "BONK", "name": "Bonk", "tags": ["verified"]}]
    monkeypatch.setattr(exit_controls.jupiter, "search_tokens", search)
    reply = asyncio.run(exit_controls.handle("watch my exit on BONK", {"id": "u1"}, WALLET))
    assert reply.startswith("Watching your BONK exit") and "Already watching" not in reply
    rows = asyncio.run(exit_monitor.list_for("u1"))
    assert [r["status"] for r in rows] == ["active", "closed"] and asyncio.run(exit_monitor.is_live(rows[0]))


def test_a_timeout_is_unavailable_data_not_a_missing_route(monkeypatch, chain):
    import httpx
    watched = _watch()

    async def timeout(input_mint, output_mint, amount):
        raise httpx.ReadTimeout("read timed out")
    monkeypatch.setattr(exit_monitor, "simulate_swap", timeout)
    asyncio.run(jobs.run(asyncio.run(jobs.get(watched["job_id"]))))
    assert asyncio.run(tasks.inbox("u1")) == []
    assert asyncio.run(exit_monitor.history(watched["id"]))[0]["quotes"][2]["error"] == "quote unavailable (timeout)"
    assert exit_monitor._route_error(RuntimeError("Jupiter: COULD_NOT_FIND_ANY_ROUTE")) == "no route"
    assert exit_monitor._route_error(ConnectionError("reset")) == "quote unavailable (connection)"
    assert exit_monitor._route_error(RuntimeError("HTTP 401 unauthorized")) == "quote unavailable (auth)"


# ---- the fourth review (2026-09-22) ----

def test_the_committed_message_carries_the_job_id_and_delivery_reconciles_from_it(monkeypatch):
    """The commit and the acknowledgement are two writes; the message is the
    durable record. Delivery finds it and neither appends nor charges."""
    from fastapi.testclient import TestClient
    from app import credits, execution_policy, main
    from app.graph import AgentRun
    from tests.conftest import sign_in

    async def handler(job, ctx):
        return {"answer": "the verdict"}
    jobs.register("t", handler)
    job = asyncio.run(jobs.create("t", {}, user_id="u1", account_id="acct", session_id="s"))
    asyncio.run(jobs.attach(job["id"]))
    client = TestClient(main.app)
    sign_in(client)

    async def fake_run_agent(*a, **k):
        return AgentRun(answer="the verdict", trajectory={"tool_name_0": "token_deep_dive"}, trade_plan=None, intent="research",
                        capabilities=["token_discovery"], job_id=job["id"], job_attached=True)
    monkeypatch.setattr(execution_policy, "run_agent", fake_run_agent)
    acknowledged = {"n": 0}

    async def crash_before_ack(job_id):
        acknowledged["n"] += 1
        raise RuntimeError("process died after the commit")
    monkeypatch.setattr(jobs, "acknowledge", crash_before_ack)
    session_id = f"ack-test-{uuid.uuid4().hex}"                                      # sessions may live in a real Redis: own id, cleared after
    try:
        r = client.post("/chat", json={"message": "deep dive on BONK", "session_id": session_id})
        assert r.status_code == 200, r.text[:200]                                   # a failed acknowledgement never fails the turn
        assert acknowledged["n"] == 1 and asyncio.run(jobs.get(job["id"]))["delivered"] is False
        messages = asyncio.run(sessions.get_messages(session_id))
        committed = [m for m in messages if m.get("role") == "assistant"]
        assert committed and committed[-1].get("job_id") == job["id"]                # the durable record

        charged = []
        async def charge_once(*a, **k):
            charged.append(a)
        monkeypatch.setattr(credits, "charge_once", charge_once)
        with jobs._lock:
            jobs._memory[job["id"]]["session_id"] = session_id
            jobs._memory[job["id"]]["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        asyncio.run(jobs.maintain())                                                 # the attachment has expired
        after = [m for m in asyncio.run(sessions.get_messages(session_id)) if m.get("role") == "assistant"]
        assert len(after) == len(committed) and charged == [] and asyncio.run(jobs.get(job["id"]))["delivered"] is True
    finally:
        asyncio.run(sessions.clear_history(session_id))


def test_a_failed_job_returned_through_chat_is_acknowledged_too(monkeypatch):
    from app.nodes import research

    async def fake_attach(job_id, timeout=None, on_event=None):
        return {"id": job_id, "status": "failed", "error": "Mobula down"}
    async def fake_create(kind, spec, **kw):
        return {"id": "job-failed"}
    monkeypatch.setattr(research.jobs, "attach", fake_attach)
    monkeypatch.setattr(research.jobs, "create", fake_create)

    async def resolved(*a, **k):
        from app.nodes.research import _TokenResolution
        return _TokenResolution(f"top holders of {MINT} on solana", chain="solana")
    monkeypatch.setattr(research, "_resolve_named_token", resolved)
    out = asyncio.run(research._run_token_deep_dive({"capabilities": ["token_discovery"], "chains": []}, "deep dive on BONK"))
    assert out["job_id"] == "job-failed" and out["job_attached"] is True and "could not be completed" in out["answer"]


def test_an_http_400_whose_body_says_no_route_is_a_no_route(monkeypatch):
    import httpx
    request = httpx.Request("GET", "https://quote.example/quote")
    response = httpx.Response(400, json={"errorCode": "COULD_NOT_FIND_ANY_ROUTE", "error": "Could not find any route"}, request=request)
    exc = httpx.HTTPStatusError("Client error '400 Bad Request'", request=request, response=response)
    assert exit_monitor._route_error(exc) == "no route"
    other = httpx.HTTPStatusError("Server error '502'", request=request, response=httpx.Response(502, text="bad gateway", request=request))
    assert exit_monitor._route_error(other) == "quote provider outage"


# ---- the fifth review (2026-09-22): a failed background charge is retried ----

def test_a_failed_background_charge_is_retried_even_though_the_message_exists(monkeypatch):
    store: dict[str, list] = {}

    async def append_turn(session_id, role, content, metadata=None):
        store.setdefault(session_id, []).append({"role": role, "content": content, **(metadata or {})})
    async def get_messages(session_id):
        return list(store.get(session_id, []))
    monkeypatch.setattr(sessions, "append_turn", append_turn)
    monkeypatch.setattr(sessions, "get_messages", get_messages)
    from app import credits
    charges = {"n": 0}

    async def charge_once(*a, **k):
        charges["n"] += 1
        if charges["n"] == 1:
            raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(credits, "charge_once", charge_once)

    async def handler(job, ctx):
        return {"answer": "x"}
    jobs.register("t", handler)
    job = asyncio.run(jobs.create("t", {}, user_id="u1", account_id="acct", session_id="s"))
    asyncio.run(jobs._set(job["id"], attached=False))
    out = asyncio.run(jobs.run(job))                                                 # appended, charge failed: still pending
    assert out["delivered"] is False and len(store["s"]) == 1 and store["s"][0]["delivered_by"] == "job"
    with jobs._lock:
        jobs._memory[job["id"]]["delivering_until"] = None
    asyncio.run(jobs.maintain())                                                     # the retry charges, does not append again
    assert charges["n"] == 2 and len(store["s"]) == 1 and asyncio.run(jobs.get(job["id"]))["delivered"] is True

    # and a message the TURN committed is still never charged twice
    store["t"] = [{"role": "assistant", "content": "x", "job_id": "job-turn"}]
    turn_job = asyncio.run(jobs.create("t", {}, user_id="u1", account_id="acct", session_id="t"))
    with jobs._lock:
        jobs._memory[turn_job["id"]].update({"attached": False, "status": "succeeded", "result": {"answer": "x"}})
        store["t"][0]["job_id"] = turn_job["id"]
    asyncio.run(jobs.deliver(asyncio.run(jobs.get(turn_job["id"]))))
    assert charges["n"] == 2 and asyncio.run(jobs.get(turn_job["id"]))["delivered"] is True
