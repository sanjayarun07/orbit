"""Settings phase 3: usage report from settle rows, sessions list / sign out
everywhere, notifications (low-credit alert, receipts), data export, delete
conversations and delete account, saved risk charter as the default for new
conversations, invoices through the Stripe shim, and team seats on Max."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import execution_policy
from app import accounts, billing, credits, emailer, main, notifications
from app.billing_plans import FREE, MAX, PRO
from app.graph import AgentRun
from app.settings import settings
from tests.conftest import sign_in


@pytest.fixture
def fake_agent(monkeypatch):
    seen = []

    async def fake_run(message, wallet, history, session_context, action):
        seen.append({"message": message, "session_context": dict(session_context or {})})
        trajectory = {"tool_name_0": "birdeye_token_overview"} if message.startswith("price") else None
        return AgentRun(answer="ok", trajectory=trajectory, trade_plan=None, intent="general", capabilities=[])

    monkeypatch.setattr(execution_policy, "run_agent", fake_run)
    return seen


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    async def fake_send(to, subject, html, text=None):
        # Magic links are reported as "not sent" (and not recorded) so the
        # sign-in helper still gets the dev link; everything else is delivered.
        if subject.startswith("Sign in to"):
            return False
        sent.append({"to": to, "subject": subject, "html": html})
        return True

    monkeypatch.setattr(emailer, "send_email", fake_send)
    return sent


def test_usage_report_groups_charges_by_day_feature_and_api_key(fake_agent):
    client = TestClient(main.app)
    sign_in(client, plan_id="pro")
    client.post("/chat", json={"message": "hello"})
    client.post("/chat", json={"message": "price of BONK"})
    secret = client.post("/me/api-keys", json={"name": "ci", "scopes": ["chat"]}).json()["secret"]
    TestClient(main.app, headers={"Authorization": f"Bearer {secret}"}).post("/chat", json={"message": "hello from key"})
    report = client.get("/me/usage?days=7").json()
    assert report["days"] == 7 and len(report["by_day"]) == 7 and report["turns"] == 3
    assert report["by_kind"] == {"chat": 2 * settings.credit_cost_chat_turn, "tool_turn": settings.credit_cost_tool_turn}
    assert report["total"] == 2 * settings.credit_cost_chat_turn + settings.credit_cost_tool_turn
    today = report["by_day"][-1]
    assert today["turns"] == 3 and today["charged"] == report["total"]
    assert report["by_api_key"] == [{"id": report["by_api_key"][0]["id"], "name": "ci", "charged": settings.credit_cost_chat_turn}]
    assert client.get("/me/usage?days=0").json()["days"] == 1  # clamped


def test_sessions_are_listed_and_can_be_revoked_everywhere_except_here():
    laptop = TestClient(main.app, headers={"User-Agent": "Laptop/1.0"})
    phone = TestClient(main.app, headers={"User-Agent": "Phone/2.0"})
    sign_in(laptop, email="multi@example.com")
    sign_in(phone, email="multi@example.com")
    sessions = laptop.get("/me/sessions").json()["sessions"]
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    assert {s["user_agent"] for s in sessions} == {"Laptop/1.0", "Phone/2.0"}
    assert all("token" not in s for s in sessions)
    assert laptop.post("/me/sessions/revoke-all").json() == {"revoked": 1}
    assert laptop.get("/me").json()["authenticated"] is True
    assert phone.get("/me").json()["authenticated"] is False


def test_low_credit_alert_fires_once_per_month_at_the_threshold(outbox, fake_agent, monkeypatch):
    client = TestClient(main.app)
    me = sign_in(client, email="low@example.com")
    prefs = client.put("/me/preferences", json={"low_credit_threshold": FREE.monthly_credits - 1}).json()["notifications"]
    assert prefs["low_credit_threshold"] == FREE.monthly_credits - 1 and prefs["low_credit_alert"] is True
    client.post("/chat", json={"message": "hello"})  # balance drops to threshold
    assert len(outbox) == 1 and "credits" in outbox[0]["subject"] and outbox[0]["to"] == "low@example.com"
    client.post("/chat", json={"message": "hello"})
    assert len(outbox) == 1  # once per month
    # Turning the alert off is respected.
    client.put("/me/preferences", json={"low_credit_alert": False})
    asyncio.run(accounts.update_user(me["user"]["id"], preferences={"low_credit_alerted_month": None}))
    client.post("/chat", json={"message": "hello"})
    assert len(outbox) == 1


def test_receipts_follow_the_preference(outbox, monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")

    async def no_pool():
        return None

    monkeypatch.setattr(billing, "get_pg_pool", no_pool)
    client = TestClient(main.app)
    me = sign_in(client, email="receipt@example.com")
    user_id = me["user"]["id"]
    session = {"id": "cs_r", "mode": "payment", "payment_status": "paid", "customer": "cus_r", "amount_total": 600, "currency": "usd",
               "metadata": {"user_id": user_id, "kind": "pack", "pack_id": "pack_500", "credits": "500"}}
    asyncio.run(billing.handle_event({"id": "evt_r1", "type": "checkout.session.completed", "data": {"object": session}}))
    assert len(outbox) == 1 and "500 credits" in outbox[0]["subject"] and "6.00 USD" in outbox[0]["html"]
    client.put("/me/preferences", json={"receipts": False})
    asyncio.run(billing.handle_event({"id": "evt_r2", "type": "checkout.session.completed", "data": {"object": session}}))
    assert len(outbox) == 1


def test_export_and_delete_conversations_cover_the_accounts_sessions(fake_agent):
    client = TestClient(main.app)
    sign_in(client, email="data@example.com")
    first = client.post("/chat", json={"message": "hello"}).json()
    client.post("/chat", json={"message": "again", "session_id": first["session_id"], "context_revision": first["session_revision"]})
    second = client.post("/chat", json={"message": "second chat"}).json()
    export = client.get("/me/export").json()
    assert export["user"]["email"] == "data@example.com"
    assert {c["session_id"] for c in export["conversations"]} == {first["session_id"], second["session_id"]}
    assert [m["content"] for m in next(c for c in export["conversations"] if c["session_id"] == first["session_id"])["messages"]] == ["hello", "ok", "again", "ok"]
    assert export["credits"]["balance"] == FREE.monthly_credits - 3 and any(r["reason"] == "settle" for r in export["credits"]["ledger"])
    assert client.delete("/me/conversations").json() == {"deleted": 2}
    assert client.get(f"/chat/history/{first['session_id']}").json()["messages"] == []
    assert client.get("/me/export").json()["conversations"] == []


def test_delete_account_requires_typed_email_and_removes_everything(fake_agent):
    client = TestClient(main.app)
    me = sign_in(client, email="gone@example.com", plan_id="pro")
    client.post("/chat", json={"message": "hello"})
    client.post("/me/api-keys", json={"name": "k"})
    assert client.request("DELETE", "/me", json={"confirm_email": "wrong@example.com"}).status_code == 400
    done = client.request("DELETE", "/me", json={"confirm_email": "GONE@example.com"})
    assert done.status_code == 200 and done.json() == {"deleted": True}
    assert client.get("/me").json()["authenticated"] is False
    assert asyncio.run(accounts.get_user(me["user"]["id"])) is None
    # The ledger stays as an anonymous financial record.
    assert asyncio.run(credits.balance(credits.user_account_id(me["user"]["id"]))) == FREE.monthly_credits + PRO.monthly_credits - 1
    # Signing in again with the same email is a brand-new account.
    fresh = sign_in(client, email="gone@example.com")
    assert fresh["created"] is True and fresh["user"]["id"] != me["user"]["id"]


def test_saved_risk_charter_seeds_new_conversations(fake_agent):
    client = TestClient(main.app)
    sign_in(client, email="charter@example.com")
    saved = client.post("/chat", json={"message": "set", "risk_charter_fields": {"max_trade_usd": 10, "verified_only": True}}).json()
    assert saved["risk_charter_fields"]["max_trade_usd"] == 10
    assert client.get("/me").json()["user"]["preferences"]["risk_charter_fields"]["max_trade_usd"] == 10
    fresh = client.post("/chat", json={"message": "hello in a new chat"}).json()
    assert fresh["risk_charter_fields"]["max_trade_usd"] == 10 and "max $10.00 per trade" in fresh["risk_charter"]
    assert fake_agent[-1]["session_context"]["risk_charter"] == fresh["risk_charter"]
    # Anonymous visitors get no default.
    anon = TestClient(main.app).post("/chat", json={"message": "hello"}).json()
    assert anon["risk_charter"] is None


def test_invoices_come_from_stripe_when_configured(monkeypatch):
    client = TestClient(main.app)
    assert sign_in(client)["billing"]["configured"] is False
    assert client.get("/me/invoices").json() == {"invoices": [], "configured": False}
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x")
    assert client.get("/me/invoices").json()["invoices"] == []  # no customer yet
    me = client.get("/me").json()
    asyncio.run(accounts.update_user(me["user"]["id"], stripe_customer_id="cus_inv"))
    monkeypatch.setattr(billing, "_api_list_invoices", lambda customer_id, limit=12: [{
        "id": "in_1", "number": "ORB-0001", "status": "paid", "amount_paid": 1900, "amount_due": 0, "currency": "usd", "created": 1_700_000_000,
        "hosted_invoice_url": "https://invoice.stripe.com/i/1", "invoice_pdf": "https://invoice.stripe.com/i/1/pdf",
        "lines": {"data": [{"description": "Orbit Pro (monthly)"}]},
    }] if customer_id == "cus_inv" else [])
    invoices = client.get("/me/invoices").json()["invoices"]
    assert invoices[0]["number"] == "ORB-0001" and invoices[0]["description"] == "Orbit Pro (monthly)" and invoices[0]["amount_paid"] == 1900


def test_team_members_share_the_owners_plan_and_credits(fake_agent, outbox):
    owner = TestClient(main.app)
    sign_in(owner, email="owner@example.com", plan_id="max")
    member = TestClient(main.app)
    sign_in(member, email="member@example.com")
    # Free users have no seats; Max owners do.
    assert member.post("/me/team/invites", json={"email": "x@example.com"}).status_code == 403
    invited = owner.post("/me/team/invites", json={"email": "Member@Example.com"})
    assert invited.status_code == 201 and invited.json()["status"] == "invited" and outbox[-1]["to"] == "member@example.com"
    assert owner.post("/me/team/invites", json={"email": "owner@example.com"}).status_code == 400
    # The invitee sees the invite on /me and accepts it.
    pending = member.get("/me").json()["team"]["invites"]
    assert pending[0]["owner_email"] == "owner@example.com"
    assert member.post("/me/team/accept", json={"owner_id": pending[0]["owner_id"]}).json()["joined"] is True
    me = member.get("/me").json()
    assert me["plan"]["id"] == "max" and me["team"]["role"] == "member" and me["team"]["owner"]["email"] == "owner@example.com"
    owner_balance = owner.get("/me").json()["credits"]["balance"]
    assert me["credits"]["balance"] == owner_balance  # shared pool
    member.post("/chat", json={"message": "hello"})
    assert owner.get("/me").json()["credits"]["balance"] == owner_balance - settings.credit_cost_chat_turn
    assert owner.get("/me/team").json()["members"][0]["status"] == "active"
    # Seats are enforced: owner + 4 members on Max.
    for i in range(3):
        assert owner.post("/me/team/invites", json={"email": f"m{i}@example.com"}).status_code == 201
    assert owner.post("/me/team/invites", json={"email": "toomany@example.com"}).status_code == 400
    # Removing the member returns them to their own Free plan and pool.
    assert owner.delete("/me/team/members/member@example.com").json() == {"removed": True}
    me = member.get("/me").json()
    assert me["plan"]["id"] == "free" and me["team"]["role"] is None and me["credits"]["balance"] == FREE.monthly_credits
    assert member.post("/me/team/leave").status_code == 400


def test_member_leaving_and_owner_downgrade_dissolve_the_link(fake_agent):
    owner = TestClient(main.app)
    owner_me = sign_in(owner, email="boss@example.com", plan_id="max")
    member = TestClient(main.app)
    sign_in(member, email="staff@example.com")
    owner.post("/me/team/invites", json={"email": "staff@example.com"})
    member.post("/me/team/accept", json={"owner_id": owner_me["user"]["id"]})
    assert member.get("/me").json()["plan"]["id"] == "max"
    assert member.post("/me/team/leave").json() == {"left": True}
    assert member.get("/me").json()["plan"]["id"] == "free" and owner.get("/me/team").json()["members"] == []
    # Re-invite, then the owner drops to Pro (1 seat): the member is detached on their next request.
    owner.post("/me/team/invites", json={"email": "staff@example.com"})
    member.post("/me/team/accept", json={"owner_id": owner_me["user"]["id"]})
    asyncio.run(accounts.update_user(owner_me["user"]["id"], plan_id="pro"))
    assert member.get("/me").json()["plan"]["id"] == "free"
    assert MAX.seats == 5
