"""Closed beta: no payments, every signed-in account on the free Beta plan.

Off by default (nothing changes for existing setups); on, billing reports
itself unconfigured whatever Stripe settings exist, the plan catalogue is the
Beta plan alone, every signed-in account resolves to it, and the browser
hides prices and purchase controls.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import accounts, billing, billing_plans, main
from app.identity import resolve_billing
from app.settings import settings
from tests.conftest import sign_in


@pytest.fixture
def beta(monkeypatch):
    monkeypatch.setattr(settings, "closed_beta", True)
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_would_be_live")   # must not matter


def test_off_by_default_nothing_changes():
    assert settings.closed_beta is False
    assert billing_plans.get_plan("free").id == "free" and billing_plans.get_plan("pro").id == "pro"
    assert [p["id"] for p in billing_plans.catalog()] == ["free", "pro", "max"]


def test_every_signed_in_plan_resolves_to_beta_but_the_trial_stays_the_trial(beta):
    for stored in (None, "free", "pro", "max", "beta", "nonsense"):
        assert billing_plans.get_plan(stored).id == "beta", stored
    assert billing_plans.get_plan("anonymous").id == "anonymous"
    plan = billing_plans.get_plan("free")
    assert plan.price_usd_month == 0 and plan.monthly_credits == settings.closed_beta_monthly_credits
    assert plan.api_keys and plan.mcp and plan.team_mode and not plan.public()["purchasable"]
    assert [p["id"] for p in billing_plans.catalog()] == ["beta"]


def test_billing_is_unconfigured_whatever_stripe_says(beta):
    assert billing.configured() is False
    client = TestClient(main.app)
    sign_in(client, email="beta-tester@example.com")
    assert client.post("/billing/checkout", json={"kind": "subscription", "item_id": "pro"}).status_code == 503
    assert client.post("/billing/portal").status_code in (503, 400)
    plans = client.get("/billing/plans").json()
    assert plans["configured"] is False and [p["id"] for p in plans["plans"]] == ["beta"]


def test_a_signed_in_tester_is_billed_on_the_beta_plan_with_its_allowance(beta):
    async def run():
        user, _ = await accounts.get_or_create_user("allowance@example.com")
        _user, _owner, plan = await resolve_billing(user)
        return plan

    plan = asyncio.run(run())
    assert plan.id == "beta" and plan.monthly_credits == settings.closed_beta_monthly_credits


def test_me_reports_the_beta_plan_and_its_credits(beta):
    client = TestClient(main.app)
    me = sign_in(client, email="me-beta@example.com")
    assert me["plan"]["id"] == "beta" and me["plan"]["price_usd_month"] == 0
    assert me["credits"]["balance"] == settings.closed_beta_monthly_credits


def test_public_config_tells_the_browser(beta):
    cfg = TestClient(main.app).get("/config/public").json()
    assert cfg["accounts"]["closed_beta"] is True and cfg["accounts"]["closed_beta_monthly_credits"] == settings.closed_beta_monthly_credits


def test_public_config_is_silent_when_off():
    cfg = TestClient(main.app).get("/config/public").json()
    assert cfg["accounts"]["closed_beta"] is False and cfg["accounts"]["closed_beta_monthly_credits"] is None


# --- the browser side, through the real inline script -------------------------

def test_the_browser_hides_purchase_controls_and_explains_the_beta():
    from tests.test_ui_swap_flow import run_case
    result = run_case("closed_beta_hides_purchase_controls")
    assert result["hidden"] == {"plansBtn": True, "buyCreditsBtn": True, "changePlanBtn": True}
    assert "Closed beta" in result["plansBody"] and "5,000" in result["plansBody"]
    assert result["plansFetched"] is False, "the plans dialog fetched a price list in the closed beta"


def test_the_browser_shows_purchase_controls_when_billing_is_open():
    from tests.test_ui_swap_flow import run_case
    result = run_case("purchase_controls_show_when_billing_is_open")
    assert result["hidden"] == {"plansBtn": False, "buyCreditsBtn": False, "changePlanBtn": False}
    assert result["plansFetched"] is True and result["closedBeta"] is None
