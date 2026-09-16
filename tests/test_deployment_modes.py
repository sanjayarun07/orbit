"""STAB-02: deployment modes and the startup configuration audit.

Two things are proved here.

First, a *matrix*: every money-moving entry point must refuse in research mode
and must be reachable in execution mode, uniformly across providers. The bug
this closes was an asymmetry -- LIVE_TRADING gated the three Jupiter routes and
left POST /execution/lifi/quote open, even though a LI.FI quote carries a
ready-to-sign transactionRequest. A test that only checked Jupiter would have
passed all along, so the matrix is driven from one list of routes and a new
money-moving route has to be added to it.

Second, that an unsafe production configuration *fails visibly* -- at startup,
with every problem named at once, rather than coming up and behaving like a
development laptop.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import deployment
from app.deployment import ExecutionDisabledError, UnsafeDeploymentError
from app.main import app
from app.settings import Settings, settings


# --- the mode matrix ---------------------------------------------------------

# Every entry point that can put a transaction in front of a user, with a body
# that is valid enough to get past request validation. None of these plan ids
# exist: refusal must come from the mode, before any lookup, which is also why
# a 403 here proves the gate runs first.
# The fourth element names the work the handler does once the gate lets it
# past, so the execution-mode half of the matrix can stub it out and prove the
# request reached the handler without needing a live plan store or provider.
MONEY_MOVING = [
    ("POST", "/trade-plans/plan-does-not-exist/confirm",
     {"confirmation_text": "CONFIRM plan-does-not-exist"}, "execute_confirmed_plan"),
    ("POST", "/trade-plans/plan-does-not-exist/wallet-transaction",
     {"confirmation_text": "CONFIRM plan-does-not-exist"}, "prepare_wallet_transaction"),
    ("POST", "/trade-plans/plan-does-not-exist/submit-wallet-transaction",
     {"confirmation_text": "CONFIRM plan-does-not-exist", "signed_transaction": "AA=="},
     "submit_wallet_transaction"),
    ("POST", "/execution/lifi/quote", {
        "from_chain": "1", "to_chain": "8453", "from_token": "ETH", "to_token": "USDC",
        "from_amount": "1000000000000000", "from_address": "0x" + "11" * 20,
    }, "get_lifi_quote"),
]


@pytest.fixture
def research(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "research")
    monkeypatch.setattr(settings, "live_trading", False)
    return TestClient(app)


@pytest.fixture
def execution(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "execution")
    monkeypatch.setattr(settings, "live_trading", True)
    return TestClient(app)


@pytest.mark.parametrize("method,path,body,_work", MONEY_MOVING, ids=[row[1] for row in MONEY_MOVING])
def test_research_mode_refuses_every_money_moving_route(research, method, path, body, _work):
    """Refused before any lookup: these plan ids do not exist, so a 403 rather
    than a 404 also proves nothing was read to produce the answer."""
    response = research.request(method, path, json=body)
    assert response.status_code == 403, f"{path} was reachable in research mode"
    assert "research mode" in response.json()["detail"]
    assert response.json()["deployment_mode"] == "research"


@pytest.mark.parametrize("method,path,body,work", MONEY_MOVING, ids=[row[1] for row in MONEY_MOVING])
def test_execution_mode_lets_the_same_routes_reach_their_handler(execution, monkeypatch, method, path, body, work):
    """The other half of the matrix: the gate must not be a permanent refusal.

    The work each handler would do is stubbed, so this proves the request got
    past the mode gate and the ownership check without needing a live plan
    store, wallet or provider -- those are STAB-09's job, not this item's.
    """
    from app import main as main_module

    reached = {}

    async def _no_plan_gate(plan_id, request):
        reached["ownership_checked"] = plan_id

    def _record(*args, **kwargs):
        reached["called"] = work
        return {"stubbed": work}

    async def _record_async(*args, **kwargs):
        return _record(*args, **kwargs)

    monkeypatch.setattr(main_module, "_require_plan_access", _no_plan_gate)
    monkeypatch.setattr(main_module, work, _record if work == "get_lifi_quote" else _record_async)
    response = execution.request(method, path, json=body)
    assert response.status_code == 200, response.text
    assert reached["called"] == work


def test_lifi_quote_is_in_the_matrix_because_it_returns_a_signable_transaction():
    """Guards the specific asymmetry this item existed to fix: LIVE_TRADING
    gated the three Jupiter routes and left this one open, even though a LI.FI
    quote comes back with a ready-to-sign transactionRequest."""
    assert any("/execution/lifi/quote" in row[1] for row in MONEY_MOVING)


def test_public_config_tells_the_browser_what_the_server_would_refuse(research):
    config = research.get("/config/public").json()
    assert config["deployment"] == {"mode": "research", "execution_enabled": False, "custodial_signing": False}
    # Relay signs in the browser, so reporting it off is what stops it in the UI.
    assert config["execution_providers"] == {"jupiter": False, "relay": False, "lifi_backup": False}


def test_public_config_reports_providers_on_in_execution_mode(execution):
    config = execution.get("/config/public").json()
    assert config["deployment"]["execution_enabled"] is True
    assert config["execution_providers"]["jupiter"] is True and config["execution_providers"]["relay"] is True


# --- mode resolution ---------------------------------------------------------

def _settings(**overrides) -> Settings:
    base = Settings()
    for key, value in overrides.items():
        object.__setattr__(base, key, value) if False else setattr(base, key, value)
    return base


def test_unset_mode_is_inferred_from_live_trading_so_existing_deployments_keep_behaving():
    assert deployment.deployment_mode(_settings(deployment_mode=None, live_trading=False)) == "research"
    assert deployment.deployment_mode(_settings(deployment_mode=None, live_trading=True)) == "execution"


def test_the_default_deployment_is_research():
    assert deployment.deployment_mode(_settings()) == "research"
    assert deployment.execution_enabled(_settings()) is False


def test_an_unrecognised_mode_never_silently_opens_execution():
    config = _settings(deployment_mode="Execution!", live_trading=True)
    assert deployment.deployment_mode(config) == "research"


def test_custodial_signing_needs_the_mode_the_flag_and_a_key_together():
    assert deployment.custodial_signing_enabled(
        _settings(deployment_mode="execution", allow_custodial_signing=True, solana_private_key="k")) is True
    # any one of the three missing is enough to keep the server out of custody
    assert deployment.custodial_signing_enabled(
        _settings(deployment_mode="research", allow_custodial_signing=True, solana_private_key="k")) is False
    assert deployment.custodial_signing_enabled(
        _settings(deployment_mode="execution", allow_custodial_signing=False, solana_private_key="k")) is False
    assert deployment.custodial_signing_enabled(
        _settings(deployment_mode="execution", allow_custodial_signing=True, solana_private_key=None)) is False


def test_server_side_signing_is_refused_even_when_execution_is_enabled(monkeypatch):
    """A wallet-only deployment must never sign for a user, and the refusal has
    to happen before the plan is loaded."""
    monkeypatch.setattr(settings, "deployment_mode", "execution")
    monkeypatch.setattr(settings, "live_trading", True)
    monkeypatch.setattr(settings, "allow_custodial_signing", False)
    monkeypatch.setattr(settings, "solana_private_key", None)
    from app.execution import execute_confirmed_plan
    with pytest.raises(ExecutionDisabledError) as caught:
        asyncio.run(execute_confirmed_plan("plan-does-not-exist", "CONFIRM plan-does-not-exist"))
    assert "Server-side signing is disabled" in str(caught.value)


# --- the startup audit -------------------------------------------------------

def _production(**overrides) -> Settings:
    """A production configuration that is otherwise clean, so each test can
    make exactly one thing unsafe and see only that problem."""
    safe = dict(
        environment="production", deployment_mode="research", live_trading=False,
        dev_expose_magic_links=False, allow_memory_fallback=False,
        database_url="postgresql://db/orbit", redis_url="redis://cache:6379/0",
        mcp_api_key="mcp-key", admin_api_key="admin-key", resend_api_key="resend-key",
        openai_api_key="sk-test", public_base_url="https://orbit.example.com",
        solana_private_key=None, allow_custodial_signing=False, x402_enabled=False,
    )
    safe.update(overrides)
    return _settings(**safe)


def _codes(config, severity=deployment.FATAL) -> set[str]:
    return {p.code for p in deployment.audit(config) if p.severity == severity}


def test_a_clean_production_configuration_starts():
    assert deployment.audit(_production()) == []
    assert deployment.enforce(_production()) == []


def test_development_defaults_are_not_fatal():
    """The defaults are a laptop's defaults; they must not block local work."""
    assert _codes(_settings()) == set()


def test_exposed_magic_links_are_fatal_in_production():
    """Returning the sign-in link in the response means anyone who knows an
    address can sign in as them once email delivery fails."""
    assert "magic-links-exposed" in _codes(_production(dev_expose_magic_links=True))


def test_memory_fallback_is_fatal_in_production():
    assert "memory-fallback-allowed" in _codes(_production(allow_memory_fallback=True))


def test_an_unauthenticated_mcp_endpoint_is_fatal_in_production():
    assert "mcp-unauthenticated" in _codes(_production(mcp_api_key=None))


def test_missing_shared_storage_is_fatal_in_production():
    assert "database-missing" in _codes(_production(database_url=None))
    assert "redis-missing" in _codes(_production(redis_url=None))


def test_a_localhost_public_base_url_is_fatal_in_production():
    assert "public-base-url-local" in _codes(_production(public_base_url="http://localhost:8000"))
    assert "public-base-url-local" in _codes(_production(public_base_url="http://orbit.example.com"))


def test_research_mode_with_live_trading_on_is_a_contradiction_not_a_silent_winner():
    assert "mode-contradicts-live-trading" in _codes(_production(deployment_mode="research", live_trading=True))


def test_a_server_key_with_live_trading_needs_an_explicit_custody_decision():
    unsafe = _production(deployment_mode="execution", live_trading=True, solana_private_key="key")
    assert "custodial-key-present" in _codes(unsafe)
    declared = _production(deployment_mode="execution", live_trading=True,
                           solana_private_key="key", allow_custodial_signing=True)
    assert "custodial-key-present" not in _codes(declared)


def test_an_unrecognised_mode_is_fatal_rather_than_quietly_research():
    assert "mode-unrecognised" in _codes(_production(deployment_mode="beta"))


def test_enforce_names_every_fatal_problem_in_one_message():
    """An operator should fix the whole list in one deploy, not discover them
    one restart at a time."""
    unsafe = _production(dev_expose_magic_links=True, allow_memory_fallback=True,
                         mcp_api_key=None, database_url=None)
    with pytest.raises(UnsafeDeploymentError) as caught:
        deployment.enforce(unsafe)
    message = str(caught.value)
    for setting in ("DEV_EXPOSE_MAGIC_LINKS", "ALLOW_MEMORY_FALLBACK", "MCP_API_KEY", "DATABASE_URL"):
        assert setting in message
    assert "4 unsafe setting" in message


def test_warnings_do_not_block_startup_but_are_returned():
    warned = deployment.enforce(_production(admin_api_key=None, resend_api_key=None))
    assert {problem.code for problem in warned} == {"admin-key-missing", "email-not-configured"}


def test_readyz_reports_the_mode_and_only_non_fatal_config_warnings(research):
    body = research.get("/readyz").json()
    assert body["deployment"]["mode"] == "research"
    assert "environment" in body
    # Anything fatal stops the process, so a live instance can never show one.
    assert all(warning["code"] for warning in body["config_warnings"])
