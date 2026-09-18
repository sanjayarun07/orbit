"""Ten deterministic tool-ranking misses on the 2026-09-17 catalog cases, and
the causes behind them: vocabulary the matchers lacked ("oi", "spiked",
"can I sell", "hold", "delistings", "newest"), a rule order that let
address+check beat security jargon, and a chain helper that refused a bare
Solana address so no Solana wallet tool was ever a candidate. Plus the
product rule: a security ask that names no token is asked about, not guessed.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app import market_providers
from app.nodes import research as research_mod
from app.routing import intent_router

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "routing_eval"))
from harness import _ranked_tools  # noqa: E402

SOL_WALLET = "GThUX1Atko4tqhN2NaiTazWSeFWMuiUvfB4V7Y5jsPQ9"
PEPE = "0x6982508145454Ce325dDbE47a25d4ec3d2311933"
CAKE = "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82"
AERO = "0x940181a94A35A4569E4529A3CDfB74e38FD98631"
WALLET = "0xd8dA6BF26964aF9D7eEd9e03E51415D2aA6f0c8f"


def test_a_bare_solana_address_names_its_chain_and_an_evm_address_does_not():
    assert market_providers._chain(f"recent transactions for {SOL_WALLET}") == "solana"
    with pytest.raises(ValueError):
        market_providers._chain(f"recent transactions for {WALLET}")
    route = intent_router.route_capabilities(f"Show recent transactions for {SOL_WALLET}")
    assert route is not None and route.chains == ("solana",)


@pytest.mark.parametrize("ask,expected", [
    # Wallet tracking and portfolio go to Mobula first by user decision
    # (2026-09-18); the chain-native tools stay ranked behind it.
    (f"Show recent transactions for {SOL_WALLET}", {"mobula_wallet_history", "helius_wallet_transactions"}),
    ("hyperliquid oi right now", {"goldrush_hyperliquid_market"}),
    ("what coins spiked hardest today", {"coingecko_gainers_losers"}),
    ("newest token profiles", {"dexscreener_latest_profiles"}),
    (f"what is {AERO} on base", {"coingecko_token_by_contract", "birdeye_token_overview"}),
    # Mobula's card (LP burned/locked/unlocked per pool) and GoPlus's flags are
    # both right answers to a rug check, and the multi-tool plan runs both.
    (f"rug check {CAKE} on bsc", {"mobula_token_security", "goplus_token_security"}),
    # GoPlus reports is_honeypot and sell tax too: either security tool is a correct first pick.
    (f"can I sell {PEPE} on ethereum", {"honeypot_token_security", "goplus_token_security"}),
    (f"honeypot check {PEPE} on ethereum", {"honeypot_token_security", "goplus_token_security"}),
    (f"what does {WALLET} hold on ethereum", {"mobula_wallet_portfolio", "bitquery_wallet_balances", "goldrush_wallet_balances"}),
    ("recent delistings", {"exchange_listing_announcements"}),
])
def test_the_deterministic_ranking_puts_the_right_tool_first(ask, expected):
    ranked = _ranked_tools(ask, semantic=False)
    assert ranked is not None, f"Layer-1 abstained on {ask!r}"
    intent, caps, chains, tools = ranked
    assert tools and tools[0] in expected, f"{ask!r}: ranked {tools[:4]} (caps {caps}, chains {chains})"


@pytest.mark.parametrize("ask", [f"can I sell {PEPE} on ethereum", f"rug check {CAKE}", "is it sellable?"])
def test_security_jargon_outranks_trade_verbs_and_the_generic_address_check(ask):
    route = intent_router.route_capabilities(ask)
    assert route is not None and route.reason == "token_security_jargon" and route.intent == "research", (ask, route)


def test_a_chainless_gainers_ask_is_market_wide_not_unanswerable(monkeypatch):
    from app import additional_providers, market_providers
    assert additional_providers._has_gainers_chain("what coins spiked hardest today")
    for chain in market_providers._CHAIN_NAMES:
        assert additional_providers._has_gainers_chain(f"biggest losers on {chain} today"), chain
    # Every chain the helper knows has a category today; if one is ever added
    # without, the gate must refuse it rather than silently go market-wide.
    monkeypatch.setitem(additional_providers._GAINERS_CATEGORY, "solana", None)
    additional_providers._GAINERS_CATEGORY.pop("solana")
    assert not additional_providers._has_gainers_chain("top gainers on solana")


def test_a_security_ask_that_names_no_token_is_asked_about_not_guessed(monkeypatch):
    monkeypatch.setattr(research_mod, "_resolve_named_token", AsyncMock(side_effect=AssertionError("must not resolve or search")))
    out = asyncio.run(research_mod.research_node({"request": "honeypot check", "capabilities": ["token_security", "token_discovery"], "chains": [], "history": "", "session_context": {}}))
    assert "Which token" in out["answer"] and out["trajectory"] is None
