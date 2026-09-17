"""Review of 2026-09-18, two P1 findings.

1. A newly created API-key secret stayed on screen after signing out, and
   after another account signed in on the same tab.
2. Deleting an account cancelled its subscription but left its open Checkout
   payable: paying it later created a subscription for a customer no account
   maps to. Closed on both sides -- deletion expires the account's open
   checkouts, and a checkout completed for an account that no longer exists
   cancels the subscription it created.
"""
import asyncio
import time

from fastapi.testclient import TestClient

from app import accounts, billing, main
from app.settings import settings
from tests.conftest import sign_in


# --- 1. the secret and the account that made it --------------------------------

def test_a_new_api_key_secret_is_cleared_on_sign_out_and_on_account_switch():
    from tests.test_ui_swap_flow import run_case
    r = run_case("api_key_secret_is_cleared_on_sign_out_and_account_switch")
    assert r["shown"] == {"hidden": False, "value": "orb_sk_SECRET123"}, "the secret is shown once, to the account that created it"
    assert r["afterSignOut"]["hidden"] is True and r["afterSignOut"]["html"] == "" and "SECRET" not in r["afterSignOut"]["value"], r
    assert r["afterSwitch"]["hidden"] is True and r["afterSwitch"]["html"] == "" and "SECRET" not in r["afterSwitch"]["value"], r


# --- 2. an open checkout does not survive the account ---------------------------

def _stripe(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_harness")
    monkeypatch.setattr(billing, "configured", lambda: True)


def test_deleting_an_account_expires_its_open_checkout(monkeypatch):
    _stripe(monkeypatch)
    client = TestClient(main.app)
    me = sign_in(client, email="open-checkout@example.com")["user"]
    asyncio.run(accounts.update_user(me["id"], stripe_customer_id="cus_open", checkout_session_id="cs_open", checkout_started_at=int(time.time())))
    monkeypatch.setattr(billing, "_api_list_active_subscriptions", lambda customer_id: [])
    listed, expired = [], []
    monkeypatch.setattr(billing, "_api_list_open_checkouts", lambda customer_id: listed.append(customer_id) or [{"id": "cs_open", "status": "open"}, {"id": "cs_other", "status": "open"}], raising=False)
    monkeypatch.setattr(billing, "_api_expire_checkout", lambda sid: expired.append(sid) or {"id": sid, "status": "expired"})
    assert client.request("DELETE", "/me", json={"confirm_email": "open-checkout@example.com"}).status_code == 200
    assert listed == ["cus_open"], "every open checkout Stripe has for the customer is looked up, not only the recorded one"
    assert sorted(expired) == ["cs_open", "cs_other"]


def test_an_open_checkout_that_cannot_be_closed_stops_the_deletion(monkeypatch):
    _stripe(monkeypatch)
    client = TestClient(main.app)
    me = sign_in(client, email="stuck-checkout@example.com")["user"]
    asyncio.run(accounts.update_user(me["id"], stripe_customer_id="cus_stuck", checkout_session_id="cs_stuck", checkout_started_at=int(time.time())))
    monkeypatch.setattr(billing, "_api_list_active_subscriptions", lambda customer_id: [])
    monkeypatch.setattr(billing, "_api_list_open_checkouts", lambda customer_id: [{"id": "cs_stuck", "status": "open"}], raising=False)

    def refuse(sid):
        raise RuntimeError("stripe down")

    monkeypatch.setattr(billing, "_api_expire_checkout", refuse)
    r = client.request("DELETE", "/me", json={"confirm_email": "stuck-checkout@example.com"})
    assert r.status_code == 409 and "checkout" in r.json()["detail"].lower()
    assert asyncio.run(accounts.get_user(me["id"])) is not None, "the account is intact and the user can retry"


def test_a_checkout_completed_for_a_deleted_account_cancels_the_subscription_it_made(monkeypatch):
    _stripe(monkeypatch)
    cancelled = []
    monkeypatch.setattr(billing, "_api_cancel_subscription", lambda sid: cancelled.append(sid) or {"id": sid, "status": "canceled"})
    session = {"id": "cs_ghost", "mode": "subscription", "subscription": "sub_orphan", "customer": "cus_ghost",
               "client_reference_id": "no-such-user", "metadata": {"user_id": "no-such-user", "plan_id": "pro"}}
    out = asyncio.run(billing._on_checkout_completed("evt_ghost", session, int(time.time())))
    assert cancelled == ["sub_orphan"], "the subscription that no account maps to is cancelled at Stripe"
    assert out["status"] == "orphan_cancelled" and out["subscription"] == "sub_orphan"
