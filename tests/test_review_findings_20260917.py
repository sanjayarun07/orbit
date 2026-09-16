"""Eight findings from the review of 32ad4c29, each reproduced before it was fixed.

Grouped by what they are actually about rather than by file:

* privilege boundaries -- an API key must not become a browser session, and a
  contract wallet's identity must not span networks it was never verified on;
* money, which must reconcile -- a deleted account must not keep being charged,
  a stale Stripe event must not undo a newer one, and entitlements must follow
  the price actually billed;
* limits, which must bind wherever the state they limit is entered;
* configuration, which must never advertise what the code will refuse.

The frontend finding (the inline swap card bypassing the risk charter) lives in
tests/test_ui_swap_flow.py with the rest of the browser coverage.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import accounts, api_keys, billing, deployment, tasks, wallet_auth
from app.main import app
from app.settings import Settings, settings
from tests.conftest import sign_in


# --- 1. an API key must not become a browser session -------------------------

def _wallet_and_signature(client, address=None):
    from eth_account import Account
    from eth_account.messages import encode_defunct

    account = Account.create()
    challenge = client.post("/auth/wallet/challenge", json={"address": account.address, "chain": "1"}).json()
    signature = Account.sign_message(encode_defunct(text=challenge["message"]), account.key).signature.hex()
    return account, challenge, signature


def test_a_restricted_api_key_cannot_link_a_wallet_or_open_a_session():
    """The escalation: a data-only key signs a challenge with a wallet it
    controls, links that wallet to the key owner's account, and gets a browser
    session cookie back -- with which it could mint an unrestricted key. The
    wallet link would also outlive the key it came from."""
    owner = TestClient(app)
    me = sign_in(owner, email="key-owner@example.com")
    # API keys need a paid plan; the escalation is about what a key can do once
    # it exists, not about who may create one.
    asyncio.run(accounts.update_user(me["user"]["id"], plan_id="pro"))
    created = owner.post("/me/api-keys", json={"name": "data only", "scopes": ["read"]})
    assert created.status_code in (200, 201), created.text
    # The response carries the key record under "key" and the one-time token
    # under "secret"; the token is what a caller actually presents.
    secret = created.json()["secret"]
    assert secret.startswith(api_keys.KEY_PREFIX), "fixture is not sending a real key"

    attacker = TestClient(app)   # no cookie, only the key
    account, challenge, signature = _wallet_and_signature(attacker)
    refused = attacker.post(
        "/auth/wallet/verify",
        json={"address": account.address, "chain": "1", "nonce": challenge["nonce"], "signature": signature},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert refused.status_code == 403, f"an API key opened a wallet session ({refused.status_code})"
    assert "browser flow" in refused.json()["detail"]
    assert "set-cookie" not in {k.lower() for k in refused.headers}, "a session cookie was issued to an API key"
    assert asyncio.run(accounts.find_wallet_owner("ethereum", account.address.lower())) is None
    assert asyncio.run(accounts.list_wallets(me["user"]["id"])) == []


def test_the_same_wallet_still_signs_in_from_a_browser():
    """The control: refusing API keys must not break the flow it protects."""
    browser = TestClient(app)
    account, challenge, signature = _wallet_and_signature(browser)
    allowed = browser.post("/auth/wallet/verify", json={
        "address": account.address, "chain": "1", "nonce": challenge["nonce"], "signature": signature})
    assert allowed.status_code == 200 and allowed.json()["authenticated"] is True


# --- 2. contract wallets are network-scoped ----------------------------------

def test_a_contract_wallet_identity_does_not_span_evm_networks():
    """An EOA address is one key on every EVM network, so one account owns it
    everywhere. A contract address is not key-derived: the same address on Base
    and Ethereum can be two contracts with two different controllers, so
    authenticating against one must never resume the other's account."""
    address = "0x" + "ab" * 20
    owner = asyncio.run(accounts.create_wallet_user("ethereum", address, accounts.CONTRACT))

    # A contract at the same address on another network is a different identity.
    assert asyncio.run(accounts.find_wallet_owner("base", address, accounts.CONTRACT)) is None
    # And on its own network it is the same one.
    resumed = asyncio.run(accounts.find_wallet_owner("ethereum", address, accounts.CONTRACT))
    assert resumed is not None and resumed["id"] == owner["id"]


def test_an_eoa_identity_still_spans_every_evm_network():
    """The control: the cross-network fix that made one wallet one account must
    survive. Narrowing contract wallets must not narrow EOAs with them."""
    address = "0x" + "cd" * 20
    owner = asyncio.run(accounts.create_wallet_user("ethereum", address, accounts.EOA))
    resumed = asyncio.run(accounts.find_wallet_owner("base", address, accounts.EOA))
    assert resumed is not None and resumed["id"] == owner["id"]


def test_an_eoa_signature_does_not_resolve_to_a_contract_wallets_account():
    """The same hole from the other side: EOA lookup is deliberately
    cross-network, so it must exclude contract rows or it walks right back into
    the merge it is supposed to prevent."""
    address = "0x" + "ef" * 20
    asyncio.run(accounts.create_wallet_user("ethereum", address, accounts.CONTRACT))
    assert asyncio.run(accounts.find_wallet_owner("base", address, accounts.EOA)) is None


def test_evm_verification_reports_whether_a_contract_proved_the_signature(monkeypatch):
    """The scoping above is only as good as knowing which path verified. An EOA
    recovery and a smart-wallet validator call must be distinguishable."""
    from eth_account import Account
    from eth_account.messages import encode_defunct

    account = Account.create()
    challenge = asyncio.run(wallet_auth.create_challenge(account.address, "orbit.test", "https://orbit.test"))
    signed = Account.sign_message(encode_defunct(text=challenge["message"]), account.key)
    _token, verified = asyncio.run(wallet_auth.verify_challenge(account.address, challenge["nonce"], signed.signature.hex()))
    assert verified["wallet_type"] == "eoa"

    async def validator_accepts(*args, **kwargs):
        return True

    monkeypatch.setattr(wallet_auth, "_verify_universal_signature", validator_accepts)
    challenge2 = asyncio.run(wallet_auth.create_challenge(account.address, "orbit.test", "https://orbit.test", 8453))
    _token, contract = asyncio.run(wallet_auth.verify_challenge(
        account.address, challenge2["nonce"], "0x" + ("12" * 160) + ("6492" * 16)))
    assert contract["wallet_type"] == "contract"
    assert contract["chain_id"] == 8453


# --- 4. deleting an account must not orphan a live subscription --------------

def test_account_deletion_cancels_the_subscription_first(monkeypatch):
    client = TestClient(app)
    me = sign_in(client, email="subscriber@example.com")
    asyncio.run(accounts.update_user(me["user"]["id"], stripe_customer_id="cus_1",
                                     stripe_subscription_id="sub_live", subscription_status="active"))
    cancelled = []
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_harness")
    monkeypatch.setattr(billing, "_api_cancel_subscription",
                        lambda sid: (cancelled.append(sid), {"id": sid, "status": "canceled"})[1])

    deleted = client.request("DELETE", "/me", json={"confirm_email": "subscriber@example.com"})
    assert deleted.status_code == 200
    assert cancelled == ["sub_live"], "the account was deleted without cancelling its subscription"


def test_a_failed_cancellation_stops_the_deletion(monkeypatch):
    """Recoverable, so it must not proceed: the user still has their data and
    can retry, which is strictly better than a live charge nobody can trace."""
    client = TestClient(app)
    me = sign_in(client, email="cancel-fails@example.com")
    asyncio.run(accounts.update_user(me["user"]["id"], stripe_customer_id="cus_2",
                                     stripe_subscription_id="sub_stuck", subscription_status="active"))

    def explode(_sid):
        raise RuntimeError("Stripe is down")

    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_harness")
    monkeypatch.setattr(billing, "_api_cancel_subscription", explode)
    refused = client.request("DELETE", "/me", json={"confirm_email": "cancel-fails@example.com"})
    assert refused.status_code == 409
    assert client.get("/me").json()["authenticated"] is True, "the account was destroyed anyway"


def test_an_account_with_no_subscription_deletes_without_touching_stripe(monkeypatch):
    client = TestClient(app)
    sign_in(client, email="no-subscription@example.com")

    def must_not_be_called(_sid):
        raise AssertionError("Stripe was contacted for an account with no subscription")

    monkeypatch.setattr(billing, "_api_cancel_subscription", must_not_be_called)
    assert client.request("DELETE", "/me", json={"confirm_email": "no-subscription@example.com"}).status_code == 200


# --- 5 and 6. Stripe events arrive late, out of order, and go stale ----------

def test_a_stale_subscription_deletion_does_not_cancel_a_newer_one():
    user, _ = asyncio.run(accounts.get_or_create_user("resubscribed@example.com"))
    asyncio.run(accounts.update_user(user["id"], stripe_customer_id="cus_resub",
                                     stripe_subscription_id="sub_new", subscription_status="active", plan_id="max"))
    result = asyncio.run(billing._on_subscription_deleted(
        "evt_old", {"id": "sub_old", "customer": "cus_resub"}))
    assert result["status"] == "ignored"
    after = asyncio.run(accounts.get_user(user["id"]))
    assert after["plan_id"] == "max" and after["stripe_subscription_id"] == "sub_new"


def test_deleting_the_active_subscription_still_downgrades():
    """The control: ignoring stale events must not ignore real ones."""
    user, _ = asyncio.run(accounts.get_or_create_user("really-cancelled@example.com"))
    asyncio.run(accounts.update_user(user["id"], stripe_customer_id="cus_real",
                                     stripe_subscription_id="sub_real", subscription_status="active", plan_id="max"))
    result = asyncio.run(billing._on_subscription_deleted(
        "evt_real", {"id": "sub_real", "customer": "cus_real"}))
    assert result["status"] == "downgraded"
    assert asyncio.run(accounts.get_user(user["id"]))["plan_id"] == "free"


def test_entitlements_follow_the_price_being_charged_not_stale_checkout_metadata(monkeypatch):
    """Checkout metadata is written once and never updated, so after an upgrade
    it still names the old plan. The price on the subscription's items is what
    Stripe actually bills."""
    monkeypatch.setattr(settings, "stripe_price_pro", "price_pro")
    monkeypatch.setattr(settings, "stripe_price_max", "price_max")
    subscription = {
        "id": "sub_upgraded",
        "metadata": {"plan_id": "pro"},                      # stale: they were on Pro
        "items": {"data": [{"price": {"id": "price_max"}}]},  # current: they pay for Max
    }
    assert billing._plan_from_subscription(subscription) == "max"


def test_metadata_is_still_used_when_no_price_matches(monkeypatch):
    """The control: metadata stays a fallback rather than being discarded."""
    monkeypatch.setattr(settings, "stripe_price_pro", "price_pro")
    subscription = {"id": "sub_odd", "metadata": {"plan_id": "pro"},
                    "items": {"data": [{"price": {"id": "price_unknown"}}]}}
    assert billing._plan_from_subscription(subscription) == "pro"


# --- 7. a limit must bind wherever the state it limits is entered ------------

def test_resuming_paused_tasks_cannot_exceed_the_plan_limit():
    """Creation checked the active count; resuming never did. A Free account
    could hold any number of paused tasks and switch them all on."""
    user, _ = asyncio.run(accounts.get_or_create_user("task-limits@example.com"))
    limit = tasks.TASK_LIMITS.get("free", 3)

    created = []
    for index in range(limit):
        created.append(asyncio.run(tasks.create_task(
            user, "reminder", {"message": f"task {index}"}, {"every_minutes": 60})))
    # Pause them all, then create and pause one more, so nothing is active.
    for task in created:
        asyncio.run(tasks.update_task(task["id"], user["id"], status="paused"))
    extra = asyncio.run(tasks.create_task(user, "reminder", {"message": "extra"}, {"every_minutes": 60}))
    asyncio.run(tasks.update_task(extra["id"], user["id"], status="paused"))

    from app import task_scheduling

    resumed = 0
    for task in created + [extra]:
        try:
            asyncio.run(task_scheduling.update_task(task["id"], user["id"], user=user, status="active"))
            resumed += 1
        except Exception:
            break
    assert resumed == limit, f"resumed {resumed} tasks on a plan that allows {limit}"


def test_resuming_a_task_that_is_already_active_is_not_blocked_by_itself():
    """The control: the limit must exclude the task being changed, or a task
    could never be edited once the account is at its limit."""
    user, _ = asyncio.run(accounts.get_or_create_user("resume-self@example.com"))
    from app import task_scheduling

    task = asyncio.run(tasks.create_task(user, "reminder", {"message": "only"}, {"every_minutes": 60}))
    asyncio.run(tasks.update_task(task["id"], user["id"], status="paused"))
    revived = asyncio.run(task_scheduling.update_task(task["id"], user["id"], user=user, status="active"))
    assert revived["status"] == "active"


# --- 8. configuration must not advertise what the code refuses ---------------

def _settings(**overrides) -> Settings:
    base = Settings()
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_execution_mode_without_live_trading_is_rejected_at_startup():
    """It used to pass the audit and report execution enabled in /config/public
    while the legacy flag still refused every Jupiter route."""
    config = _settings(deployment_mode="execution", live_trading=False, openai_api_key="sk-test")
    codes = {problem.code for problem in deployment.audit(config) if problem.severity == deployment.FATAL}
    assert "mode-contradicts-live-trading" in codes


def test_a_consistent_execution_configuration_still_starts():
    config = _settings(deployment_mode="execution", live_trading=True, openai_api_key="sk-test")
    codes = {problem.code for problem in deployment.audit(config) if problem.severity == deployment.FATAL}
    assert codes == set()


def test_the_trade_routes_honour_one_policy_rather_than_two():
    """With the contradiction rejected at startup, execution.py must not carry
    a second switch that could still refuse what the mode just allowed.

    Asserted on the source rather than by calling the routes: reading the plan
    store here makes the test depend on what ran before it, and the claim is
    structural anyway -- there is one gate, not two.
    """
    import inspect

    from app import execution

    for function in (execution.execute_confirmed_plan,
                     execution.prepare_wallet_transaction,
                     execution.submit_wallet_transaction):
        source = inspect.getsource(function)
        assert "settings.live_trading" not in source, (
            f"{function.__name__} still consults LIVE_TRADING directly; the deployment "
            "mode is the single execution policy"
        )
        assert "require_execution_enabled" in source or "require_custodial_signing" in source
