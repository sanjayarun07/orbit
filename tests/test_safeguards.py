"""Safeguards the review found missing outside the trading core:

1. Conversations belong to the account that used them: another signed-in
   user cannot read, extend or delete one by knowing its id, and ownership
   never transfers.
2. The risk charter gates Relay (cross-chain) drafts like Jupiter plans.
3. A charter rule that cannot be evaluated is reported as unresolved, not
   passed; the position limit measures post-trade exposure.
4. A scheduled occurrence is claimed atomically: concurrent workers, or a
   worker overlapping "run now", deliver and charge it once.
5. The MCP server applies the same ownership rule to every session operation.
6. Credit holds are atomic per account: concurrent turns cannot overspend.
7. Only subscription invoices grant the monthly allowance; a credit-pack
   invoice grants nothing beyond the pack itself.
8. A running task holds a lease: "run now" cannot re-claim it, a failed run is
   rescheduled, and a claim whose lease expired is recovered.
9. A conversation claim that loses a race is denied, and a chat turn claims
   the conversation before doing any work.
10. Recovering an expired claim reuses the persisted occurrence, so a run that
    died after charging never charges twice.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import execution_policy
from app import accounts, billing, credits, main, mcp_server, tasks
from app.identity import current_identity
from app.graph import AgentRun
from app.nodes import trading
from app.settings import settings
from tests.conftest import sign_in


@pytest.fixture
def fake_agent(monkeypatch):
    async def fake_run(message, wallet, history, session_context, action):
        return AgentRun(answer="ok", trajectory=None, trade_plan=None, intent="general", capabilities=[])

    monkeypatch.setattr(execution_policy, "run_agent", fake_run)


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


# --- 5. MCP ownership ---------------------------------------------------------

def _mcp_identity(client):
    """The Identity the MCP admission middleware would resolve for this signed-in client."""
    from app.identity import Identity
    from app.billing_plans import get_plan
    me = client.get("/me").json()
    user = asyncio.run(accounts.get_user(me["user"]["id"]))
    return Identity(kind="user", account_id=user["id"], ip="test", user=user, plan=get_plan(user.get("plan_id")))


def test_mcp_session_tools_apply_the_ownership_rule(fake_agent):
    alice, bob = TestClient(main.app), TestClient(main.app)
    sign_in(alice, email="mcp-alice@example.com")
    sign_in(bob, email="mcp-bob@example.com")
    sid = alice.post("/chat", json={"message": "hello"}).json()["session_id"]
    token = current_identity.set(_mcp_identity(bob))
    try:
        assert asyncio.run(mcp_server.orbit_history(sid)) == {"error": "Conversation not found", "status": 404}
        assert asyncio.run(mcp_server.orbit_delete_history(sid))["status"] == 404
        assert asyncio.run(mcp_server.orbit_policy(sid))["status"] == 404
        assert asyncio.run(mcp_server.orbit_handoff_url(sid))["status"] == 404
        assert asyncio.run(mcp_server.orbit_connect_wallet(sid, "0x1111111111111111111111111111111111111111"))["status"] == 404
        assert asyncio.run(mcp_server._turn("hijack", sid, None))["status"] == 404
        with pytest.raises(Exception):
            asyncio.run(mcp_server.history_resource(sid))
        with pytest.raises(Exception):
            asyncio.run(mcp_server.policy_resource(sid))
    finally:
        current_identity.reset(token)
    # Nothing leaked or changed: Alice still owns an intact conversation with no wallet bound.
    history = alice.get(f"/chat/history/{sid}").json()
    assert len(history["messages"]) == 2 and not history["context"].get("mcp_wallet_address")
    # The owner's own MCP calls work, and an unowned session is claimed by the MCP caller too.
    token = current_identity.set(_mcp_identity(alice))
    try:
        assert len(asyncio.run(mcp_server.orbit_history(sid))["messages"]) == 2
        fresh = asyncio.run(mcp_server.orbit_connect_wallet(None, "0x1111111111111111111111111111111111111111"))["session_id"]
    finally:
        current_identity.reset(token)
    assert asyncio.run(accounts.chat_session_owner(fresh)) == alice.get("/me").json()["user"]["id"]
    assert bob.get(f"/chat/history/{fresh}").status_code == 404


# --- 6. atomic credit holds ---------------------------------------------------

def test_concurrent_reserves_cannot_overspend():
    client = TestClient(main.app)
    me = sign_in(client, email="spender@example.com")
    account = me["user"]["id"]
    balance = asyncio.run(credits.balance(account))
    hold = max(balance // 2 + 1, settings.credit_cost_chat_turn)      # two full holds would exceed the balance

    async def race():
        return await asyncio.gather(*(credits.reserve(account, f"turn-{i}", amount=hold) for i in range(6)), return_exceptions=True)

    results = asyncio.run(race())
    held = [r for r in results if isinstance(r, int)]
    assert sum(held) <= balance and asyncio.run(credits.balance(account)) == balance - sum(held) >= 0
    assert any(isinstance(r, credits.InsufficientCredits) for r in results)


# --- 8. task leases and failure recovery ------------------------------------

def test_run_now_cannot_reclaim_a_running_task_and_a_stale_lease_is_recovered():
    client, user, task = _make_task(kind="reminder")
    running = asyncio.run(tasks.claim_task(dict(task)))
    assert running is not None and running["claimed_until"]
    # While the lease holds, neither the worker nor "run now" can take it again.
    assert all(t["id"] != task["id"] for t in asyncio.run(tasks.due_tasks()))
    assert asyncio.run(tasks.run_task(asyncio.run(tasks.get_task(task["id"]))))["result"] == "skipped: already running"
    assert client.post(f"/me/tasks/{task['id']}/run").json()["result"] == "skipped: already running"
    # The worker died: once the lease expires the occurrence is due again and runs.
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    asyncio.run(tasks.update_task(task["id"], user["id"], claimed_until=expired))
    assert any(t["id"] == task["id"] for t in asyncio.run(tasks.due_tasks()))
    result = asyncio.run(tasks.run_task(asyncio.run(tasks.get_task(task["id"]))))
    assert result["fired"] is True
    after = asyncio.run(tasks.get_task(task["id"]))
    assert after["claimed_until"] is None and after["next_run_at"] > datetime.now(timezone.utc).isoformat()


def test_a_failed_run_is_rescheduled_and_releases_its_lease(monkeypatch):
    client, user, task = _make_task(kind="reminder")
    real_evaluate = tasks.evaluate

    async def boom(task):
        raise RuntimeError("provider down")

    monkeypatch.setattr(tasks, "evaluate", boom)
    result = asyncio.run(tasks.run_task(dict(task)))
    assert result == {"id": task["id"], "fired": False, "result": "error", "status": "active"}
    after = asyncio.run(tasks.get_task(task["id"]))
    assert after["last_result"].startswith("error: provider down") and after["claimed_until"] is None
    assert after["next_run_at"] and after["next_run_at"] > datetime.now(timezone.utc).isoformat()
    # It is no longer stuck: the next occurrence runs normally.
    monkeypatch.setattr(tasks, "evaluate", real_evaluate)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    asyncio.run(tasks.update_task(task["id"], user["id"], next_run_at=past))
    assert asyncio.run(tasks.run_task(asyncio.run(tasks.get_task(task["id"]))))["fired"] is True


# --- 9. losing claims are denied; turns claim first -------------------------

def test_a_losing_claim_is_denied_and_a_turn_claims_first(fake_agent, monkeypatch):
    from app import session_access
    alice, bob = TestClient(main.app), TestClient(main.app)
    sign_in(alice, email="race-alice@example.com")
    sign_in(bob, email="race-bob@example.com")
    sid = TestClient(main.app).post("/chat", json={"message": "hello"}).json()["session_id"]   # unowned
    a_id, b_id = _mcp_identity(alice), _mcp_identity(bob)

    async def race():
        return await asyncio.gather(session_access.require_session_access(sid, a_id, claim=True),
                                    session_access.require_session_access(sid, b_id, claim=True), return_exceptions=True)

    results = asyncio.run(race())
    assert sum(r is None for r in results) == 1 and sum(isinstance(r, session_access.SessionAccessDenied) for r in results) == 1
    owner = asyncio.run(accounts.chat_session_owner(sid))
    assert owner in (a_id.user["id"], b_id.user["id"])
    # The store refusing a claim (another caller won between the read and the write) is a denial, never an admission.
    fresh = TestClient(main.app).post("/chat", json={"message": "hello"}).json()["session_id"]

    async def lost(user_id, session_id):
        return False

    real_touch = accounts.touch_chat_session
    monkeypatch.setattr(accounts, "touch_chat_session", lost)
    with pytest.raises(session_access.SessionAccessDenied):
        asyncio.run(session_access.require_session_access(fresh, a_id, claim=True))
    monkeypatch.setattr(accounts, "touch_chat_session", real_touch)
    # A signed-in chat turn on an unowned conversation claims it before the turn runs.
    assert alice.post("/chat", json={"message": "mine now", "session_id": fresh}).status_code == 200
    assert asyncio.run(accounts.chat_session_owner(fresh)) == a_id.user["id"]
    assert bob.post("/chat", json={"message": "hijack", "session_id": fresh}).status_code == 404


# --- 10. recovery reuses the occurrence ---------------------------------------

def test_recovering_an_expired_claim_charges_the_occurrence_once(monkeypatch):
    monkeypatch.setattr(tasks.home_highlights, "get_highlights", lambda force=False: {"as_of": "x", "source": "news", "cards": []})
    monkeypatch.setattr(tasks.market_overview, "_get_json", lambda url: {})
    client, user, task = _make_task(kind="brief", schedule={"daily": "08:00"})
    before = client.get("/me").json()["credits"]["balance"]
    # The worker claimed the occurrence, charged the brief, then died before delivering.
    running = asyncio.run(tasks.claim_task(dict(task)))
    assert running["claimed_occurrence"] == running["occurrence"] == task["next_run_at"]
    account = credits.user_account_id(user["id"])
    asyncio.run(credits.append(account, -settings.credit_cost_brief, "task:brief", "task_run", f"{task['id']}:{running['occurrence']}", {"kind": "brief"}))
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    asyncio.run(tasks.update_task(task["id"], user["id"], claimed_until=expired))
    # Recovery delivers the same occurrence and finds its charge already in the ledger.
    recovered = asyncio.run(tasks.run_task(asyncio.run(tasks.get_task(task["id"]))))
    assert recovered["fired"] is True
    assert client.get("/me").json()["credits"]["balance"] == before - settings.credit_cost_brief
    after = asyncio.run(tasks.get_task(task["id"]))
    assert after["claimed_occurrence"] is None and after["claimed_until"] is None and after["next_run_at"]


def test_a_paid_occurrence_is_delivered_on_recovery_even_with_no_credits_left(monkeypatch):
    monkeypatch.setattr(tasks.home_highlights, "get_highlights", lambda force=False: {"as_of": "x", "source": "news", "cards": []})
    monkeypatch.setattr(tasks.market_overview, "_get_json", lambda url: {})
    client, user, task = _make_task(kind="brief", schedule={"daily": "08:00"})
    account = credits.user_account_id(user["id"])
    running = asyncio.run(tasks.claim_task(dict(task)))
    # The run charged the brief with the user's last credits, then died before delivering.
    asyncio.run(credits.append(account, -settings.credit_cost_brief, "task:brief", "task_run", f"{task['id']}:{running['occurrence']}", {"kind": "brief"}))
    remaining = asyncio.run(credits.balance(account))
    asyncio.run(credits.append(account, -remaining, "drain", "test", "drain"))
    assert asyncio.run(credits.balance(account)) == 0
    asyncio.run(tasks.update_task(task["id"], user["id"], claimed_until=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
    recovered = asyncio.run(tasks.run_task(asyncio.run(tasks.get_task(task["id"]))))
    assert recovered["fired"] is True and recovered["result"] != "skipped: out of credits"
    assert len([i for i in asyncio.run(tasks.inbox(user["id"])) if i.get("task_id") == task["id"]]) == 1
    assert asyncio.run(credits.balance(account)) == 0          # nothing charged twice
    after = asyncio.run(tasks.get_task(task["id"]))
    assert after["fire_count"] == 1 and after["claimed_occurrence"] is None and after["next_run_at"]


# --- 11. history lists the signed-in account's own conversations -----------

def test_conversations_list_is_per_account(fake_agent):
    alice, bob = TestClient(main.app), TestClient(main.app)
    sign_in(alice, email="hist-alice@example.com")
    sign_in(bob, email="hist-bob@example.com")
    a1 = alice.post("/chat", json={"message": "what is a liquidity pool"}).json()["session_id"]
    a2 = alice.post("/chat", json={"message": "top holders of BONK\nsecond line ignored"}).json()["session_id"]
    alice.post("/chat", json={"message": "and a follow-up", "session_id": a1})
    bob.post("/chat", json={"message": "bob's own question"})
    mine = alice.get("/me/conversations").json()["conversations"]
    assert [c["session_id"] for c in mine] == [a1, a2]          # newest activity first
    assert mine[0]["title"] == "what is a liquidity pool" and mine[0]["messages"] == 4
    assert mine[1]["title"] == "top holders of BONK"
    theirs = bob.get("/me/conversations").json()["conversations"]
    assert [c["session_id"] for c in theirs] != [] and a1 not in {c["session_id"] for c in theirs}
    assert TestClient(main.app).get("/me/conversations").status_code == 401


def test_a_signed_in_accounts_history_outlives_the_anonymous_expiry(fake_agent):
    """Conversations expire two hours after the last turn -- right for a
    signed-out visitor's scratch space, fatal for the history a signed-in
    account is promised (it is why an account with hundreds of past sessions
    listed none of them). Every turn they own pushes the expiry out."""
    from app import sessions

    client = TestClient(main.app)
    sign_in(client, email="retention@example.com")
    sid = client.post("/chat", json={"message": "keep this"}).json()["session_id"]
    anon_sid = TestClient(main.app).post("/chat", json={"message": "scratch"}).json()["session_id"]

    base = settings.chat_history_ttl_seconds

    async def _ttls(keys):
        # A fresh client inside this one loop: the shared app client belongs to
        # the server's loop and cannot be awaited from here.
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            return [await client.ttl(key) for key in keys]
        finally:
            await client.aclose()

    if settings.redis_url:
        owned_ttl, anon_ttl = asyncio.run(_ttls([f"chat_history:{sid}", f"chat_history:{anon_sid}"]))
        assert owned_ttl > base, f"a signed-in account's history must outlive the {base}s scratch expiry (got {owned_ttl}s)"
        assert owned_ttl > settings.chat_history_signed_in_ttl_seconds - 120
        assert 0 < anon_ttl <= base, f"a signed-out visitor's conversation stays scratch space (got {anon_ttl}s)"
    else:
        assert sessions._retention.get(sid) == settings.chat_history_signed_in_ttl_seconds
        assert anon_sid not in sessions._retention
        aged = time.time() - (base + 60)
        sessions._last_seen[sid], sessions._last_seen[anon_sid] = aged, aged
        sessions._prune_expired()
        assert sid in sessions._sessions and anon_sid not in sessions._sessions

    assert sid in {c["session_id"] for c in client.get("/me/conversations").json()["conversations"]}
