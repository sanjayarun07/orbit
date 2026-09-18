"""Transcript of 2026-09-18: "What are the risks of buying BONK?" was answered
by the protocol knowledge base with "the passages do not contain any specific
information about the risks of buying BONK" and citations for Lombard and
Spark. Three things let that happen and each is closed here: the deep-dive
lens did not recognise the phrasing; the knowledge matcher claimed the ask
because "BONK" resolved to an incident in the registry; and the knowledge
tool returned off-topic passages instead of standing aside."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.knowledge import tool as kb
from app.nodes import research as research_mod

Q = "What are the risks of buying BONK?"


@pytest.fixture(scope="module", autouse=True)
def _snapshot():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "routing_eval"))
    from harness import warm_knowledge_snapshot
    result = warm_knowledge_snapshot()
    if asyncio.iscoroutine(result):
        asyncio.run(result)


@pytest.mark.parametrize("text", [Q, "risks of holding $WIF", "how risky is BONK", "is BONK risky", "is PEPE worth buying", "what are the risks of investing in JUP"])
def test_ownership_risk_phrasings_are_due_diligence_asks(text):
    assert research_mod._DEEPDIVE.search(text), text
    assert research_mod._DEEPDIVE_TOKEN.search(text), f"the token must be found in: {text}"


def test_the_bonk_risk_question_runs_the_deep_dive_and_never_reaches_the_knowledge_base(monkeypatch):
    monkeypatch.setattr(research_mod, "_run_token_deep_dive", AsyncMock(return_value={"answer": "lens", "trajectory": {"tool_name_0": "token_deep_dive"}}))
    monkeypatch.setattr(research_mod, "_gather_planned", AsyncMock(side_effect=AssertionError("the lens answers; no tool plan runs")))
    monkeypatch.setattr(research_mod, "get_provider_router", lambda: SimpleNamespace(try_route_across=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no ranked route")), matched_capabilities=lambda *a: ()))
    out = asyncio.run(research_mod.research_node({"request": Q, "capabilities": ["web_research", "token_security"], "chains": [], "history": "", "session_context": {}}))
    assert out["answer"] == "lens" and out["trajectory"]["tool_name_0"] == "token_deep_dive"


def test_the_knowledge_matcher_keeps_ownership_risk_asks_but_still_takes_protocol_and_incident_questions():
    if kb.snapshot() is None or len(kb.snapshot()) == 0:
        pytest.skip("no knowledge snapshot in this environment")
    assert not kb.matches(Q)
    assert not kb.matches("is it safe to buy BONK")
    assert kb.matches("what are the risks of Aave v3 E-mode"), "a protocol risk question is documentation"
    assert kb.matches("what happened in the BONK Fun exploit"), "an incident question is documentation"


def test_off_topic_passages_are_not_an_answer(monkeypatch):
    chunk = SimpleNamespace(heading="Risks", content="Bitcoin Earn carries smart-contract risk.")
    hits = [SimpleNamespace(chunk=chunk, score=0.4, sources=["semantic"], document_title="Bitcoin Earn", document_url="https://docs.lombard.finance", protocol_name="Lombard LBTC")]
    bonk = SimpleNamespace(entity=SimpleNamespace(canonical_name="BONK Fun exploit — 2026-03-11", symbol="BONK", entity_type="incident"), confidence=0.75)
    monkeypatch.setattr(kb, "_run", lambda coro: (coro.close() or (hits, SimpleNamespace(entities=[bonk], graph_expanded=[]))))
    with pytest.raises(RuntimeError, match="No indexed knowledge matched"):
        kb.knowledge_base_search(Q)
    # the same passages ARE the answer when the question named their protocol
    lombard = SimpleNamespace(entity=SimpleNamespace(canonical_name="Lombard LBTC", symbol="LBTC", entity_type="protocol"), confidence=0.9)
    monkeypatch.setattr(kb, "_run", lambda coro: (coro.close() or (hits, SimpleNamespace(entities=[lombard], graph_expanded=[]))))
    assert "Bitcoin Earn carries smart-contract risk" in kb.knowledge_base_search("what are the risks of Lombard Bitcoin Earn")
