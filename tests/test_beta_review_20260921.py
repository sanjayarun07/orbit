"""Fixes from the first beta user's logged turns (turn log, 2026-09-21)."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app import handles, jupiter as jupiter_mod, portfolio as portfolio_mod
from app.nodes import portfolio as portfolio_node_mod, research

EVM = "0x9ce0cb4a193acbce0dca3283972341aed6f3f614"
SOL = "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ"
WSOL = portfolio_mod.WRAPPED_SOL_MINT


# ---- "@frankdegods wallet analysis" answered for the connected EVM wallet as "Ethereum-style address" ----

def test_a_handle_in_a_wallet_ask_is_recognised_only_without_an_address():
    assert handles.social_handle("@frankdegods wallet analysis") == "frankdegods"
    assert handles.social_handle("what does @ansem hold") == "ansem"
    assert handles.social_handle(f"@frankdegods wallet {SOL}") is None       # an address wins
    assert handles.social_handle("email me at a@b.co about my wallet") is None
    assert handles.social_handle("@frankdegods is bullish on SOL") is None    # not a wallet ask


def test_portfolio_node_answers_a_handle_honestly_instead_of_using_the_connected_wallet(monkeypatch):
    monkeypatch.setattr(portfolio_node_mod, "build_portfolio_snapshot", AsyncMock(side_effect=AssertionError("must not read the connected wallet")))
    out = asyncio.run(portfolio_node_mod.portfolio_node({"request": "@frankdegods wallet analysis", "wallet_address": EVM, "capabilities": ["token_holdings"],
                                                          "chains": [], "history": "", "session_context": {}}))
    assert "@frankdegods" in out["answer"] and "Paste the wallet address" in out["answer"] and out["trajectory"] is None
    assert "Ethereum" not in out["answer"]


def test_research_node_answers_a_handle_the_same_way():
    out = asyncio.run(research.research_node({"request": "@frankdegods wallet analysis", "capabilities": ["wallet_intelligence"], "chains": [], "session_context": {}}))
    assert "@frankdegods" in out["answer"] and out["trajectory"] is None


def test_an_evm_connected_wallet_is_composed_per_chain_not_read_from_solana_rpc(monkeypatch):
    monkeypatch.setattr(portfolio_node_mod, "build_portfolio_snapshot", AsyncMock(side_effect=AssertionError("Solana RPC must not see an EVM wallet")))
    monkeypatch.setattr(portfolio_node_mod, "_compose_wallet_portfolio", AsyncMock(return_value=("# Wallet token balances\n| ETH | 0.8 |", {"tool_name_0": "goldrush_wallet_balances"})))
    out = asyncio.run(portfolio_node_mod.portfolio_node({"request": "show my holdings", "wallet_address": EVM, "capabilities": ["token_holdings"],
                                                          "chains": ["base"], "history": "", "session_context": {}}))
    assert "| ETH | 0.8 |" in out["answer"] and out["trajectory"]["tool_name_0"] == "goldrush_wallet_balances"
    portfolio_node_mod._compose_wallet_portfolio.assert_awaited_once_with(EVM, "base")


# ---- a 1,170-token Solana wallet showed SOL with no price ----

def test_one_failed_jupiter_batch_keeps_the_others(monkeypatch):
    calls = []

    async def search(self, query):
        calls.append(query)
        if len(calls) == 2:
            raise RuntimeError("429")
        return [{"id": m, "symbol": "T", "usdPrice": 1.0} for m in query.split(",")]
    monkeypatch.setattr(jupiter_mod.JupiterClient, "search_tokens", search)
    mints = [f"{i:0>44}".replace("0", "A") if i else WSOL for i in range(250)]
    found = asyncio.run(jupiter_mod.jupiter.tokens_by_mints(mints))
    assert len(calls) == 3 and WSOL in found and len(found) == 150     # chunk 2 lost, chunks 1 and 3 kept


def test_sol_is_always_among_the_retried_prices(monkeypatch):
    async def batch(mints):
        return {}                                                     # the batch answered nothing
    retried = []

    async def lookup(mint):
        retried.append(mint)
        return {"symbol": "SOL", "usdPrice": 118.0} if mint == WSOL else None
    monkeypatch.setattr(jupiter_mod.jupiter, "tokens_by_mints", batch)
    monkeypatch.setattr(portfolio_mod, "_price_lookup", lookup)
    mints = ["M" * 44 + str(i) for i in range(30)] + [WSOL]              # SOL last, past the retry window
    found = asyncio.run(portfolio_mod._price_mints(mints))
    assert retried[0] == WSOL and found[WSOL]["usdPrice"] == 118.0 and len(retried) == portfolio_mod._FALLBACK_LOOKUPS + 1


# ---- second review (2026-09-22): a forecast ask, and promotion read as sentiment ----

from app import answer_gate, sentiment_analyst as sa, x_tweets
from app.signals import Subject


def test_the_gate_never_asks_to_name_a_token_the_question_already_names():
    verdict = {"missing": "price prediction for the next 5-10 hours", "subject": "Bitcoin price prediction"}
    text = answer_gate._could_not_find("What will happen for BTC in next 5-10 hours ?", verdict)
    assert "Name the token" not in text and "won't guess" in text and "funding and open interest" in text
    plain = answer_gate._could_not_find("BONK treasury address", {"missing": "the treasury address", "subject": ""})
    assert "For BONK I can pull" in plain and "Name the token" not in plain
    generic = answer_gate._could_not_find("who is behind it", {"missing": "the team", "subject": ""})
    assert "Name the token" in generic


def test_a_forecast_ask_is_answered_as_the_tape_not_refused(monkeypatch):
    seen = {}

    async def inner(state, sink):
        seen["request"] = state["request"]
        return {"answer": "# BTC market\nprice $85,821 · 24h +5.8% · funding +0.01%", "trajectory": {"tool_name_0": "birdeye_token_overview"}}
    monkeypatch.setattr(research, "_research_node", inner)
    monkeypatch.setattr(research, "_web_context_part", lambda state: None)
    out = asyncio.run(research.research_node({"request": "What will happen for BTC in next 5-10 hours ?", "capabilities": ["web_research"], "chains": [], "session_context": {}}))
    assert "funding" in seen["request"] and "BTC" in seen["request"] and "next 5-10 hours" not in seen["request"]
    assert out["answer"].startswith("Nobody's data says where BTC goes in the next 5-10 hours") and "price $85,821" in out["answer"]


def test_a_promotional_sample_is_flagged_and_its_vote_halved():
    promo = [x_tweets.normalize({"id": str(i), "text": f"$ZEC whitelist spots open, claim now #{i}", "author": {"userName": f"a{i}"}, "likeCount": 5}) for i in range(8)]
    real = [x_tweets.normalize({"id": str(100 + i), "text": "$ZEC holding support nicely", "author": {"userName": f"b{i}"}, "likeCount": 5}) for i in range(4)]
    s = x_tweets.stats(promo + real, symbol="ZEC")
    assert s["promo_share_pct"] == pytest.approx(66.7, abs=0.1)
    subject = Subject(kind="token", id="ZEC", symbol="ZEC")
    j = {"stance": "bullish", "stance_probabilities": {"bullish": 0.93, "bearish": 0.01, "neutral": 0.06}, "stance_confidence": 0.9,
         "mood": "Optimistic / bullish", "mood_score": 3.0, "catalyst": "Rumour or minor update", "catalyst_score": 1.0, "organic_probability": 0.84}
    vote = sa.to_signal(subject, "2026-09-22T00:00:00+00:00", {**s, "sample_size": 50}, j)
    assert vote.metadata["damped"] and vote.value == pytest.approx(0.92 * 0.5) and "promotion" in vote.reasoning
    card = sa.render_card("ZEC", {"query": "$ZEC", "new": 12}, s, j)
    assert "67% of the sample is promotion" in card
