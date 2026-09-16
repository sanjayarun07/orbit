import asyncio
import base64
from types import SimpleNamespace

from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

import app.execution as execution


def _signed_transaction(keypair: Keypair, blockhash: Hash | None = None) -> str:
    message = MessageV0.try_compile(keypair.pubkey(), [], [], blockhash or Hash.default())
    transaction = VersionedTransaction(message, [keypair])
    return base64.b64encode(bytes(transaction)).decode()


def test_browser_wallet_prepare_does_not_require_server_private_key(monkeypatch):
    keypair = Keypair()
    transaction = _signed_transaction(keypair)
    plan = SimpleNamespace(
        plan_id="plan",
        status="pending_confirmation",
        confirmation_text="CONFIRM plan",
        wallet_address=str(keypair.pubkey()),
    )

    async def get_plan(_plan_id):
        return plan

    async def get_transaction(_plan):
        return transaction

    monkeypatch.setattr(execution, "get_plan", get_plan)
    monkeypatch.setattr(execution, "get_prepared_transaction", get_transaction)
    monkeypatch.setattr(execution.settings, "live_trading", True)
    monkeypatch.setattr(execution.settings, "solana_private_key", None)

    result = asyncio.run(execution.prepare_wallet_transaction("plan", "CONFIRM plan"))
    assert result["transaction"] == transaction
    assert result["wallet_address"] == str(keypair.pubkey())


def test_browser_wallet_submit_rejects_a_different_transaction(monkeypatch):
    expected_signer = Keypair()
    expected = _signed_transaction(expected_signer)
    different = _signed_transaction(Keypair())
    plan = SimpleNamespace(
        plan_id="plan",
        status="pending_confirmation",
        confirmation_text="CONFIRM plan",
        wallet_address=str(expected_signer.pubkey()),
    )

    async def get_plan(_plan_id):
        return plan

    async def get_transaction(_plan):
        return expected

    monkeypatch.setattr(execution, "get_plan", get_plan)
    monkeypatch.setattr(execution, "get_prepared_transaction", get_transaction)
    monkeypatch.setattr(execution.settings, "live_trading", True)

    try:
        asyncio.run(
            execution.submit_wallet_transaction("plan", "CONFIRM plan", different)
        )
    except ValueError as exc:
        assert "does not match the reviewed trade plan" in str(exc)
    else:
        raise AssertionError("A transaction from a different plan must be rejected")


def test_browser_wallet_may_refresh_only_the_recent_blockhash(monkeypatch):
    signer = Keypair()
    expected = _signed_transaction(signer)
    refreshed = _signed_transaction(
        signer, Hash.from_string("4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4WVhZuQp")
    )
    plan = SimpleNamespace(
        plan_id="plan",
        status="pending_confirmation",
        confirmation_text="CONFIRM plan",
        wallet_address=str(signer.pubkey()),
    )
    marked = []

    async def get_plan(_plan_id):
        return plan

    async def get_transaction(_plan):
        return expected

    async def simulate(_transaction):
        return {"ok": True}

    async def send(_method, _params):
        return str(VersionedTransaction.from_bytes(base64.b64decode(refreshed)).signatures[0])

    async def mark(completed_plan, status):
        completed_plan.status = status
        marked.append(completed_plan)

    async def claim(submitting_plan, signature):
        submitting_plan.status = "submitting"

    monkeypatch.setattr(execution, "get_plan", get_plan)
    monkeypatch.setattr(execution, "get_prepared_transaction", get_transaction)
    monkeypatch.setattr(execution, "simulate_transaction", simulate)
    monkeypatch.setattr(execution, "rpc", send)
    monkeypatch.setattr(execution, "claim_plan_submission", claim)
    monkeypatch.setattr(execution, "update_submission", mark)
    monkeypatch.setattr(execution.settings, "live_trading", True)

    result = asyncio.run(
        execution.submit_wallet_transaction("plan", "CONFIRM plan", refreshed)
    )
    assert result["signature"] == str(VersionedTransaction.from_bytes(base64.b64decode(refreshed)).signatures[0])
    assert result["status"] == "submitted"
    assert marked == [plan]


def test_quote_node_turns_a_provider_rate_limit_into_a_retry_reply(monkeypatch):
    """Live (e2e sweep): Jupiter answered 429 on /swap while building the
    plan and the chat turn became a 500. A provider error is a plain reply:
    nothing was signed or submitted."""
    import asyncio

    import httpx

    from app.nodes import trading

    async def rate_limited(wallet, proposal):
        request = httpx.Request("POST", "https://api.jup.ag/swap/v1/swap")
        raise httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))

    monkeypatch.setattr(trading, "create_trade_plan", rate_limited)
    state = {"proposal": object(), "execution_provider": "jupiter", "wallet_address": "wallet"}
    out = asyncio.run(trading.quote_and_simulate_node(state))
    assert out.get("trade_plan") is None and "rate-limiting" in out["error"] and "Nothing was submitted" in out["error"]

    async def down(wallet, proposal):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(trading, "create_trade_plan", down)
    out = asyncio.run(trading.quote_and_simulate_node(state))
    assert "unavailable" in out["error"] and "ConnectError" in out["error"]
