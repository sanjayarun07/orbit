import asyncio
import base64
from datetime import datetime, timedelta, timezone

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from app import execution_policy
from app import execution, plans, reconciliation, relay_tracking
from app.models import TradePlan, SwapProposal


@pytest.fixture
def reviewed(monkeypatch):
    async def no_pool():
        return None
    monkeypatch.setattr(plans, "get_pg_pool", no_pool)
    monkeypatch.setattr(relay_tracking, "get_pg_pool", no_pool)
    monkeypatch.setattr(plans, "_plans", {})
    monkeypatch.setattr(plans, "_prepared_transactions", {})
    monkeypatch.setattr(relay_tracking, "_memory", {})
    monkeypatch.setattr(execution.settings, "live_trading", True)
    key = Keypair()
    tx = VersionedTransaction(MessageV0.try_compile(key.pubkey(), [], [], Hash.default()), [key])
    encoded = base64.b64encode(bytes(tx)).decode()
    now = datetime.now(timezone.utc)
    plan = TradePlan(
        plan_id="test-plan", status="pending_confirmation", created_at=now,
        expires_at=now + timedelta(minutes=2), wallet_address=str(key.pubkey()),
        proposal=SwapProposal(input_mint="in", output_mint="out", amount_atomic=1, slippage_bps=50, reason="test"),
        quote={}, input_token={"mint":"in","symbol":"IN","name":"Input","decimals":9},
        output_token={"mint":"out","symbol":"OUT","name":"Output","decimals":6},
        simulation={"ok":True}, confirmation_text="CONFIRM test-plan",
    )
    plans._plans[plan.plan_id] = plan
    plans._prepared_transactions[plan.plan_id] = encoded
    async def get_tx(_plan):
        return encoded
    async def simulate(_tx):
        return {"ok": True}
    monkeypatch.setattr(execution, "get_prepared_transaction", get_tx)
    monkeypatch.setattr(execution, "simulate_transaction", simulate)
    return plan, tx, encoded


def test_signature_is_stored_before_network_and_timeout_is_not_retryable(reviewed, monkeypatch):
    plan, tx, encoded = reviewed
    async def send(method, args):
        assert plan.status == "submitting"
        assert plan.submission_signature == str(tx.signatures[0])
        raise TimeoutError()
    monkeypatch.setattr(execution, "rpc", send)
    async def run():
        result = await execution.submit_wallet_transaction(plan.plan_id, plan.confirmation_text, encoded)
        assert result["status"] == "submission_unknown"
        with pytest.raises(ValueError):
            await execution.submit_wallet_transaction(plan.plan_id, plan.confirmation_text, encoded)
    asyncio.run(run())


@pytest.mark.parametrize("value,status", [
    (None, "submission_unknown"),
    ({"confirmationStatus":"processed","err":None}, "submitted"),
    ({"confirmationStatus":"confirmed","err":None}, "submitted"),
    ({"confirmationStatus":"finalized","err":None}, "executed"),
    ({"confirmationStatus":"finalized","err":{"InstructionError":[0,"error"]}}, "failed"),
])
def test_reconciliation_requires_finalized_evidence(reviewed, monkeypatch, value, status):
    plan, tx, _ = reviewed
    plan.status = "submission_unknown"
    plan.submission_signature = str(tx.signatures[0])
    async def read(method, args):
        assert method == "getSignatureStatuses"
        return {"value":[value]}
    monkeypatch.setattr(reconciliation, "rpc", read)
    asyncio.run(reconciliation.reconcile_once())
    assert plan.status == status


def test_late_update_cannot_overwrite_final_outcome(reviewed):
    plan, _, _ = reviewed
    plan.status = "executed"
    asyncio.run(plans.update_submission(plan, "submission_unknown"))
    assert plan.status == "executed"


def test_unsettled_plan_survives_quote_expiration_and_pruning(reviewed):
    plan, tx, _ = reviewed
    plan.status = "submitted"
    plan.submission_signature = str(tx.signatures[0])
    plan.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    plans._prune_memory()
    assert plans._plans[plan.plan_id] is plan


def test_prebroadcast_persistence_failure_never_calls_rpc(reviewed, monkeypatch):
    plan, _, encoded = reviewed
    async def fail(*args):
        raise RuntimeError("database unavailable")
    async def forbidden(*args):
        raise AssertionError("Must not broadcast without durable claim")
    monkeypatch.setattr(execution, "claim_plan_submission", fail)
    monkeypatch.setattr(execution, "rpc", forbidden)
    with pytest.raises(RuntimeError):
        asyncio.run(execution.submit_wallet_transaction(plan.plan_id, plan.confirmation_text, encoded))


def test_relay_attempt_claim_is_once_only(reviewed):
    async def run():
        outcomes = await asyncio.gather(relay_tracking.track("request-1234567890"), relay_tracking.track("request-1234567890"))
        assert sum(o["execution_claimed"] for o in outcomes) == 1
        assert all(o["status"] == "submission_unknown" for o in outcomes)
    asyncio.run(run())


def test_relay_new_quote_cannot_execute_same_chat_turn_twice(reviewed):
    async def run():
        first = await relay_tracking.track("request-1234567890", "chat", 3)
        second = await relay_tracking.track("newquote-1234567890", "chat", 3)
        assert first["execution_claimed"] is True
        assert second["execution_claimed"] is False
        assert (await relay_tracking.for_turn("chat", 3))["request_id"] == first["request_id"]
    asyncio.run(run())


def test_relay_stale_turn_rejected_before_wallet_execution(reviewed, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    class Lease:
        async def release(self):
            pass
    async def lease(_):
        return Lease()
    async def context(_):
        return {"revision":4,"active_workflow":{"intent":"cross_chain_swap"}}
    monkeypatch.setattr(main, "acquire_session_turn", lease)
    monkeypatch.setattr(main, "get_session_context", context)
    response = TestClient(main.app).post("/executions/relay/request-1234567890", json={"session_id":"chat","revision":3})
    assert response.status_code == 409
    assert not relay_tracking._memory


def test_chat_plan_wallet_submission_and_reconciliation(reviewed, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, sessions
    from app.graph import AgentRun
    plan, tx, encoded = reviewed
    async def no_redis():
        return None
    monkeypatch.setattr(sessions, "get_redis", no_redis)
    async def agent(*args):
        return AgentRun("Review this swap", None, plan, "trade", ["swap"])
    async def send(method, args):
        return str(tx.signatures[0])
    monkeypatch.setattr(execution_policy, "run_agent", agent)
    monkeypatch.setattr(execution, "rpc", send)
    client = TestClient(main.app)
    from tests.conftest import sign_in
    sign_in(client)  # a wallet-bound turn needs a signed-in account
    response = client.post("/chat", json={"message":"swap 0.01 SOL to USDC on Solana with 50bps", "wallet_address":plan.wallet_address})
    assert response.status_code == 200
    assert response.json()["trade_plan"]["plan_id"] == plan.plan_id
    prepared = client.post(f"/trade-plans/{plan.plan_id}/wallet-transaction", json={"confirmation_text":plan.confirmation_text})
    assert prepared.status_code == 200
    submitted = client.post(f"/trade-plans/{plan.plan_id}/submit-wallet-transaction", json={"confirmation_text":plan.confirmation_text, "signed_transaction":encoded})
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "submitted"
    async def confirmed(method, args):
        return {"value":[{"confirmationStatus":"finalized","err":None}]}
    monkeypatch.setattr(reconciliation, "rpc", confirmed)
    asyncio.run(reconciliation.reconcile_once())
    assert client.get(f"/trade-plans/{plan.plan_id}").json()["status"] == "executed"
