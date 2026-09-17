"""The P1 findings of the 2026-09-17 consolidated review, R01-R05, as
regression tests: each is the review's own reproduction with its assertion
inverted, so the same inputs that demonstrated the defect now demonstrate the
fix. The review's evidence file asserts the bug; this file asserts the rule.

Every refusal has a control beside it proving the legitimate path still works.
A suite that only checks denials passes on a server that denies everything.
"""
import asyncio
import json
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import accounts, api_keys, main
from app.settings import settings
from tests.conftest import sign_in

ROOT = Path(__file__).resolve().parents[1]


# --- R01 · Markdown links cannot inject attributes ----------------------------

class _Anchor(HTMLParser):
    attrs: dict = {}
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.attrs = dict(attrs)


def _render_inline(markdown: str) -> str:
    """Run the checked-in renderer in Node under a faithful text-node
    serialiser: a DOM text node escapes &, < and > and nothing else. That is
    exactly the property the review exploited, so the emulation must not be
    kinder than the browser."""
    source = (ROOT / "app/static/index.html").read_text()
    snippet = source[source.index("function escapeHtml("):source.index("function splitMarkdownRow(")]
    js = ("const document={createElement:()=>({textContent:'',get innerHTML(){return this.textContent"
          ".replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')}})};" + snippet
          + "console.log(renderInline(" + json.dumps(markdown) + "));")
    return subprocess.check_output(["node", "-e", js], text=True)


def test_a_markdown_link_cannot_break_out_of_its_href():
    """The review's payload: a URL carrying a quote that used to close the href
    and add an onmouseover handler."""
    html = _render_inline('[link](https://example.com/"onmouseover="window.reviewxss=1)')
    anchor = _Anchor(); anchor.feed(html)
    assert "onmouseover" not in anchor.attrs, html
    assert set(anchor.attrs) <= {"href", "target", "rel"} or anchor.attrs == {}, html


def test_quotes_are_escaped_by_the_shared_helper_itself():
    """Fixing the Markdown regex alone would leave every other attribute use of
    the helper exposed; the fix has to live in escapeHtml."""
    html = _render_inline('he said "hi" and it\'s fine')
    assert '"' not in html.replace("&quot;", "") and "'" not in html.replace("&#39;", "")
    assert "&quot;hi&quot;" in html and "it&#39;s" in html


def test_an_ordinary_link_still_renders():
    """The control."""
    html = _render_inline("see [docs](https://example.com/path?q=1&r=2) now")
    anchor = _Anchor(); anchor.feed(html)
    assert anchor.attrs.get("href") == "https://example.com/path?q=1&r=2"
    assert anchor.attrs.get("rel") == "noopener noreferrer"


def test_the_admin_helper_escapes_quotes_too():
    source = (ROOT / "app/static/admin.html").read_text()
    snippet = source[source.index("function esc("):source.index("async function api(")]
    js = ("const document={createElement:()=>({textContent:'',get innerHTML(){return this.textContent"
          ".replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')}})};" + snippet
          + "console.log(esc('a\"b\\'c'));")
    assert subprocess.check_output(["node", "-e", js], text=True).strip() == "a&quot;b&#39;c"


# --- R02 · API keys cannot manage the account; scopes mean what they say -----

def _key(client, scopes):
    created = client.post("/me/api-keys", json={"name": "k", "scopes": scopes})
    assert created.status_code == 201, created.text
    return created.json()["secret"]


def test_a_data_key_cannot_mutate_or_destroy_account_state():
    """The review's sequence, inverted: preferences, conversation deletion and
    session revocation with a key holding only `data`."""
    browser = TestClient(main.app)
    user = sign_in(browser, plan_id="pro")["user"]
    secret = _key(browser, ["data"])
    asyncio.run(accounts.touch_chat_session(user["id"], "private-review-chat"))

    attacker = TestClient(main.app)
    attacker.headers["Authorization"] = "Bearer " + secret
    assert attacker.put("/me/preferences", json={"display_name": "changed by data key"}).status_code == 403
    assert attacker.delete("/me/conversations").status_code == 403
    assert attacker.post("/me/sessions/revoke-all").status_code == 403
    assert attacker.post("/me/api-keys", json={"name": "escalate", "scopes": ["chat", "data", "mcp"]}).status_code == 403
    assert attacker.request("DELETE", "/me", json={"confirm_email": user.get("email") or ""}).status_code == 403

    # Nothing moved.
    assert asyncio.run(accounts.chat_session_owner("private-review-chat")) == user["id"]
    assert browser.get("/me").json()["authenticated"] is True, "the browser session was revoked"
    assert (asyncio.run(accounts.get_user(user["id"])).get("display_name")) != "changed by data key"


def test_every_account_management_route_refuses_an_api_key():
    """Enumerated rather than sampled: every /me and /billing mutation, and the
    account reads a key has no business with."""
    browser = TestClient(main.app)
    sign_in(browser, plan_id="pro")
    secret = _key(browser, ["chat", "data", "mcp"])   # every scope: scope is not the reason
    key = TestClient(main.app)
    key.headers["Authorization"] = "Bearer " + secret
    refused = []
    for method, path in [
        ("GET", "/me/api-keys"), ("GET", "/me/sessions"), ("GET", "/me/invoices"), ("GET", "/me/export"),
        ("GET", "/me/conversations"), ("GET", "/me/team"), ("GET", "/me/tasks"), ("GET", "/me/inbox"),
        ("GET", "/me/usage"), ("POST", "/me/tasks"), ("POST", "/me/inbox/read"), ("POST", "/me/team/invites"),
        ("POST", "/me/team/accept"), ("POST", "/me/team/leave"), ("POST", "/billing/checkout"), ("POST", "/billing/portal"),
    ]:
        response = key.request(method, path, json={})
        if response.status_code != 403:
            refused.append((method, path, response.status_code))
    assert refused == [], f"reachable with an API key: {refused}"


def test_an_unknown_scope_is_refused_rather_than_expanded():
    """A typo used to become every scope."""
    with pytest.raises(ValueError) as caught:
        asyncio.run(api_keys.create("user-1", "typo", ["read"]))
    assert "Unknown scope" in str(caught.value)
    with pytest.raises(ValueError):
        asyncio.run(api_keys.create("user-1", "none", []))
    browser = TestClient(main.app)
    sign_in(browser, plan_id="pro")
    assert browser.post("/me/api-keys", json={"name": "typo", "scopes": ["read"]}).status_code == 400


def test_a_scoped_key_still_does_what_its_scope_allows():
    """The controls: a chat key chats and reads history; a data key reads data
    and its own credit balance; a key without the scope is refused."""
    browser = TestClient(main.app)
    sign_in(browser, plan_id="pro")
    chat_secret = _key(browser, ["chat"])
    data_secret = _key(browser, ["data"])

    chat = TestClient(main.app); chat.headers["Authorization"] = "Bearer " + chat_secret
    data = TestClient(main.app); data.headers["Authorization"] = "Bearer " + data_secret

    assert data.get("/me/credits").status_code == 200
    assert chat.get("/me/credits").status_code == 403, "a chat-only key read the credit balance"
    assert data.get("/me").json()["authenticated"] is True
    # History needs the chat scope; the id is unowned so a 404 is "not yours", not "refused".
    assert chat.get("/chat/history/some-session").status_code in (200, 404)
    assert data.get("/chat/history/some-session").status_code == 403


def test_a_browser_session_still_manages_its_own_account():
    """The other control: tightening keys must not tighten browsers."""
    browser = TestClient(main.app)
    sign_in(browser, plan_id="pro")
    assert browser.put("/me/preferences", json={"display_name": "me"}).status_code == 200
    assert browser.get("/me/api-keys").status_code == 200
    assert browser.get("/me/conversations").status_code == 200
    assert browser.post("/me/sessions/revoke-all").status_code == 200


# --- R03 · the server wallet needs an explicit entitlement --------------------

def _custodial_setup(monkeypatch):
    """The review's fixture: a real ephemeral signer, a plan owned by the caller
    and addressed to that signer, broadcast intercepted. Returns (client, user,
    plan, signed) where `signed` collects anything that reached broadcast."""
    import base64
    import base58
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    from app import execution, plans
    from app.models import SwapProposal, TradePlan

    client = TestClient(main.app)
    user = sign_in(client)["user"]
    signer = Keypair()
    now = datetime.now(timezone.utc)
    plan = TradePlan(
        plan_id="custodial-review", status="pending_confirmation", created_at=now, expires_at=now + timedelta(minutes=5),
        wallet_address=str(signer.pubkey()),
        proposal=SwapProposal(input_mint="in", output_mint="out", amount_atomic=1, slippage_bps=10, reason="test"),
        quote={}, input_token={"mint": "in", "name": "In", "symbol": "IN", "decimals": 9},
        output_token={"mint": "out", "name": "Out", "symbol": "OUT", "decimals": 6},
        simulation={"ok": True}, confirmation_text="CONFIRM custodial-review", owner_account_id="user:" + user["id"],
    )
    message = MessageV0.try_compile(signer.pubkey(), [], [], Hash.default())
    tx = base64.b64encode(bytes(VersionedTransaction(message, [signer]))).decode()
    monkeypatch.setattr(settings, "deployment_mode", "execution")
    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(settings, "allow_custodial_signing", True)
    monkeypatch.setattr(settings, "solana_private_key", base58.b58encode(bytes(signer)).decode())
    monkeypatch.setattr(plans, "get_plan", AsyncMock(return_value=plan))
    monkeypatch.setattr(execution, "get_plan", AsyncMock(return_value=plan))
    monkeypatch.setattr(execution, "get_prepared_transaction", AsyncMock(return_value=tx))
    monkeypatch.setattr(execution, "simulate_transaction", AsyncMock(return_value={"ok": True}))
    signed = []

    async def capture(p, t, encoded):
        signed.append(t)
        return {"status": "submitted", "signature": str(t.signatures[0])}

    monkeypatch.setattr(execution, "_broadcast_reviewed", capture)
    return client, user, plan, signed


def test_an_unentitled_account_cannot_have_the_server_sign_its_plan(monkeypatch):
    """The review's reproduction, inverted: owning a plan addressed to the
    server signer is not an entitlement to the server's key."""
    client, user, plan, signed = _custodial_setup(monkeypatch)
    monkeypatch.setattr(settings, "custodial_signing_principals", "")
    response = client.post("/trade-plans/custodial-review/confirm", json={"confirmation_text": plan.confirmation_text})
    assert response.status_code == 403, response.text
    assert "not entitled" in response.json()["detail"]
    assert signed == [], "the server signed for an account with no entitlement"


def test_an_entitled_account_can_still_use_the_server_wallet(monkeypatch):
    """The control: the entitlement admits the account it names."""
    client, user, plan, signed = _custodial_setup(monkeypatch)
    monkeypatch.setattr(settings, "custodial_signing_principals", user["id"])
    response = client.post("/trade-plans/custodial-review/confirm", json={"confirmation_text": plan.confirmation_text})
    assert response.status_code == 200, response.text
    assert len(signed) == 1


def test_entitlement_does_not_let_one_account_sign_anothers_plan(monkeypatch):
    """The other half of the rule: entitled to the wallet, but not the owner."""
    client, user, plan, signed = _custodial_setup(monkeypatch)
    monkeypatch.setattr(settings, "custodial_signing_principals", user["id"])
    plan.owner_account_id = "user:somebody-else"
    response = client.post("/trade-plans/custodial-review/confirm", json={"confirmation_text": plan.confirmation_text})
    assert response.status_code in (403, 404) and signed == []


def test_custodial_signing_for_nobody_is_a_startup_error():
    from app import deployment
    from app.settings import Settings

    config = Settings()
    config.deployment_mode, config.live_trading = "execution", True
    config.allow_custodial_signing, config.solana_private_key = True, "key"
    config.custodial_signing_principals, config.openai_api_key = "", "sk-test"
    codes = {p.code for p in deployment.audit(config) if p.severity == deployment.FATAL}
    assert "custodial-principals-missing" in codes


# --- R04 · billing state survives late and stale events ----------------------

@pytest.mark.parametrize("late", ["update", "invoice", "checkout"])
def test_a_cancelled_subscription_is_not_resurrected_by_a_late_event(late):
    """The review's three variants, inverted. Cancellation used to erase the
    subscription id, so a late event for that same subscription saw 'no
    current subscription' and restored paid access."""
    from app import billing

    async def run():
        user, _ = await accounts.get_or_create_user(f"cancel-{late}@example.com")
        meta = {"user_id": user["id"], "plan_id": "pro"}
        await accounts.update_user(user["id"], stripe_customer_id=f"cus_{late}", stripe_subscription_id="sub_x",
                                   subscription_created=1000, subscription_status="active", plan_id="pro")
        await billing._on_subscription_deleted("evt_delete", {"id": "sub_x", "created": 1000, "metadata": meta}, event_created=5000)
        if late == "update":
            result = await billing._on_subscription_updated("evt_old", {"id": "sub_x", "created": 1000, "status": "active", "metadata": meta}, event_created=4000)
        elif late == "invoice":
            result = await billing._on_invoice_paid("evt_old", {"id": "in_old", "subscription": "sub_x", "customer": f"cus_{late}", "metadata": meta,
                                                                "lines": {"data": [{"metadata": {"plan_id": "pro"}}]}}, event_created=4000)
        else:
            result = await billing._on_checkout_completed("evt_old", {"mode": "subscription", "subscription": "sub_x", "customer": f"cus_{late}",
                                                                      "created": 900, "metadata": meta}, event_created=4000)
        current = await accounts.get_user(user["id"])
        return result, current

    result, current = asyncio.run(run())
    assert current["plan_id"] == "free", f"{late}: a late event restored paid access ({result})"
    assert current["subscription_status"] == "canceled"


def test_a_late_event_with_no_envelope_time_cannot_revive_a_cancelled_subscription():
    """Without ordering information, an event for a cancelled subscription is
    stale. This is the shape of the review's own reproduction, which passed no
    envelope time at all."""
    from app import billing

    async def run():
        user, _ = await accounts.get_or_create_user("cancel-noenv@example.com")
        await accounts.update_user(user["id"], stripe_customer_id="cus_noenv", stripe_subscription_id="sub_x",
                                   subscription_created=1000, subscription_status="active", plan_id="pro")
        await billing._on_subscription_deleted("evt_delete", {"id": "sub_x", "created": 1000, "customer": "cus_noenv"})
        await billing._on_subscription_updated("evt_old", {"id": "sub_x", "created": 1000, "status": "active", "customer": "cus_noenv",
                                                           "metadata": {"plan_id": "pro"}})
        return await accounts.get_user(user["id"])

    current = asyncio.run(run())
    assert current["plan_id"] == "free" and current["subscription_status"] == "canceled"


def test_a_genuinely_newer_event_can_reactivate_a_cancelled_subscription(monkeypatch):
    """The control: Stripe reactivates before period end, and that arrives as a
    newer update for the same id. The tombstone must yield to it."""
    from app import billing

    monkeypatch.setattr(settings, "stripe_price_pro", "price_pro")

    async def run():
        user, _ = await accounts.get_or_create_user("reactivate@example.com")
        await accounts.update_user(user["id"], stripe_customer_id="cus_re", stripe_subscription_id="sub_x",
                                   subscription_created=1000, subscription_status="active", plan_id="pro")
        await billing._on_subscription_deleted("evt_delete", {"id": "sub_x", "created": 1000, "customer": "cus_re"}, event_created=5000)
        result = await billing._on_subscription_updated("evt_new", {"id": "sub_x", "created": 1000, "status": "active", "customer": "cus_re",
                                                                    "items": {"data": [{"price": {"id": "price_pro"}}]}}, event_created=6000)
        return result, await accounts.get_user(user["id"])

    result, current = asyncio.run(run())
    assert result["status"] == "updated" and current["plan_id"] == "pro" and current["subscription_status"] == "active"


def test_an_older_update_for_the_current_subscription_is_ignored():
    """Same id, delivered late: the subscription's own creation time cannot
    order it, the envelope time can."""
    from app import billing

    async def run():
        user, _ = await accounts.get_or_create_user("same-id-order@example.com")
        await accounts.update_user(user["id"], stripe_customer_id="cus_so", stripe_subscription_id="sub_x",
                                   subscription_created=1000, subscription_status="active", plan_id="max", subscription_event_at=7000)
        result = await billing._on_subscription_updated("evt_late", {"id": "sub_x", "created": 1000, "status": "past_due", "customer": "cus_so"},
                                                        event_created=6000)
        return result, await accounts.get_user(user["id"])

    result, current = asyncio.run(run())
    assert result["status"] == "ignored" and current["subscription_status"] == "active"


def test_invoice_lines_resolve_the_plan_from_the_price_not_stale_metadata(monkeypatch):
    """The review's reproduction, inverted: a Max-priced invoice carrying old
    Pro metadata must grant Max."""
    from app import billing

    monkeypatch.setattr(settings, "stripe_price_max", "price_max_review")

    async def run():
        user, _ = await accounts.get_or_create_user("price-lines@example.com")
        await accounts.update_user(user["id"], stripe_customer_id="cus_pl", stripe_subscription_id="sub_max", subscription_status="active", plan_id="max")
        result = await billing._on_invoice_paid("evt_price", {"id": "in_max", "subscription": "sub_max", "customer": "cus_pl",
                                                              "lines": {"data": [{"price": {"id": "price_max_review"}, "metadata": {"plan_id": "pro"}}]}},
                                                event_created=8000)
        return result, await accounts.get_user(user["id"])

    result, current = asyncio.run(run())
    assert result["plan_id"] == "max" and current["plan_id"] == "max"


# --- R05 · one live subscription per account ----------------------------------

def test_a_subscribed_account_cannot_start_a_second_subscription_checkout(monkeypatch):
    from app import billing

    monkeypatch.setattr(billing, "configured", lambda: True)
    monkeypatch.setattr(settings, "stripe_price_max", "price_max_review")
    created = []
    monkeypatch.setattr(billing, "_api_create_checkout", lambda params: created.append(params) or {"id": "cs", "url": "https://example.com"})
    user = {"id": "u", "stripe_customer_id": "cus_x", "stripe_subscription_id": "sub_active", "subscription_status": "active", "plan_id": "pro"}
    with pytest.raises(billing.SubscriptionExists):
        asyncio.run(billing.create_checkout(user, "subscription", "max", "https://example.com"))
    assert created == [], "a second subscription checkout was started"


def test_an_unsubscribed_account_still_starts_a_checkout(monkeypatch):
    """The control, and the cancelled case: a cancelled subscription on record
    must not block resubscribing."""
    from app import billing

    monkeypatch.setattr(billing, "configured", lambda: True)
    monkeypatch.setattr(settings, "stripe_price_max", "price_max_review")
    monkeypatch.setattr(billing, "_api_list_active_subscriptions", lambda customer_id: [])
    created = []
    monkeypatch.setattr(billing, "_api_create_checkout", lambda params: created.append(params) or {"id": f"cs_{len(created)}", "url": "https://example.com"})
    for user in ({"id": "u1", "stripe_customer_id": "cus_1", "plan_id": "free"},
                 {"id": "u2", "stripe_customer_id": "cus_2", "stripe_subscription_id": "sub_old", "subscription_status": "canceled", "plan_id": "free"}):
        asyncio.run(billing.create_checkout(user, "subscription", "max", "https://example.com"))
    assert len(created) == 2


def test_the_checkout_route_answers_409_and_points_at_the_portal(monkeypatch):
    from app import billing

    monkeypatch.setattr(billing, "configured", lambda: True)
    monkeypatch.setattr(settings, "stripe_price_max", "price_max_review")
    client = TestClient(main.app)
    me = sign_in(client)["user"]
    asyncio.run(accounts.update_user(me["id"], stripe_customer_id="cus_r", stripe_subscription_id="sub_live", subscription_status="active", plan_id="pro"))
    response = client.post("/billing/checkout", json={"kind": "subscription", "item_id": "max"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "subscription_exists"


def test_account_deletion_cancels_every_live_subscription_not_only_the_recorded_one(monkeypatch):
    from app import billing

    client = TestClient(main.app)
    me = sign_in(client, email="two-subs@example.com")["user"]
    asyncio.run(accounts.update_user(me["id"], stripe_customer_id="cus_two", stripe_subscription_id="sub_known", subscription_status="active"))
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_harness")
    monkeypatch.setattr(billing, "_api_list_active_subscriptions",
                        lambda customer_id: [{"id": "sub_known", "status": "active"}, {"id": "sub_overlap", "status": "active"}])
    cancelled = []
    monkeypatch.setattr(billing, "_api_cancel_subscription", lambda sid: cancelled.append(sid) or {"id": sid, "status": "canceled"})
    assert client.request("DELETE", "/me", json={"confirm_email": "two-subs@example.com"}).status_code == 200
    assert sorted(cancelled) == ["sub_known", "sub_overlap"]
