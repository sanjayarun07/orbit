"""A swap request in research mode is answered as one, not as a quote.

The card in the screenshot of 2026-09-17 read "I'm preparing a Relay quote...
Review the live quote below", locked the intent, warned about destination
gas, and then said "This deployment is running in research mode, so it
cannot move funds." The routes and the browser card knew the mode; the node
that writes the message did not.
"""
import asyncio

import pytest

from app.nodes import trading
from app.settings import settings

STATE = {"request": "swap 0.01 sol on solana to usdc on base", "wallet_address": "So11111111111111111111111111111111111111112",
         "chains": ["solana", "base"], "history": "", "session_context": {}, "missing_fields": [], "execution_provider": "relay"}


@pytest.fixture
def research(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "research")
    monkeypatch.setattr(settings, "live_trading", False)


@pytest.fixture
def execution(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "execution")
    monkeypatch.setattr(settings, "live_trading", True)


def test_a_cross_chain_swap_in_research_mode_is_refused_up_front_with_nothing_locked(research):
    out = asyncio.run(trading.cross_chain_swap_node(dict(STATE)))
    assert "research mode" in out["answer"] and "0.01 sol to usdc on base" in out["answer"]
    assert "preparing" not in out["answer"].lower() and "review the live quote" not in out["answer"].lower()
    assert out["cross_chain_swap"] is None, "a draft would lock the intent and raise the gas advisory over a card that refuses"


def test_a_jupiter_swap_in_research_mode_is_refused_the_same_way(research):
    out = asyncio.run(trading.trade_planner_node({**STATE, "execution_provider": "jupiter"}))
    assert "research mode" in out["answer"] and "preparing" not in out["answer"].lower()
    assert out.get("cross_chain_swap") is None and out.get("proposal") is None


def test_the_mode_gate_comes_before_the_wallet_gate(research):
    out = asyncio.run(trading.cross_chain_swap_node({**STATE, "wallet_address": ""}))
    assert "research mode" in out["answer"] and "pending_wallet_request" not in out


def test_in_execution_mode_the_quote_is_still_prepared(execution):
    out = asyncio.run(trading.cross_chain_swap_node(dict(STATE)))
    assert out["answer"].startswith("I’m preparing a Relay quote") and out["cross_chain_swap"] is not None
