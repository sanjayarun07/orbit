"""Shared fixtures: every test starts with empty in-memory account, credit and
API-key stores (the anonymous trial is keyed on the test client's IP, so
without this one test could exhaust the next one's credits), plus a helper
that signs a TestClient in through the magic-link flow."""
import pytest

from app import accounts, api_keys, billing, credits, tasks


@pytest.fixture(autouse=True)
def _reset_account_stores(monkeypatch):
    # Unit tests exercise the in-memory stores; a configured DATABASE_URL must
    # not leak Postgres (and its event-loop-bound pool) into every test.
    async def no_pool():
        return None

    for module in (accounts, credits, api_keys, billing, tasks):
        monkeypatch.setattr(module, "get_pg_pool", no_pool)
    from app.integrations import tradingview as _tradingview

    monkeypatch.setattr(_tradingview, "get_pg_pool", no_pool)
    billing.reset()
    tasks.reset()
    # Rate-limit and daily-budget windows are module state too. Every test
    # client shares one IP, so without this the per-IP trial budget spent by
    # one test starves the anonymous callers of every test after it.
    from app import limits

    limits._local_windows.clear()

    # And they must not reach a real Redis from the suite: the per-IP trial
    # budget lives for two days, so one run against the dev instance starved
    # every anonymous caller of every later run. Retention tests still reach
    # Redis through sessions.get_redis; this only keeps admission in memory.
    async def no_redis():
        return None

    monkeypatch.setattr(limits, "get_redis", no_redis)
    monkeypatch.setattr(_tradingview, "get_redis", no_redis)
    # The /auth/ rate limit is per IP and lives in Redis when configured; every
    # test signs in from the same client IP, so it must not carry across tests.
    from app import main

    async def allow(identity):
        return True, 0

    monkeypatch.setattr(main, "allow_auth_request", allow)
    # Checkout now reconciles against Stripe's view of the customer's live
    # subscriptions before creating a session. The suite never reaches Stripe;
    # the default answer is "none", and tests about reconciliation override it.
    monkeypatch.setattr(billing, "_api_list_active_subscriptions", lambda customer_id: [])
    monkeypatch.setattr(billing, "_api_retrieve_checkout", lambda session_id: {"id": session_id, "status": "expired"})
    monkeypatch.setattr(billing, "_api_expire_checkout", lambda session_id: {"id": session_id, "status": "expired"})
    # Never send real email from the suite; the dev link stands in for it.
    from app.settings import settings

    monkeypatch.setattr(settings, "resend_api_key", None)
    monkeypatch.setattr(settings, "dev_expose_magic_links", True)
    accounts.reset()
    credits.reset()
    api_keys.reset()
    yield
    accounts.reset()
    credits.reset()
    api_keys.reset()


def sign_in(client, email="user@example.com", plan_id=None):
    """Complete the email magic-link flow on a TestClient (no email provider
    configured -> the dev link is returned) and return the /me payload."""
    started = client.post("/auth/email/start", json={"email": email}).json()
    token = started["dev_link"].rsplit("signin=", 1)[1]
    me = client.post("/auth/email/verify", json={"token": token}).json()
    if plan_id:
        import asyncio
        asyncio.run(accounts.update_user(me["user"]["id"], plan_id=plan_id))
        me = client.get("/me").json()
    return me


@pytest.fixture
def signed_in_client():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    sign_in(client)
    return client
