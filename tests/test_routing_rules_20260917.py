"""Three routing decisions settled with the user on 2026-09-17.

1. A pasted address wins over own-wallet phrasing: "recent activity for this
   wallet <address>" is a lookup about that address (research), not the
   connected wallet.
2. Perp positions are a wallet's state: "open perps for <address>" and "my
   open perps" are portfolio asks; a pasted address becomes the turn's
   read-only wallet, and a non-EVM address gets a question back rather than
   a guess (Hyperliquid accounts are 0x).
3. An equity ask never enters the token deep dive: "is MSTR a good buy" is
   classed equity by the router, but the deep-dive intercept ran first and
   resolved MSTR to a tokenized-stock namesake.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.nodes import portfolio as portfolio_mod, research as research_mod
from app.routing import intent_router, resolver

EVM = "0xd8dA6BF26964aF9D7eEd9e03E51415D2aA6f0c8f"
SOL = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


# --- 1 · a pasted address wins over "this wallet" ----------------------------

@pytest.mark.parametrize("ask", [f"recent activity for this wallet on ethereum {EVM}", f"what's this wallet holding on base {EVM}"])
def test_a_pasted_address_makes_an_own_wallet_phrasing_a_lookup(ask):
    route = intent_router.route_capabilities(ask)
    assert route is not None and route.intent == "research", (ask, route)


@pytest.mark.parametrize("ask", ["show my recent transactions", "this wallet's recent activity", "how much SOL do I have"])
def test_own_wallet_phrasings_without_an_address_stay_portfolio(ask):
    route = intent_router.route_capabilities(ask)
    assert route is not None and route.intent == "portfolio", (ask, route)


# --- 2 · perp positions are portfolio ------------------------------------------

@pytest.mark.parametrize("ask", [f"open perps for {EVM}", "my open perps", f"hyperliquid positions for {EVM}", "what are my perps"])
def test_perp_position_asks_are_portfolio(ask):
    route = intent_router.route_capabilities(ask)
    assert route is not None and route.intent == "portfolio" and route.reason == "perp_positions" and "perp_positions" in route.capabilities, (ask, route)


def test_perp_market_vocabulary_without_a_wallet_is_still_market_data():
    route = intent_router.route_capabilities("funding rate for BTC on hyperliquid")
    assert route is not None and route.intent == "research" and route.reason != "perp_positions"


def test_a_pasted_address_becomes_the_turns_read_only_wallet_for_a_portfolio_ask():
    async def never_called(program, **kwargs):
        raise AssertionError("an anchored rule must not consult the model")

    out = asyncio.run(resolver.resolve({"request": f"open perps for {EVM}", "history": "", "session_context": {}}, never_called))
    assert out["intent"] == "portfolio" and out["wallet_address"] == EVM
    assert out["routing_decision"]["wallet_source"] == "pasted_address"


def test_a_connected_wallet_is_never_replaced_by_a_pasted_address():
    async def never_called(program, **kwargs):
        raise AssertionError("anchored")

    out = asyncio.run(resolver.resolve({"request": f"open perps for {EVM}", "history": "", "session_context": {}, "wallet_address": "0x" + "ab" * 20}, never_called))
    assert "wallet_address" not in out


def test_the_portfolio_node_answers_perps_from_the_focused_tool(monkeypatch):
    monkeypatch.setattr(portfolio_mod, "_goldrush_hyperliquid_supplement", AsyncMock(return_value="# Hyperliquid\nETH long 2x"))
    out = asyncio.run(portfolio_mod.portfolio_node({"request": f"open perps for {EVM}", "wallet_address": EVM, "capabilities": ["perp_positions", "wallet_intelligence"], "chains": [], "history": "", "session_context": {}}))
    assert out["answer"].startswith("# Hyperliquid") and out["trajectory"]["tool_name_0"] == "goldrush_hyperliquid_positions"


def test_a_non_evm_wallet_with_a_perps_ask_gets_a_question_not_a_guess(monkeypatch):
    """Hyperliquid accounts are 0x; a Solana wallet here is ambiguous, and the
    user's rule is to ask."""
    monkeypatch.setattr(portfolio_mod, "_goldrush_hyperliquid_supplement", AsyncMock(side_effect=AssertionError("must not be called")))
    out = asyncio.run(portfolio_mod.portfolio_node({"request": f"open perps for {SOL}", "wallet_address": SOL, "capabilities": ["perp_positions", "wallet_intelligence"], "chains": [], "history": "", "session_context": {}}))
    assert "Did you mean" in out["answer"] and out["trajectory"] is None


# --- 3 · an equity ask never enters the token deep dive --------------------------

def test_an_equity_advice_ask_skips_the_token_deep_dive(monkeypatch):
    monkeypatch.setattr(research_mod, "_run_token_deep_dive", AsyncMock(side_effect=AssertionError("token deep dive ran for an equity ask")))
    monkeypatch.setattr(research_mod, "_equity_research", AsyncMock(return_value={"answer": "equity brief", "trajectory": None}))
    out = asyncio.run(research_mod.research_node({"request": "is MSTR a good buy", "capabilities": ["equity_research"], "chains": [], "history": "", "session_context": {}}))
    assert out["answer"] == "equity brief"


def test_a_crypto_deep_dive_ask_still_reaches_the_lens(monkeypatch):
    monkeypatch.setattr(research_mod, "_run_token_deep_dive", AsyncMock(return_value={"answer": "lens", "trajectory": None}))
    out = asyncio.run(research_mod.research_node({"request": "deep dive on BONK", "capabilities": ["token_discovery"], "chains": [], "history": "", "session_context": {}}))
    assert out["answer"] == "lens"
