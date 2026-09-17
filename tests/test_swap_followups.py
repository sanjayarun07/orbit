"""Follow-ups that amend a cross-chain swap, from a user transcript:

    swap 0.1sol to usdc on base   -> Relay quote
    .01sol                        -> was routed to the Jupiter planner (asked for slippage)
    50bps                         -> was treated as an unknown asset
    .01sol to usdc on base with 50bps max slippage -> "I need the source chain, the amount..."

A parameter fragment continues the active trade even when a quote is already
awaiting approval, and an amount that opens the message needs no verb.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import execution_policy, main
from app.graph import AgentRun
from tests.conftest import sign_in

from app import experience
from app.nodes import portfolio, trading
from app.routing.controls import is_wallet_connected_ack
from app.routing import resolver
from app.routing.trade_parser import parse_execution_draft


@pytest.fixture(autouse=True)
def _execution_mode(monkeypatch):
    """These tests drive the swap planners; the planners refuse in research
    mode (the test default), so they run in execution mode with custody off."""
    from app.settings import settings as _settings
    monkeypatch.setattr(_settings, "deployment_mode", "execution")
    monkeypatch.setattr(_settings, "live_trading", True)


@pytest.mark.parametrize("request_text, amount, source, output, bps", [
    (".01sol", "0.01", "sol", None, None),
    ("0.5 ETH", "0.5", "ETH", None, None),
    (".01sol to usdc on base with 50bps max slippage", "0.01", "sol", "usdc", 50),
    ("2 sol to usdc", "2", "sol", "usdc", None),
    ("50bps", None, None, None, 50),            # not an amount of a token called bps
    ("10 minutes", None, None, None, None),
])
def test_an_opening_amount_needs_no_verb(request_text, amount, source, output, bps):
    draft = parse_execution_draft(request_text, ())
    assert (draft.amount, draft.input_token, draft.output_token, draft.slippage_bps) == (amount, source, output, bps)


def _state(request, active):
    return {"request": request, "session_context": {"active_workflow": active}}


_QUOTED = {"intent": "cross_chain_swap", "status": "pending_approval", "source_chain": "solana", "destination_chain": "base",
           "amount": "0.1", "input_token": "sol", "output_token": "usdc", "slippage_bps": None}


@pytest.mark.parametrize("fragment", [".01sol", "50bps", "0.05 sol", "with 30 bps"])
def test_a_fragment_amends_a_quote_awaiting_approval(fragment):
    async def no_model(*args, **kwargs):
        raise AssertionError("a fragment on an active trade must not reach the model")

    update = asyncio.run(resolver.resolve(_state(fragment, _QUOTED), no_model))
    assert update["intent"] == "cross_chain_swap" and update["route_source"] == "session_context"
    assert set(update["chains"]) == {"solana", "base"}


def test_the_node_merges_the_fragment_into_the_prior_draft():
    wallet = {"wallet_address": "wallet", "chains": ["solana", "base"]}
    out = asyncio.run(trading.cross_chain_swap_node({**_state(".01sol", _QUOTED), **wallet}))
    draft = out["cross_chain_swap"]
    assert draft is not None and (draft.amount, draft.input_token, draft.output_token, draft.destination_chain) == ("0.01", "sol", "usdc", "base")
    amended = {**_QUOTED, "amount": "0.01"}
    out = asyncio.run(trading.cross_chain_swap_node({**_state("50bps", amended), **wallet}))
    assert out["cross_chain_swap"].slippage_bps == 50 and out["cross_chain_swap"].amount == "0.01"
    # The verb-less full form stands on its own, with or without a prior draft.
    out = asyncio.run(trading.cross_chain_swap_node({**_state(".01sol to usdc on base with 50bps max slippage", None), **wallet}))
    d = out["cross_chain_swap"]
    assert d is not None and (d.amount, d.source_chain, d.destination_chain, d.slippage_bps) == ("0.01", "solana", "base", 50)


@pytest.mark.parametrize("request_text, kept", [("connected", True), ("yes connected", True), ("50bps", True), ("what is the weather", False), ("tell me about bitcoin", False)])
def test_a_pending_quote_survives_an_acknowledgement_but_not_a_topic_switch(request_text, kept):
    previous = {"revision": 3, "focus": None, "active_workflow": dict(_QUOTED)}
    context = experience.advance_session_context(previous, request_text, None, "general", [], [], None)
    assert (context["active_workflow"] is not None) is kept


def test_a_portfolio_check_without_a_wallet_is_parked_for_the_connected_reply():
    """Transcript: "Wallet health check" -> "I need a wallet address"; "yes connected" -> generic clarification."""
    out = asyncio.run(portfolio.portfolio_node({"request": "Wallet health check", "wallet_address": "", "capabilities": ["wallet_health"], "session_context": {}}))
    assert out["pending_wallet_request"] == "Wallet health check" and "reply `connected`" in out["answer"]
    assert is_wallet_connected_ack("yes connected")


def test_a_connected_wallet_is_remembered_until_the_client_disconnects(monkeypatch):
    seen = []

    async def fake_run(message, wallet, history, session_context, action):
        seen.append(wallet)
        return AgentRun(answer="ok", trajectory=None, trade_plan=None, intent="portfolio", capabilities=[])

    monkeypatch.setattr(execution_policy, "run_agent", fake_run)
    client = TestClient(main.app)
    sign_in(client, email="remember-wallet@example.com")
    wallet = "0x1111111111111111111111111111111111111111"
    sid = client.post("/chat", json={"message": "wallet health check", "wallet_address": wallet}).json()["session_id"]
    client.post("/chat", json={"message": "and my token holdings?", "session_id": sid})            # no wallet named
    assert seen == [wallet, wallet]
    assert client.delete(f"/chat/wallet/{sid}").json()["status"] == "forgotten"
    client.post("/chat", json={"message": "wallet health check", "session_id": sid})
    assert seen[-1] == ""
    # Another account cannot forget (or probe) someone else's conversation.
    other = TestClient(main.app); sign_in(other, email="remember-other@example.com")
    assert other.delete(f"/chat/wallet/{sid}").status_code == 404
