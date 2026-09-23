"""Shared fixtures: every test starts with empty in-memory account, credit and
API-key stores (the anonymous trial is keyed on the test client's IP, so
without this one test could exhaust the next one's credits), plus a helper
that signs a TestClient in through the magic-link flow."""
import os

# The suite describes a deployment with its providers configured: which tools
# register, how they rank, that admin routes answer 401 not 503. On a
# developer machine the .env supplies the keys; CI has no .env, and every
# push from 2026-09-17 to 2026-09-22 failed on exactly those tests. A
# placeholder makes each provider "configured" and nothing more -- the
# fixtures below block the network, and a test that reached it would fail
# on the placeholder rather than pass. Real values in the environment win.
for _key in ("OPENAI_API_KEY", "ADMIN_API_KEY", "MOBULA_API_KEY", "GOLDRUSH_API_KEY", "BITQUERY_API_KEY", "PERPLEXITY_API_KEY", "COINGECKO_API_KEY",
             "BIRDEYE_API_KEY", "HELIUS_API_KEY", "GOPLUS_ACCESS_TOKEN", "NANSEN_API_KEY", "ROOTDATA_API_KEY", "JUPITER_API_KEY", "RELAY_API_KEY",
             "COINMARKETCAP_API_KEY", "DUNE_API_KEY"):
    os.environ.setdefault(_key, "test-placeholder")

import pytest  # noqa: E402

from app import accounts, api_keys, billing, credits, tasks  # noqa: E402


@pytest.fixture(autouse=True)
def _no_token_2022_rpc(monkeypatch):
    # The portfolio reads both token programs (2026-09-23); unit tests that
    # stub the legacy read must not reach the real RPC for Token-2022.
    from app import portfolio as _portfolio

    async def _none(wallet):
        return {"value": []}
    monkeypatch.setattr(_portfolio, "get_token_accounts_2022", _none)


@pytest.fixture(autouse=True)
def _reset_account_stores(monkeypatch):
    # Unit tests exercise the in-memory stores; a configured DATABASE_URL must
    # not leak Postgres (and its event-loop-bound pool) into every test.
    async def no_pool():
        return None

    from app import decision_records as _decision_records, exit_monitor as _exit_monitor, feedback as _feedback, holder_snapshots as _holder_snapshots, jobs as _jobs, turn_log as _turn_log, user_memory as _user_memory
    from app import sentiment_analyst as _sentiment, x_tweets as _x_tweets

    for module in (accounts, credits, api_keys, billing, tasks, _user_memory, _decision_records, _holder_snapshots, _turn_log, _x_tweets, _feedback, _jobs, _exit_monitor):
        monkeypatch.setattr(module, "get_pg_pool", no_pool)
    _x_tweets.reset_for_test()
    _sentiment.reset_for_test()
    _user_memory.reset_for_test()
    _decision_records.reset_for_test()
    _turn_log.reset_for_test()
    _jobs.reset_for_test()
    _exit_monitor.reset_for_test()
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
    # Ratings likewise: the suite's feedback lives in the memory store, never
    # in a real Redis the developer's .env points at.
    from app import feedback as _feedback_mod

    monkeypatch.setattr(_feedback_mod, "get_redis", no_redis)
    _feedback_mod.reset()
    # Related questions call the model after every eligible turn; the suite
    # opts in per test (tests/test_followups.py) rather than paying for it.
    from app.settings import settings as _settings

    monkeypatch.setattr(_settings, "followups_enabled", False)
    # Memory is the suite's store (get_pg_pool is patched to None above); the
    # developer's DATABASE_URL must not make every store treat that as an
    # outage and strip the words the tests assert on.
    monkeypatch.setattr(_settings, "database_url", None)
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
    from app import exchange_listings as _listings, mobula_client as _mclient

    monkeypatch.setattr(_mclient, "get", _offline)
    monkeypatch.setattr(_listings, "_symbols", _offline)
    # The deployment-wide Mobula counter lives in Redis; the suite counts
    # nothing against the developer's live instance.
    monkeypatch.setattr(_settings, "mobula_shared_limit", False)
    _mclient.reset_for_test()
    monkeypatch.setattr(_mmeme, "token_trades", _offline)
    monkeypatch.setattr(_mmeme, "token_first_buyers", _offline)
    monkeypatch.setattr(_mmeme, "token_bundle_check", _offline)
    monkeypatch.setattr(_msec, "_logo_reuses", lambda address, chain: None)
    monkeypatch.setattr(_settings, "warm_caches_on_start", False)
    # The snapshot ledger's worker and its deep-dive hook never reach Mobula
    # in the suite; tests of the ledger stub `take` themselves.
    monkeypatch.setattr(_settings, "holder_snapshots_enabled", False)
    # The X sentiment analyst needs TwitterAPI.io and TypeSafe; the suite
    # never reaches either. Tests of the analyst set the keys and stub HTTP.
    monkeypatch.setattr(_settings, "twitterapi_io_key", None)
    monkeypatch.setattr(_settings, "reddit_client_id", None)
    monkeypatch.setattr(_settings, "reddit_client_secret", None)
    monkeypatch.setattr(_settings, "polymarket_enabled", False)
    from app import polymarket_odds as _polymarket, reddit_crowd as _reddit

    _reddit.reset_for_test()
    _polymarket.reset_for_test()
    from app import holder_snapshots as _snapshots

    _snapshots.reset_for_test()
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
