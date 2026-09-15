"""Accounts, credits and API keys: the magic-link flow, the anonymous trial,
per-turn charging with refund of the unused reservation, the 402 when credits
run out, sign-in gates, preferences, and per-user API keys on /chat and /mcp."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import accounts, api_keys, credits, main
from app.billing_plans import ANONYMOUS, FREE, PRO
from app.graph import AgentRun
from app.settings import settings
from tests.conftest import sign_in


@pytest.fixture
def fake_agent(monkeypatch):
    calls = []

    async def fake_run(message, wallet, history, session_context, action):
        calls.append(message)
        trajectory = {"tool_name_0": "birdeye_token_overview"} if message.startswith("price") else None
        team = {"coordinator": "ok"} if message.startswith("desk") else None
        return AgentRun(answer="ok", trajectory=trajectory, trade_plan=None, intent="general", capabilities=[], team_report=team)

    monkeypatch.setattr(main, "run_agent", fake_run)
    return calls


def test_magic_link_signs_in_and_grants_the_monthly_allowance():
    client = TestClient(main.app)
    anonymous = client.get("/me").json()
    assert anonymous["authenticated"] is False and anonymous["plan"]["id"] == "anonymous"
    assert anonymous["credits"]["balance"] == ANONYMOUS.trial_credits

    started = client.post("/auth/email/start", json={"email": "Someone@Example.com "}).json()
    assert started["sent"] is False and started["email"] == "someone@example.com" and "signin=" in started["dev_link"]
    token = started["dev_link"].rsplit("signin=", 1)[1]
    verified = client.post("/auth/email/verify", json={"token": token})
    assert verified.status_code == 200 and "HttpOnly" in verified.headers["set-cookie"]
    body = verified.json()
    assert body["authenticated"] is True and body["created"] is True and body["plan"]["id"] == "free"
    assert body["credits"]["balance"] == FREE.monthly_credits
    # The link is single use.
    assert client.post("/auth/email/verify", json={"token": token}).status_code == 401
    # The allowance is granted once per month, not on every request.
    assert client.get("/me").json()["credits"]["balance"] == FREE.monthly_credits
    assert client.post("/auth/signout").json() == {"authenticated": False}
    assert client.get("/me").json()["authenticated"] is False


def test_invalid_email_is_rejected():
    client = TestClient(main.app)
    assert client.post("/auth/email/start", json={"email": "not-an-email"}).status_code == 400


def test_turns_are_charged_by_what_they_did_and_unused_reservation_is_refunded(fake_agent):
    client = TestClient(main.app)
    sign_in(client)
    start = client.get("/me").json()["credits"]["balance"]
    plain = client.post("/chat", json={"message": "hello"}).json()
    assert plain["credits"] == {"charged": settings.credit_cost_chat_turn, "kind": "chat", "balance": start - 1}
    tool = client.post("/chat", json={"message": "price of BONK"}).json()
    assert tool["credits"]["kind"] == "tool_turn" and tool["credits"]["charged"] == settings.credit_cost_tool_turn
    desk = client.post("/chat", json={"message": "desk: TVL"}).json()
    assert desk["credits"]["kind"] == "team_desk" and desk["credits"]["charged"] == settings.credit_cost_team_turn
    expected = start - settings.credit_cost_chat_turn - settings.credit_cost_tool_turn - settings.credit_cost_team_turn
    assert client.get("/me/credits").json()["balance"] == expected
    ledger = client.get("/me/credits").json()["ledger"]
    assert {row["reason"] for row in ledger} >= {"monthly_grant", "reserve", "settle"}


def test_failed_turn_costs_nothing(monkeypatch):
    async def boom(message, wallet, history, session_context, action):
        raise RuntimeError("provider down")

    monkeypatch.setattr(main, "run_agent", boom)
    client = TestClient(main.app)
    sign_in(client)
    before = client.get("/me").json()["credits"]["balance"]
    assert client.post("/chat", json={"message": "hello"}).status_code == 500
    assert client.get("/me").json()["credits"]["balance"] == before


def test_anonymous_trial_runs_out_with_a_402_that_points_to_sign_in(fake_agent, monkeypatch):
    client = TestClient(main.app, headers={"X-Orbit-Device": "device-A"})
    for _ in range(ANONYMOUS.trial_credits):
        assert client.post("/chat", json={"message": "hello"}).status_code == 200
    out = client.post("/chat", json={"message": "hello"})
    assert out.status_code == 402
    detail = out.json()["detail"]
    assert detail["error"] == "insufficient_credits" and detail["signed_in"] is False and detail["balance"] == 0
    # A different device (same IP) is its own trial; the same device is not reset by a reload.
    other = TestClient(main.app, headers={"X-Orbit-Device": "device-B"})
    assert other.post("/chat", json={"message": "hello"}).status_code == 200
    assert client.post("/chat", json={"message": "hello"}).status_code == 402


def test_sign_in_gates_history_wallets_and_portfolio(fake_agent):
    client = TestClient(main.app)
    assert client.get("/chat/history/abc").status_code == 401
    assert client.get("/chat/history/abc").json()["detail"]["error"] == "sign_in_required"
    assert client.get("/portfolio/5CEbueQnq1Ym2uSSx2xXds3jQAqT1BDnkA59RZobSPAG").status_code == 401
    gated = client.post("/chat", json={"message": "hello", "wallet_address": "5CEbueQnq1Ym2uSSx2xXds3jQAqT1BDnkA59RZobSPAG"})
    assert gated.status_code == 401 and gated.json()["detail"]["error"] == "sign_in_required"
    # Anonymous research without a wallet still works on the trial.
    assert client.post("/chat", json={"message": "hello"}).status_code == 200


def test_preferences_are_stored_server_side():
    client = TestClient(main.app)
    sign_in(client)
    updated = client.put("/me/preferences", json={"display_name": "  Aru ", "risk_profile": "conservative", "theme": "dark"}).json()
    assert updated["user"]["display_name"] == "Aru"
    assert updated["user"]["preferences"] == {"risk_profile": "conservative", "theme": "dark"}
    again = client.put("/me/preferences", json={"low_credit_alert": True}).json()
    assert again["user"]["preferences"] == {"risk_profile": "conservative", "theme": "dark", "low_credit_alert": True}


def test_api_keys_need_a_paid_plan_and_authenticate_chat(fake_agent):
    client = TestClient(main.app)
    sign_in(client)
    assert client.post("/me/api-keys", json={"name": "ci"}).status_code == 403  # Free plan
    sign_in(client, plan_id="pro")
    created = client.post("/me/api-keys", json={"name": "ci", "scopes": ["chat", "data"]})
    assert created.status_code == 201
    secret, record = created.json()["secret"], created.json()["key"]
    assert secret.startswith(api_keys.KEY_PREFIX) and record["prefix"] == secret[:15] and record["scopes"] == ["chat", "data"]
    listed = client.get("/me/api-keys").json()["keys"]
    assert [k["id"] for k in listed] == [record["id"]] and "secret" not in listed[0]

    api_client = TestClient(main.app, headers={"Authorization": f"Bearer {secret}"})
    me = api_client.get("/me").json()
    assert me["authenticated"] is True and me["api_key"]["name"] == "ci" and me["plan"]["id"] == "pro"
    turn = api_client.post("/chat", json={"message": "hello"})
    assert turn.status_code == 200 and turn.json()["credits"]["charged"] == 1
    # Keys are managed from a browser session, never from another key.
    assert api_client.post("/me/api-keys", json={"name": "x"}).status_code == 403
    # Revoked keys stop working immediately.
    assert client.delete(f"/me/api-keys/{record['id']}").json() == {"revoked": True}
    assert api_client.post("/chat", json={"message": "hello"}).status_code == 401
    assert TestClient(main.app, headers={"Authorization": "Bearer orb_live_nope"}).get("/me").status_code == 401


def test_api_key_scope_is_enforced(fake_agent):
    client = TestClient(main.app)
    sign_in(client, plan_id="max")
    secret = client.post("/me/api-keys", json={"name": "data-only", "scopes": ["data"]}).json()["secret"]
    api_client = TestClient(main.app, headers={"Authorization": f"Bearer {secret}"})
    assert api_client.post("/chat", json={"message": "hello"}).status_code == 403


def test_mcp_accepts_a_user_key_and_refuses_free_plans(monkeypatch):
    monkeypatch.setattr(settings, "mcp_api_key", "global-secret")
    # Without the app lifespan the MCP session manager isn't running, so an
    # admitted request fails downstream (500) -- what matters here is that the
    # admission middleware let it through (never 401/403).
    client = TestClient(main.app, raise_server_exceptions=False)
    sign_in(client, plan_id="pro")
    secret = client.post("/me/api-keys", json={"name": "claude", "scopes": ["mcp", "chat"]}).json()["secret"]
    # The global key still works; a user key with the mcp scope is admitted as that user.
    assert client.post("/mcp", json={}, headers={"Authorization": "Bearer global-secret"}).status_code not in (401, 403)
    assert client.post("/mcp", json={}, headers={"Authorization": f"Bearer {secret}"}).status_code not in (401, 403)
    assert client.post("/mcp", json={}, headers={"Authorization": "Bearer orb_live_bogus"}).status_code == 401
    no_scope = client.post("/me/api-keys", json={"name": "chat-only", "scopes": ["chat"]}).json()["secret"]
    assert client.post("/mcp", json={}, headers={"Authorization": f"Bearer {no_scope}"}).status_code == 403


def test_ledger_grants_are_idempotent_by_reference():
    account = credits.user_account_id("u1")
    assert asyncio.run(credits.grant(account, 500, "purchase", "stripe_event", "evt_1")) is True
    assert asyncio.run(credits.grant(account, 500, "purchase", "stripe_event", "evt_1")) is False  # redelivered webhook
    assert asyncio.run(credits.balance(account)) == 500
    asyncio.run(credits.ensure_monthly_grant(account, PRO))
    asyncio.run(credits.ensure_monthly_grant(account, PRO))
    assert asyncio.run(credits.balance(account)) == 500 + PRO.monthly_credits


def test_reserve_never_overdraws_and_release_refunds_all():
    account = credits.user_account_id("u2")
    asyncio.run(credits.grant(account, 2, "test", "grant", "t"))
    reserved = asyncio.run(credits.reserve(account, "turn-1"))
    assert reserved == 2 and asyncio.run(credits.balance(account)) == 0
    asyncio.run(credits.release(account, "turn-1", reserved))
    assert asyncio.run(credits.balance(account)) == 2
    asyncio.run(credits.settle(account, "turn-2", asyncio.run(credits.reserve(account, "turn-2")), 1, "chat"))
    assert asyncio.run(credits.balance(account)) == 1
    asyncio.run(credits.reserve(account, "turn-3"))
    with pytest.raises(credits.InsufficientCredits):
        asyncio.run(credits.reserve(account, "turn-4"))


def test_billing_catalog_lists_the_three_plans():
    plans = TestClient(main.app).get("/billing/plans").json()
    assert [p["id"] for p in plans["plans"]] == ["free", "pro", "max"]
    assert [p["monthly_credits"] for p in plans["plans"]] == [100, 2000, 7500]
    assert [p["price_usd_month"] for p in plans["plans"]] == [0.0, 19.0, 49.0]
    assert plans["trial_credits"] == 10


def test_admin_can_set_plan_and_grant_credits_idempotently(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-secret")
    client = TestClient(main.app)
    sign_in(client, email="vip@example.com")
    admin = {"Authorization": "Bearer admin-secret"}
    assert client.put("/admin/users/vip@example.com/plan", json={"plan_id": "pro"}, headers=admin).json()["plan_id"] == "pro"
    assert client.put("/admin/users/nobody@example.com/plan", json={"plan_id": "pro"}, headers=admin).status_code == 404
    assert client.put("/admin/users/vip@example.com/plan", json={"plan_id": "anonymous"}, headers=admin).status_code == 400
    assert client.put("/admin/users/vip@example.com/plan", json={"plan_id": "pro"}).status_code in (401, 403)
    me = client.get("/me").json()
    assert me["plan"]["id"] == "pro" and me["credits"]["balance"] == FREE.monthly_credits + PRO.monthly_credits
    first = client.post("/admin/users/vip@example.com/credits", json={"amount": 250, "reason": "comp", "reference": "ticket-1"}, headers=admin).json()
    again = client.post("/admin/users/vip@example.com/credits", json={"amount": 250, "reason": "comp", "reference": "ticket-1"}, headers=admin).json()
    assert first["applied"] is True and again["applied"] is False and again["balance"] == first["balance"]
    assert client.get("/me").json()["credits"]["balance"] == me["credits"]["balance"] + 250
