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
