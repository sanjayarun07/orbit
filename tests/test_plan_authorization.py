"""Trade-plan authorization has to mean the same thing at every entry point.

Three reproductions, each of a real defect found reviewing 466fbcc4:

1. Plan ownership was keyed on `identity.account_id`, which is the *billing*
   account. Team members are billed to their owner's account, so every member
   of a team shared one ownership key and could view and execute each other's
   plans. Who pays and who owns are different questions.
2. The MCP tool returned the whole plan, `confirmation_text` included, with no
   ownership check at all -- so the HTTP gate was reachable around.
3. The Relay execution-claim endpoint had no deployment-mode check, so a
   research deployment still let the browser claim a Relay execution and go on
   to wallet approval.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import accounts, plan_access
from app.identity import Identity
from app.main import app
from app.models import SwapProposal, TokenInfo, TradePlan
from app.settings import settings
from tests.conftest import sign_in


def _plan(plan_id: str, owner: str | None) -> TradePlan:
    now = datetime.now(timezone.utc)
    return TradePlan(
        plan_id=plan_id, status="pending_confirmation", created_at=now,
        expires_at=now + timedelta(minutes=10), wallet_address="OwnerWallet",
        proposal=SwapProposal(
            input_mint="So11111111111111111111111111111111111111112",
            output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            amount_atomic=1000, slippage_bps=50, reason="fixture"),
        quote={},
        input_token=TokenInfo(mint="So11111111111111111111111111111111111111112", symbol="SOL", name="Solana", decimals=9),
        output_token=TokenInfo(mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", symbol="USDC", name="USD Coin", decimals=6),
        simulation={"ok": True}, confirmation_text=f"CONFIRM {plan_id}",
        owner_account_id=owner,
    )


def _identity(user_id: str, billed_account: str) -> Identity:
    """A signed-in caller whose billing account may belong to someone else --
    exactly the shape a team member has."""
    return Identity("user", billed_account, "127.0.0.1", user={"id": user_id})


# --- 1. who pays is not who owns ---------------------------------------------

def test_two_team_members_do_not_share_a_plan_owner():
    """Both members bill to the owner's account. That must not make one
    member's plan reachable by the other."""
    shared_billing = "user:team-owner"
    alice = _identity("alice", shared_billing)
    bob = _identity("bob", shared_billing)

    assert alice.account_id == bob.account_id, "fixture must reproduce the shared billing account"
    assert alice.principal_id != bob.principal_id, "a teammate must not be the same principal"


def test_a_teammate_cannot_reach_another_teammates_plan(monkeypatch):
    shared_billing = "user:team-owner"
    alice = _identity("alice", shared_billing)
    bob = _identity("bob", shared_billing)
    plan = _plan("plan-quoted-by-alice", alice.principal_id)

    async def fake_get_plan(plan_id):
        if plan_id == plan.plan_id:
            return plan
        raise KeyError(plan_id)

    # One loader, patched in one place -- plan_access reads plans.get_plan as a
    # module attribute precisely so it cannot diverge from what a handler reads.
    monkeypatch.setattr("app.plans.get_plan", fake_get_plan)

    assert asyncio.run(plan_access.require_plan_access(plan.plan_id, alice)) is not None
    with pytest.raises(plan_access.PlanAccessDenied):
        asyncio.run(plan_access.require_plan_access(plan.plan_id, bob))


def test_the_owner_of_a_plan_is_the_caller_not_the_payer(monkeypatch):
    """The stamped owner must be the individual, so a plan quoted by a team
    member belongs to that member."""
    from app import plans

    token = plans.set_plan_owner(_identity("alice", "user:team-owner").principal_id)
    try:
        assert plans.plan_owner() == "user:alice"
    finally:
        plans.reset_plan_owner(token)


def test_an_anonymous_caller_is_still_its_own_principal():
    anonymous = Identity("anonymous", "anon:device-abc", "127.0.0.1")
    assert anonymous.principal_id == "anon:device-abc"


# --- 2. MCP goes through the same gate ---------------------------------------

def test_mcp_refuses_a_plan_the_caller_does_not_own(monkeypatch):
    """orbit_trade_plan used to return the whole plan, confirmation text and
    all, to anyone who knew the id -- around the HTTP gate entirely."""
    from app import mcp_server, plans
    from app.identity import current_identity

    owner = _identity("alice", "user:alice")
    intruder = _identity("mallory", "user:mallory")
    plan = _plan("plan-for-mcp", owner.principal_id)

    async def fake_get_plan(plan_id):
        if plan_id == plan.plan_id:
            return plan
        raise KeyError(plan_id)

    monkeypatch.setattr("app.plans.get_plan", fake_get_plan)

    token = current_identity.set(intruder)
    try:
        refused = asyncio.run(mcp_server.orbit_trade_plan(plan.plan_id))
    finally:
        current_identity.reset(token)
    assert refused.get("status") == 404
    assert "CONFIRM" not in str(refused), "the confirmation text leaked to a non-owner"

    token = current_identity.set(owner)
    try:
        allowed = asyncio.run(mcp_server.orbit_trade_plan(plan.plan_id))
    finally:
        current_identity.reset(token)
    assert allowed["plan_id"] == plan.plan_id
    assert allowed["confirmation_text"] == plan.confirmation_text


def test_mcp_and_http_share_one_authorization_function():
    """Two copies of a rule drift apart; this is the structural guard against
    the next entry point being added without the check."""
    import inspect

    from app import main, mcp_server

    assert "plan_access" in inspect.getsource(main._require_plan_access)
    assert "plan_access" in inspect.getsource(mcp_server.orbit_trade_plan)


# --- 3. research mode covers the Relay claim ---------------------------------

def test_research_mode_refuses_the_relay_execution_claim(monkeypatch):
    """Claiming a Relay execution is the step that precedes wallet approval.
    Without a mode check the browser could still walk into a live swap."""
    monkeypatch.setattr(settings, "deployment_mode", "research")
    monkeypatch.setattr(settings, "live_trading", False)
    client = TestClient(app)
    response = client.post("/executions/relay/" + "r" * 24, json={})
    assert response.status_code == 403
    assert "research mode" in response.json()["detail"]


def test_execution_mode_still_allows_the_relay_claim_past_the_gate(monkeypatch):
    """The gate must not be a permanent refusal. The tracking store is stubbed
    so this proves the request reached the handler, nothing more."""
    from app import relay_tracking

    async def _track(request_id, session_id=None, revision=None):
        return {"request_id": request_id, "status": "pending"}

    monkeypatch.setattr(settings, "deployment_mode", "execution")
    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(relay_tracking, "track", _track)
    client = TestClient(app)
    response = client.post("/executions/relay/" + "r" * 24, json={})
    assert response.status_code == 200


def test_reading_relay_settlement_status_is_not_gated(monkeypatch):
    """Status is how an operator reconciles a swap that already happened.
    Switching a deployment to research mode must not blind it to those."""
    from app import relay_tracking

    async def _status(request_id):
        return {"request_id": request_id, "status": "success"}

    monkeypatch.setattr(settings, "deployment_mode", "research")
    monkeypatch.setattr(relay_tracking, "get_status", _status)
    client = TestClient(app)
    response = client.get("/executions/relay/" + "r" * 24)
    assert response.status_code == 200
