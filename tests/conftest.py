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

    from app import user_memory as _user_memory

    for module in (accounts, credits, api_keys, billing, tasks, _user_memory):
        monkeypatch.setattr(module, "get_pg_pool", no_pool)
    _user_memory.reset_for_test()
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
    # Related questions call the model after every eligible turn; the suite
    # opts in per test (tests/test_followups.py) rather than paying for it.
    from app.settings import settings as _settings

    monkeypatch.setattr(_settings, "followups_enabled", False)
    monkeypatch.setattr(_settings, "answer_gate_enabled", False)
    # Cross-chat memory extracts with the model and embeds; tests opt in.
    monkeypatch.setattr(_settings, "user_memory_enabled", False)
    # The listing registry asks CoinGecko which coin a ticker is; the suite
    # answers "none listed" unless a test says otherwise.
    from app import symbol_registry as _symbols

    _symbols.reset()
    monkeypatch.setattr(_symbols, "listed", lambda symbol: [])

    # Outcome-scored tools (app/tool_outcomes.py) learn per-tool success rates
    # that shift the router's ranking. They are module state, so one test's
    # failed provider call silently reordered a later test's expected ranking
    # (tests/test_session_focus_resolution.py passed alone, failed in a run).
    from app import tool_outcomes as _outcomes

    with _outcomes._lock:
        _outcomes._counts.clear()

    # The provider router is an lru_cache singleton, so its per-tool health
    # (degraded by a failed call) and its admin overrides outlive the test
    # that caused them and reorder a later test's ranking.
    from app.provider_registry import get_provider_router as _router

    _live = _router()
    _live._health.clear()
    _live._overrides.clear()

    # A configured MOBULA_API_KEY makes the wallet and security tools live;
    # no test may reach the network for them. Tests that exercise the cards
    # patch these back with fixtures of their own.
    from app import mobula_security as _msec, mobula_wallet as _mwallet

    def _offline(*_a, **_k):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(_msec, "token_security", _offline)
    monkeypatch.setattr(_mwallet, "_get", _offline)

    from app import mobula_meme as _mmeme

    monkeypatch.setattr(_mmeme, "_get", _offline)
    monkeypatch.setattr(_mmeme, "_get_v1", _offline)
    monkeypatch.setattr(_mmeme, "token_trades", _offline)
    monkeypatch.setattr(_mmeme, "token_first_buyers", _offline)
    monkeypatch.setattr(_mmeme, "token_bundle_check", _offline)
    monkeypatch.setattr(_msec, "_logo_reuses", lambda address, chain: None)
    monkeypatch.setattr(_settings, "warm_caches_on_start", False)
    monkeypatch.setattr(_tradingview, "get_redis", no_redis)
    # The subject probe looks a name up on the web when the router is unsure;
    # the suite never reaches Perplexity for that. Tests of the probe itself
    # switch it on and stub the search.
    from app.routing import subject_probe as _subject_probe

    monkeypatch.setattr(_subject_probe, "perplexity_available", lambda: False)
    _subject_probe.reset()
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
