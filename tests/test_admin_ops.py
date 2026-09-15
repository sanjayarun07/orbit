"""Admin & ops: account search and detail, support actions (plan, credits,
key revocation, sign-out-everywhere), Stripe event list + replay, and the
business metrics (MRR, conversion, credits burned per feature, failed
payments, sign-ups)."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import accounts, billing, credits, main
from app.billing_plans import FREE, MAX, PRO
from app.graph import AgentRun
from app.settings import settings
from tests.conftest import sign_in

ADMIN = {"Authorization": "Bearer admin-secret"}


@pytest.fixture(autouse=True)
def admin_key(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-secret")


@pytest.fixture
def fake_agent(monkeypatch):
    async def fake_run(message, wallet, history, session_context, action):
        trajectory = {"tool_name_0": "birdeye_token_overview", "observation_0": "ok"} if message.startswith("price") else None
        return AgentRun(answer="ok", trajectory=trajectory, trade_plan=None, intent="general", capabilities=[])

    monkeypatch.setattr(main, "run_agent", fake_run)


def test_admin_endpoints_need_the_admin_key():
    client = TestClient(main.app)
    assert client.get("/admin/users").status_code in (401, 403)
    assert client.get("/admin/metrics/business").status_code in (401, 403)
    assert client.get("/admin/billing/events", headers={"Authorization": "Bearer nope"}).status_code in (401, 403)


def test_search_and_detail_show_what_support_needs(fake_agent):
    client = TestClient(main.app)
    sign_in(client, email="alice@example.com", plan_id="pro")
    client.post("/chat", json={"message": "price of BONK"})
    client.post("/me/api-keys", json={"name": "ci"})
    other = TestClient(main.app)
    sign_in(other, email="bob@example.com")
    admin = TestClient(main.app, headers=ADMIN)

    users = admin.get("/admin/users?q=alice").json()["users"]
    assert [u["email"] for u in users] == ["alice@example.com"] and users[0]["plan_id"] == "pro"
    assert users[0]["balance"] == FREE.monthly_credits + PRO.monthly_credits - settings.credit_cost_tool_turn
    assert len(admin.get("/admin/users").json()["users"]) == 2

    detail = admin.get("/admin/users/alice@example.com").json()
    assert detail["plan"]["id"] == "pro" and detail["conversations"] == 1 and len(detail["api_keys"]) == 1
    assert detail["usage"]["by_kind"] == {"tool_turn": settings.credit_cost_tool_turn}
    assert len(detail["sessions"]) == 1 and any(r["reason"] == "settle" for r in detail["credits"]["ledger"])
    assert admin.get("/admin/users/nobody@example.com").status_code == 404

    # Support actions.
    key_id = detail["api_keys"][0]["id"]
    assert admin.post(f"/admin/users/alice@example.com/api-keys/{key_id}/revoke").json() == {"revoked": True}
    assert admin.post(f"/admin/users/alice@example.com/api-keys/{key_id}/revoke").status_code == 404
    assert admin.post("/admin/users/alice@example.com/sessions/revoke").json() == {"revoked": 1}
    assert client.get("/me").json()["authenticated"] is False


def test_business_metrics_roll_up_plans_credits_and_conversion(fake_agent, monkeypatch):
    a, b, c = TestClient(main.app), TestClient(main.app), TestClient(main.app)
    sign_in(a, email="a@example.com", plan_id="pro")
    sign_in(b, email="b@example.com", plan_id="max")
    sign_in(c, email="c@example.com")
    asyncio.run(accounts.update_user((b.get("/me").json())["user"]["id"], subscription_status="past_due"))
    a.post("/chat", json={"message": "hello"})
    a.post("/chat", json={"message": "price of BONK"})
    c.post("/chat", json={"message": "hello"})
    admin = TestClient(main.app, headers=ADMIN)
    m = admin.get("/admin/metrics/business").json()
    assert m["users"] == {"total": 3, "paid": 2, "free": 1, "by_plan": {"pro": 1, "max": 1, "free": 1}, "team_members": 0, "conversion_pct": 66.67}
    assert m["mrr_usd"] == PRO.price_usd_month + MAX.price_usd_month
    assert m["failed_payments"] == 1 and m["subscriptions"]["past_due"] == 1
    assert m["credits"]["burned_by_kind"] == {"chat": 2 * settings.credit_cost_chat_turn, "tool_turn": settings.credit_cost_tool_turn}
    assert m["credits"]["burned_total"] == 2 * settings.credit_cost_chat_turn + settings.credit_cost_tool_turn
    assert m["credits"]["active_accounts"] == 2 and m["credits"]["turns"] == 3
    assert m["credits"]["granted_by_reason"]["monthly_grant"] == 2 * FREE.monthly_credits + FREE.monthly_credits + PRO.monthly_credits + MAX.monthly_credits
    assert sum(m["signups_by_day"].values()) == 3
    assert m["stripe"]["configured"] is False and m["stripe"]["recent_events"] == []


def test_stripe_events_are_listed_and_can_be_replayed(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")

    async def no_pool():
        return None

    monkeypatch.setattr(billing, "get_pg_pool", no_pool)
    client = TestClient(main.app)
    me = sign_in(client, email="pay@example.com")
    user_id = me["user"]["id"]
    session = {"id": "cs_1", "mode": "payment", "payment_status": "paid", "customer": "cus_1", "amount_total": 600, "currency": "usd",
               "metadata": {"user_id": user_id, "kind": "pack", "pack_id": "pack_500", "credits": "500"}}
    event = {"id": "evt_replay1", "type": "checkout.session.completed", "data": {"object": session}}
    asyncio.run(billing.handle_event(event))
    admin = TestClient(main.app, headers=ADMIN)
    events = admin.get("/admin/billing/events").json()
    assert events["configured"] is True and events["events"][0]["id"] == "evt_replay1"

    monkeypatch.setattr(billing, "_api_retrieve_event", lambda event_id: event if event_id == "evt_replay1" else (_ for _ in ()).throw(RuntimeError("no such event")))
    replay = admin.post("/admin/billing/events/evt_replay1/replay").json()
    assert replay["replayed"] is True and replay["status"] == "duplicate"  # the ledger row already exists: no double credit
    assert client.get("/me").json()["credits"]["balance"] == FREE.monthly_credits + 500
    assert admin.post("/admin/billing/events/evt_missing/replay").status_code == 502
    assert admin.post("/admin/billing/events/not-an-id/replay").status_code == 400
