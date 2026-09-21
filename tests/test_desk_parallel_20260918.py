"""The desk after the Movez thread (2026-09-18): the post runs Market
Research, Execution and Risk in parallel and has the Coordinator block on
all three. Ours keeps Risk after the draft, because Risk must judge the
real order and not the request. Two things the comparison did change:

- a trade the charter already refuses in plain text is refused before a
  thesis is written and a quote is fetched, and
- an analysis turn with one voice and no charter skips the Coordinator.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.nodes import team
from app.nodes.trading import charter_precheck

FIELDS = {"max_trade_usd": 100.0, "allowed_chains": ["solana"], "max_slippage_bps": 50}


@pytest.mark.parametrize("request_text,expected", [
    ("swap $500 of SOL to USDC", "you asked for $500.00 but your charter caps a trade at $100.00"),
    ("buy 2k usdc of BONK", "you asked for $2,000.00 but your charter caps a trade at $100.00"),
    ("swap 0.5 SOL to USDC on base", "you asked on base but your charter allows only solana"),
    ("swap 0.1 SOL with 200 bps slippage", "you asked for 200 bps of slippage but your charter caps it at 50 bps"),
    ("swap 0.1 SOL with 3% slippage", "you asked for 300 bps of slippage but your charter caps it at 50 bps"),
])
def test_the_precheck_reads_what_the_request_already_says(request_text, expected):
    assert charter_precheck(FIELDS, request_text) == expected


@pytest.mark.parametrize("request_text", [
    "swap $50 of SOL to USDC", "swap 0.5 SOL to USDC on solana", "swap 0.1 SOL with 30 bps slippage",
    "swap 0.5 SOL to USDC", "thoughts on BONK",
])
def test_the_precheck_never_refuses_what_the_request_does_not_say(request_text):
    assert charter_precheck(FIELDS, request_text) is None, "only the real quote can decide the rest"


def test_no_charter_fields_means_no_precheck():
    assert charter_precheck({}, "swap $500 of SOL to USDC") is None


def test_a_refused_trade_costs_no_thesis_and_no_quote(monkeypatch):
    async def never(*a, **k):
        raise AssertionError("the desk must not work on a trade the charter already refuses")

    monkeypatch.setattr(team, "_market_research", never)
    monkeypatch.setattr(team, "_execution_and_risk", never)
    state = {"request": "swap $500 of SOL to USDC", "team_subintent": "trade", "chains": [],
             "session_context": {"risk_charter": "max $100 a trade, solana only", "risk_charter_fields": FIELDS}}
    out = asyncio.run(team.team_node(state))
    assert out["answer"].startswith("🚫 Blocked by your risk charter: you asked for $500.00")
    assert out["trade_plan"] is None and out["risk_assessment"].verdict == "blocked" and out["intent"] == "trade"


def test_the_precheck_needs_a_written_charter_not_just_fields(monkeypatch):
    seen = {}

    async def research(state, request):
        seen["ran"] = True
        return "thesis", 5, None, None

    monkeypatch.setattr(team, "_market_research", research)
    monkeypatch.setattr(team, "_execution_and_risk", lambda state: _coro(("draft", None, None)))
    state = {"request": "swap $500 of SOL to USDC", "team_subintent": "trade", "chains": [], "session_context": {"risk_charter_fields": FIELDS}}
    asyncio.run(team.team_node(state))
    assert seen.get("ran"), "fields without a set charter are not a charter"


def _coro(value):
    async def run():
        return value
    return run()


async def _resolved(request, caps, chains=()):
    """A resolver that found the token without a question (BONK on Solana)."""
    from app.nodes import research as research_mod
    return research_mod._TokenResolution(f"{request} DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 on solana", chain="solana")


def test_an_analysis_turn_with_no_charter_skips_the_coordinator(monkeypatch):
    async def research(state, request):
        return "Structure: ...\nCONVICTION 6/10\nWRONG IF it loses $1.20", 6, {"tool_name_0": "birdeye_token_overview"}, None

    monkeypatch.setattr(team, "_market_research", research)
    monkeypatch.setattr(team, "_resolve_named_token", _resolved)   # the live resolver is not under test here
    monkeypatch.setattr(team.runtime, "answer", lambda *a, **k: (_ for _ in ()).throw(AssertionError("one voice needs no Coordinator")))
    monkeypatch.setattr(team.answer_gate, "gate", lambda question, result: _coro(result))
    out = asyncio.run(team.team_node({"request": "thoughts on BONK", "team_subintent": "analysis", "chains": [], "session_context": {}}))
    assert out["answer"].startswith("Structure:") and out["team_report"]["conviction"] == 6 and out["intent"] == "research"


def test_a_charter_still_gets_the_coordinator_on_an_analysis_turn(monkeypatch):
    async def research(state, request):
        return "thesis", 6, None, None

    called = {}

    async def synth(program, **kwargs):
        called.update(kwargs)
        return SimpleNamespace(answer="folded against your charter")

    monkeypatch.setattr(team, "_market_research", research)
    monkeypatch.setattr(team, "_resolve_named_token", _resolved)
    monkeypatch.setattr(team.runtime, "answer", synth)
    monkeypatch.setattr(team.answer_gate, "gate", lambda question, result: _coro(result))
    out = asyncio.run(team.team_node({"request": "thoughts on BONK", "team_subintent": "analysis", "chains": [],
                                      "session_context": {"risk_charter": "only verified tokens"}}))
    assert out["answer"] == "folded against your charter" and "only verified tokens" in called["risk"]
