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


# --- transcript of 2026-09-18: "Audit report on ANSEM" ------------------------------
# Four turns about one token: the audit ask went to web search (French public-
# sector audit reports); "Audit report on ANSEM TOKEN" answered "Which token
# should I check?" although ANSEM had just been resolved and was in focus.

@pytest.mark.parametrize("text,ticker", [
    ("Audit report on ANSEM", "ANSEM"), ("Audit report on ANSEM TOKEN", "ANSEM"), ("Solana ANSEM token", "ANSEM"),
    ("ANSEM token on solana", "ANSEM"), ("is BONK audited", "BONK"), ("security check for WIF", "WIF"), ("due diligence on JUP", "JUP"),
])
def test_audit_and_token_phrasings_name_their_ticker(text, ticker):
    assert research_mod._named_tickers(text) == [ticker]


def test_generic_token_words_are_not_tickers():
    assert research_mod._named_tickers("what is the ERC20 token standard") == []
    assert research_mod._named_tickers("audit report on the company") == []


@pytest.mark.parametrize("text", ["Audit report on ANSEM", "is BONK audited", "security check for WIF", "is it audited?"])
def test_an_audit_ask_is_a_token_security_rule(text):
    from app.routing import intent_router
    route = intent_router.route_capabilities(text)
    assert route is not None and "token_security" in route.capabilities, text


def _security_state(request, **extra):
    return {"request": request, "capabilities": ["token_security", "token_discovery"], "chains": [], "history": "", "session_context": {}, **extra}


def _stub_downstream(monkeypatch, seen):
    async def resolve(request, capabilities, user_chains=()):
        seen["resolve"] = request
        return SimpleNamespace(clarification=None, pending=None, request=request + " 9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump on solana" if "9cRCn9" not in request else request, chain="solana")

    monkeypatch.setattr(research_mod, "_resolve_named_token", resolve)
    monkeypatch.setattr(research_mod, "direct_mcp_request", lambda request, token_subject=False: None)
    monkeypatch.setattr(research_mod, "is_crypto_trends_query", lambda request: False)

    async def gathered(request, capabilities, chains, state, scope_web=False):
        seen["gathered"] = (request, tuple(capabilities), tuple(chains))
        return {"answer": "# Token security dossier", "trajectory": {"tool_name_0": "solana_token_security"}}

    monkeypatch.setattr(research_mod, "_gather_planned", gathered)
    monkeypatch.setattr(research_mod, "get_provider_router", lambda: SimpleNamespace(matched_capabilities=lambda *a: (), try_route_across=lambda *a, **k: None, plan_across=lambda *a, **k: []))


def test_an_audit_ask_that_names_the_token_runs_the_security_check_instead_of_asking(monkeypatch):
    seen = {}
    _stub_downstream(monkeypatch, seen)
    out = asyncio.run(research_mod.research_node(_security_state("Audit report on ANSEM TOKEN")))
    assert out["answer"].startswith("# Token security dossier"), out["answer"]
    assert seen["resolve"] == "Audit report on ANSEM TOKEN" and "token_security" in seen["gathered"][1]


def test_a_security_follow_up_uses_the_token_in_focus(monkeypatch):
    seen = {}
    _stub_downstream(monkeypatch, seen)
    focus = {"kind": "token", "label": "ANSEM", "address": "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", "chain": "solana"}
    out = asyncio.run(research_mod.research_node(_security_state("is it audited?", session_context={"focus": focus})))
    assert out["answer"].startswith("# Token security dossier")
    assert "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump" in seen["gathered"][0] and seen["gathered"][2] == ("solana",)


def test_with_no_token_named_and_nothing_in_focus_the_question_is_still_asked(monkeypatch):
    seen = {}
    _stub_downstream(monkeypatch, seen)
    out = asyncio.run(research_mod.research_node(_security_state("is it audited?")))
    assert "Which token should I check" in out["answer"] and "gathered" not in seen


# --- look it up before guessing or asking (user rule, 2026-09-18) --------------------
# The token resolver: a ticker Jupiter and Bitquery do not know is looked up on
# the web for its chain and resolved there; a ticker that lives on two chains
# takes the chain the web says the message means, before asking.

def _resolver_stubs(monkeypatch, *, mint=None, ds=(), canonical=None):
    monkeypatch.setattr(research_mod, "_jupiter_solana_mint", lambda ticker: mint)
    monkeypatch.setattr(research_mod, "token_candidates", lambda ticker: list(ds))

    async def on_chain(ticker, chain, strict=False):
        return canonical(ticker, chain) if canonical else None

    monkeypatch.setattr(research_mod, "_canonical_on_chain", on_chain)


def test_an_unknown_ticker_is_resolved_on_the_chain_the_web_names(monkeypatch):
    from app.routing import subject_probe
    _resolver_stubs(monkeypatch, canonical=lambda ticker, chain: {"chain": chain, "address": "0xbase" + ticker.lower(), "symbol": ticker} if chain == "base" else None)
    monkeypatch.setattr(subject_probe, "probe", lambda request: {"kind": "token", "symbol": "NEWCOIN", "chain": "base", "confidence": 0.9, "subject": "NEWCOIN"})
    out = asyncio.run(research_mod._resolve_named_token("top holders of NEWCOIN", {"token_discovery"}, ()))
    assert out.chain == "base" and "0xbasenewcoin on base" in out.request and not out.clarification


def test_an_unknown_ticker_the_web_cannot_place_is_left_to_the_router(monkeypatch):
    from app.routing import subject_probe
    _resolver_stubs(monkeypatch)
    monkeypatch.setattr(subject_probe, "probe", lambda request: {"kind": "other", "confidence": 0.9})
    out = asyncio.run(research_mod._resolve_named_token("top holders of NEWCOIN", {"token_discovery"}, ()))
    assert out.chain is None and out.request == "top holders of NEWCOIN" and not out.clarification


def test_a_cross_chain_tie_takes_the_chain_the_web_means_instead_of_asking(monkeypatch):
    from app.routing import subject_probe
    _resolver_stubs(monkeypatch, mint="So1anaMintOfDUP", ds=[
        {"chain": "base", "address": "0xdup", "symbol": "DUP", "liquidity_usd": 2_000_000.0, "volume_24h_usd": 500_000.0},
        {"chain": "solana", "address": "So1anaMintOfDUP", "symbol": "DUP", "liquidity_usd": 800_000.0, "volume_24h_usd": 400_000.0}],
        canonical=lambda ticker, chain: {"chain": "base", "address": "0xdup", "symbol": ticker, "liquidity_usd": 2_000_000.0} if chain == "base" else None)
    monkeypatch.setattr(subject_probe, "probe", lambda request: {"kind": "token", "symbol": "DUP", "chain": "base", "confidence": 0.95, "subject": "DUP"})
    out = asyncio.run(research_mod._resolve_named_token("top holders of DUP", {"token_discovery"}, ()))
    assert out.chain == "base" and not out.clarification, out.clarification
    monkeypatch.setattr(subject_probe, "probe", lambda request: None)
    asked = asyncio.run(research_mod._resolve_named_token("top holders of DUP", {"token_discovery"}, ()))
    assert asked.clarification and "several chains" in asked.clarification, "with nothing from the web the question is still asked"
