"""When the router is not sure what a message is about, the subject is looked
up on the web first and the answer routes the turn (user rule, 2026-09-18).
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.routing import resolver, subject_probe
from app.routing.semantic import embedding_router
from app.settings import settings


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    subject_probe.reset()
    resolver._understanding_cache.clear()
    monkeypatch.setattr(subject_probe, "perplexity_available", lambda: True)
    # The context search is opted into per test; never the real network here.
    monkeypatch.setattr(subject_probe, "perplexity_finance_search", lambda q: None)
    monkeypatch.setattr(subject_probe, "perplexity_web_search", lambda q: None)
    yield
    subject_probe.reset()


@pytest.mark.parametrize("text,subject", [
    ("Audit report on ANSEM", "ANSEM"), ("what about $GIGA", "GIGA"), ("tell me about Hyperliquid", "Hyperliquid"),
    ("is PUMP worth it", "PUMP"), ("what is an ETF", None), ("hi", None), ("price of SOL", "SOL"), ("Reliance results", "Reliance"), ("What is the best DEX", None),
])
def test_the_subject_worth_looking_up(text, subject):
    assert subject_probe.subject_of(text) == subject


def test_the_probe_asks_once_per_subject_and_caches_the_answer(monkeypatch):
    calls = []

    def search(tool, prompt, instructions):
        calls.append((tool, prompt))
        return "Here you go: " + json.dumps({"kind": "token", "name": "The Black Bull", "symbol": "ANSEM", "chain": "solana", "summary": "a Solana memecoin", "confidence": 0.9})

    monkeypatch.setattr(subject_probe, "perplexity_invoke", search)
    first = subject_probe.probe("Audit report on ANSEM")
    second = subject_probe.probe("what about ANSEM?")
    assert first["kind"] == "token" and first["chain"] == "solana" and first["subject"] == "ANSEM"
    assert second == first and len(calls) == 1 and calls[0][0] == "web_search" and "ANSEM" in calls[0][1]


@pytest.mark.parametrize("found,intent,caps,chains", [
    ({"kind": "token", "symbol": "ANSEM", "chain": "solana", "confidence": 0.9}, "research", ["token_security", "token_discovery", "market_data"], ["solana"]),
    ({"kind": "equity", "symbol": "RELIANCE", "exchange": "NSE", "confidence": 0.8}, "research", ["equity_research"], []),
    ({"kind": "protocol", "name": "Hyperliquid", "chain": "other", "confidence": 0.9}, "research", ["knowledge", "defi_data", "web_research"], []),
    ({"kind": "person", "name": "Ansem", "confidence": 0.9}, "research", ["web_research"], []),
])
def test_a_confident_probe_becomes_a_route(found, intent, caps, chains):
    route = subject_probe.route_from({**found, "subject": found.get("symbol") or found.get("name")}, "audit report on X")
    assert route["intent"] == intent and route["capabilities"] == caps and route["chains"] == chains and route["route_source"] == "subject_probe"


def test_a_low_confidence_or_unknown_probe_does_not_route():
    assert subject_probe.route_from({"kind": "token", "symbol": "X", "chain": "solana", "confidence": 0.3}, "x") is None
    assert subject_probe.route_from({"kind": "other", "confidence": 0.95}, "x") is None


def _abstaining_model():
    async def model(program, **kwargs):
        return SimpleNamespace(understanding={"speech_act": "abstain", "domain": "general", "explicit_action": False, "confidence": 0.3})
    return model


def test_an_uncertain_turn_is_routed_by_the_probe_instead_of_asking_back(monkeypatch):
    monkeypatch.setattr(subject_probe, "perplexity_invoke", lambda tool, prompt, instructions: json.dumps(
        {"kind": "token", "name": "The Black Bull", "symbol": "ANSEM", "chain": "solana", "summary": "memecoin", "confidence": 0.92}))
    out = asyncio.run(resolver.resolve({"request": "what about ANSEM", "history": "", "session_context": {}}, _abstaining_model(), embedding_factory=embedding_router))
    assert out["intent"] == "research" and "token_security" in out["capabilities"] and out["chains"] == ["solana"]
    assert out["contextual_request"] == "what about ANSEM (ANSEM token on solana)"
    assert out["routing_decision"]["method"] == "subject_probe" and out["routing_decision"]["reason"] == "probe:token"


def test_with_nothing_found_the_turn_still_asks_back(monkeypatch):
    monkeypatch.setattr(subject_probe, "perplexity_invoke", lambda tool, prompt, instructions: "no idea")
    out = asyncio.run(resolver.resolve({"request": "what about ANSEM", "history": "", "session_context": {}}, _abstaining_model(), embedding_factory=embedding_router))
    assert out["intent"] == "general" and out.get("clarification")
    assert out["routing_decision"]["reason"] == "model_uncertain"


def test_a_strong_rule_still_wins_over_the_probe(monkeypatch):
    called = []
    monkeypatch.setattr(subject_probe, "probe", lambda request: called.append(request) or None)
    out = asyncio.run(resolver.resolve({"request": "rug check 9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", "history": "", "session_context": {}}, _abstaining_model(), embedding_factory=embedding_router))
    assert "token_security" in out["capabilities"] and not called, "an anchored rule never needs a web look-up"


# --- search first, synthesize, then the tools (user rule, 2026-09-18) ---------

def test_the_context_search_picks_finance_for_a_market_question_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(subject_probe, "perplexity_finance_search", lambda q: calls.append(("finance", q)) or "OpenLedger (OPEN) vests over 48 months.")
    monkeypatch.setattr(subject_probe, "perplexity_web_search", lambda q: calls.append(("web", q)) or "Ansem is a crypto trader on X.")
    assert subject_probe.context_search("what is OPEN trading at") == "OpenLedger (OPEN) vests over 48 months."
    assert subject_probe.context_search("what is OPEN trading at") == "OpenLedger (OPEN) vests over 48 months."
    assert subject_probe.context_search("who is ansem") == "Ansem is a crypto trader on X."
    assert subject_probe.context_search("OPEN token unlock schedule") == "Ansem is a crypto trader on X."
    assert calls == [("finance", "what is OPEN trading at"), ("web", "who is ansem"), ("web", "OPEN token unlock schedule")], \
        "one search per message; finance only for a price or figure (it rejects answers without a live quote)"
    assert subject_probe.context_card("OPEN token unlock schedule", "text") == "# Web context — OPEN\n\ntext"


def test_an_uncertain_turn_carries_the_webs_answer_alongside_the_probed_route(monkeypatch):
    monkeypatch.setattr(subject_probe, "perplexity_invoke", lambda tool, prompt, instructions: json.dumps(
        {"kind": "token", "name": "OpenLedger", "symbol": "OPEN", "chain": "bsc", "summary": "AI data token", "confidence": 0.9}))
    monkeypatch.setattr(subject_probe, "perplexity_web_search", lambda q: "OpenLedger (OPEN): 12-month cliff, then 36 months of monthly vesting.")
    out = asyncio.run(resolver.resolve({"request": "what about OPEN", "history": "", "session_context": {}}, _abstaining_model(), embedding_factory=embedding_router))
    assert out["intent"] == "research" and out["chains"] == ["bsc"]
    assert out["web_context"].startswith("OpenLedger (OPEN): 12-month cliff") and out["routing_decision"]["web_context"] is True


def test_with_no_settled_subject_the_webs_answer_still_stands_before_asking(monkeypatch):
    monkeypatch.setattr(subject_probe, "perplexity_invoke", lambda tool, prompt, instructions: "no idea")
    monkeypatch.setattr(subject_probe, "perplexity_web_search", lambda q: "Ansem is a crypto trader on X known for memecoin calls.")
    out = asyncio.run(resolver.resolve({"request": "what about Ansem", "history": "", "session_context": {}}, _abstaining_model(), embedding_factory=embedding_router))
    assert out["intent"] == "research" and out["capabilities"] == ["web_research"] and not out.get("clarification")
    assert out["web_context"].startswith("Ansem is a crypto trader") and out["routing_decision"]["reason"] == "model_uncertain:web_context"


def test_the_research_node_reads_the_web_answer_with_the_tools_cards(monkeypatch):
    from app import composition
    from app.nodes import research

    async def tools(state, sink):
        return {"answer": "# Unlock schedule — OPEN\n| 2026-09-08 | 10,000,000 tokens |", "trajectory": {"tool_name_0": "defillama_token_unlocks", "observation_0": "x"}}

    async def synthesize(request, cards, trajectory, advice=False):
        return "**Taken together**\n\nThe cliff ends in September.\n\n---\n\n" + cards

    monkeypatch.setattr(research, "_research_node", tools)
    monkeypatch.setattr(composition, "synthesize", synthesize)
    out = asyncio.run(research.research_node({"request": "OPEN token unlock schedule", "capabilities": ["listing_events"], "chains": [],
                                              "web_context": "OpenLedger (OPEN): 12-month cliff, then 36 months of monthly vesting.", "session_context": {}}))
    assert out["answer"].startswith("**Taken together**")
    assert "# Web context — OPEN" in out["answer"] and "# Unlock schedule — OPEN" in out["answer"]
    assert out["trajectory"]["tool_name_0"] == research.WEB_CONTEXT_TOOL and out["trajectory"]["tool_name_1"] == "defillama_token_unlocks"

    async def same(state, sink):
        return {"answer": "the web card", "trajectory": {"tool_name_0": research.WEB_CONTEXT_TOOL}}

    monkeypatch.setattr(research, "_research_node", same)
    out = asyncio.run(research.research_node({"request": "what about Ansem", "capabilities": ["web_research"], "chains": [], "web_context": "Ansem is a trader.", "session_context": {}}))
    assert out["answer"] == "the web card", "the web answer is never combined with itself"
