"""Safeguards the review found missing outside the trading core:

1. Conversations belong to the account that used them: another signed-in
   user cannot read, extend or delete one by knowing its id, and ownership
   never transfers.
2. The risk charter gates Relay (cross-chain) drafts like Jupiter plans.
3. A charter rule that cannot be evaluated is reported as unresolved, not
   passed; the position limit measures post-trade exposure.
4. A scheduled occurrence is claimed atomically: concurrent workers, or a
   worker overlapping "run now", deliver and charge it once.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import accounts, main, tasks
from app.graph import AgentRun
from app.nodes import trading
from app.settings import settings
from tests.conftest import sign_in


@pytest.fixture
def fake_agent(monkeypatch):
    async def fake_run(message, wallet, history, session_context, action):
        return AgentRun(answer="ok", trajectory=None, trade_plan=None, intent="general", capabilities=[])

    monkeypatch.setattr(main, "run_agent", fake_run)


# --- 1. conversation ownership -------------------------------------------------

def test_another_user_cannot_read_extend_or_delete_a_conversation(fake_agent):
    alice, bob = TestClient(main.app), TestClient(main.app)
    sign_in(alice, email="alice@example.com")
    sign_in(bob, email="bob@example.com")
    sid = alice.post("/chat", json={"message": "hello"}).json()["session_id"]
    assert alice.get(f"/chat/history/{sid}").status_code == 200
    # Bob knows the id. Every operation is a 404 (no probing of which ids exist).
    assert bob.get(f"/chat/history/{sid}").status_code == 404
    assert bob.delete(f"/chat/history/{sid}").status_code == 404
    assert bob.post("/chat", json={"message": "hijack", "session_id": sid}).status_code == 404
    assert bob.post("/chat/feedback", json={"session_id": sid, "session_revision": 1, "rating": "up"}).status_code == 404
    assert bob.get(f"/executions/relay/session/{sid}/1").status_code == 404
    # Alice's conversation is intact and still hers.
    assert len(alice.get(f"/chat/history/{sid}").json()["messages"]) == 2
    owner = asyncio.run(accounts.chat_session_owner(sid))
    assert owner == alice.get("/me").json()["user"]["id"]
    # Ownership never transfers, even through the mapping call itself.
    bob_id = bob.get("/me").json()["user"]["id"]
    assert asyncio.run(accounts.touch_chat_session(bob_id, sid)) is False
    assert asyncio.run(accounts.chat_session_owner(sid)) == owner
    # Anonymous callers cannot extend an owned conversation either.
    assert TestClient(main.app).post("/chat", json={"message": "hi", "session_id": sid}).status_code == 404


def test_an_anonymous_conversation_is_claimed_on_first_signed_in_use(fake_agent):
    client = TestClient(main.app)
    sid = client.post("/chat", json={"message": "hello"}).json()["session_id"]   # anonymous: unmapped
    assert asyncio.run(accounts.chat_session_owner(sid)) is None
    me = sign_in(client, email="claim@example.com")
    assert client.get(f"/chat/history/{sid}").status_code == 200                  # reading claims it
    assert asyncio.run(accounts.chat_session_owner(sid)) == me["user"]["id"]
    other = TestClient(main.app)
    sign_in(other, email="other@example.com")
    assert other.get(f"/chat/history/{sid}").status_code == 404


# --- 2. the charter gates Relay drafts ---------------------------------------

def _relay_state(request, fields=None, charter="structured"):
    context = {"risk_charter": charter, "risk_charter_fields": fields} if fields else {}
    return {"request": request, "wallet_address": "0x1111111111111111111111111111111111111111", "chains": [], "session_context": context}


def test_relay_draft_is_blocked_by_chain_and_slippage_rules():
    request = "Swap 0.1 ETH on Base to USDC on Base with 500 bps slippage"
    free = asyncio.run(trading.cross_chain_swap_node(_relay_state(request)))
    assert free["cross_chain_swap"] is not None and free["cross_chain_swap"].slippage_bps == 500
    gated = asyncio.run(trading.cross_chain_swap_node(_relay_state(request, {"allowed_chains": ["solana"], "max_slippage_bps": 10})))
    assert gated["cross_chain_swap"] is None and gated["risk_assessment"].verdict == "blocked"
    assert "source chain is base" in gated["answer"].lower() and "slippage is 500 bps but your max is 10 bps" in gated["answer"]
    # Within the rules: the draft goes through.
    ok = asyncio.run(trading.cross_chain_swap_node(_relay_state(request, {"allowed_chains": ["base"], "max_slippage_bps": 500})))
    assert ok["cross_chain_swap"] is not None
    # A slippage cap with no slippage requested is unresolved -> blocked, with the fix spelled out.
    vague = asyncio.run(trading.cross_chain_swap_node(_relay_state("Swap 0.1 ETH on Base to USDC on Base", {"max_slippage_bps": 50})))
    assert vague["cross_chain_swap"] is None and "state one at or below it" in vague["answer"]
    # Verified-only cannot be satisfied off Solana.
    strict = asyncio.run(trading.cross_chain_swap_node(_relay_state(request, {"verified_only": True})))
    assert strict["cross_chain_swap"] is None and "Jupiter-verified" in strict["answer"]


# --- 3. unresolved rules and post-trade exposure ---------------------------

def _plan(notional=200.0, slippage=50, out_mint="OUTmint", verified=True):
    return SimpleNamespace(plan_id="plan-1", input_value_usd=notional, proposal=SimpleNamespace(slippage_bps=slippage),
                           output_token=SimpleNamespace(mint=out_mint, symbol="OUT", verified=verified))


def test_position_limit_uses_post_trade_exposure_and_reports_unknowns():
    fields = {"max_position_pct": 50}
    # Portfolio value unknown: unresolved, never a silent pass.
    violations, unresolved = trading._charter_field_violations(fields, _plan(), None, [])
    assert violations == [] and unresolved and "portfolio value is unavailable" in unresolved[0]
    # $200 trade into a $1,000 portfolio is 20%... but $400 is already held: 60% after the trade.
    holdings = [{"mint": "OUTmint", "symbol": "OUT", "usd_value": 400.0}, {"mint": "other", "usd_value": 600.0}]
    violations, unresolved = trading._charter_field_violations(fields, _plan(), 1000.0, holdings)
    assert unresolved == [] and len(violations) == 1 and "60.0%" in violations[0] and "already hold $400.00" in violations[0]
    # Nothing held yet: 20%, within the limit.
    assert trading._charter_field_violations(fields, _plan(), 1000.0, []) == ([], [])
    # Unknown notional leaves the USD cap unresolved too.
    _, unresolved = trading._charter_field_violations({"max_trade_usd": 100}, _plan(notional=None), 1000.0, [])
    assert unresolved and "USD value is unknown" in unresolved[0]


def test_charter_node_withholds_the_card_when_a_rule_cannot_be_checked(monkeypatch):
    superseded = []

    async def snapshot_down(wallet):
        raise RuntimeError("rpc down")

    async def mark(plan_id):
        superseded.append(plan_id)

    monkeypatch.setattr(trading, "build_portfolio_snapshot", snapshot_down)
    monkeypatch.setattr(trading, "mark_plan_superseded", mark)
    state = {"trade_plan": _plan(), "wallet_address": "wallet", "session_context": {"risk_charter": "structured", "risk_charter_fields": {"max_position_pct": 25}}}
    out = asyncio.run(trading.charter_risk_node(state))
    assert out["trade_plan"] is None and out["risk_assessment"].verdict == "unresolved" and superseded == ["plan-1"]
    assert "could not be verified" in out["answer"] and "max position 25%" in out["answer"]

    async def snapshot_ok(wallet):
        return {"total_usd_value": 1000.0, "holdings": [{"mint": "OUTmint", "usd_value": 40.0}]}   # 24% after the trade

    monkeypatch.setattr(trading, "build_portfolio_snapshot", snapshot_ok)
    ok = asyncio.run(trading.charter_risk_node(state))
    assert ok.get("trade_plan", "kept") == "kept" and ok["risk_assessment"].verdict == "ok" and "max position pct" in ok["risk_assessment"].summary


# --- 4. atomic task claims ---------------------------------------------------

def _make_task(kind="reminder", schedule=None):
    client = TestClient(main.app)
    me = sign_in(client, email=f"claim-{kind}@example.com", plan_id="max")
    user = asyncio.run(accounts.get_user(me["user"]["id"]))
    spec = {"message": "check SOL"} if kind == "reminder" else {}
    task = asyncio.run(tasks.create_task(user, kind, spec, schedule or {"every_minutes": 5}, "inapp", 0))
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    task = asyncio.run(tasks.update_task(task["id"], user["id"], next_run_at=past))
    return client, user, task


def test_concurrent_runs_deliver_an_occurrence_once():
    client, user, task = _make_task()

    async def race():
        return await asyncio.gather(tasks.run_task(dict(task)), tasks.run_task(dict(task)))

    results = asyncio.run(race())
    assert sorted(r["fired"] for r in results) == [False, True]
    assert [r for r in results if not r["fired"]][0]["result"] == "skipped: already running"
    after = asyncio.run(tasks.get_task(task["id"]))
    assert after["fire_count"] == 1 and after["next_run_at"] and after["next_run_at"] > datetime.now(timezone.utc).isoformat()
    assert len([i for i in asyncio.run(tasks.inbox(user["id"])) if i.get("task_id") == task["id"]]) == 1


def test_run_now_overlapping_the_worker_charges_a_brief_once(monkeypatch):
    monkeypatch.setattr(tasks.home_highlights, "get_highlights", lambda force=False: {"as_of": "x", "source": "news", "cards": []})
    monkeypatch.setattr(tasks.market_overview, "_get_json", lambda url: {})
    client, user, task = _make_task(kind="brief", schedule={"daily": "08:00"})
    before = client.get("/me").json()["credits"]["balance"]

    async def race():
        return await asyncio.gather(tasks.run_task(dict(task)), tasks.run_task(dict(task)))

    results = asyncio.run(race())
    assert sorted(r["fired"] for r in results) == [False, True]
    assert client.get("/me").json()["credits"]["balance"] == before - settings.credit_cost_brief
    # The worker never picks up a claimed task: it is no longer due while running.
    assert asyncio.run(tasks.due_tasks()) == [] or all(t["id"] != task["id"] for t in asyncio.run(tasks.due_tasks()))
    # A "run now" on the rescheduled task is its own occurrence and charges again, once.
    run = client.post(f"/me/tasks/{task['id']}/run").json()
    assert run["fired"] is True
    assert client.get("/me").json()["credits"]["balance"] == before - 2 * settings.credit_cost_brief
