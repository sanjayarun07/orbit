"""The conversation's research objective (app/research_objective.py): kept
across turns, attached to the resolved request, and read by discovery
searches and the synthesis. The four-turn comparison with Minara (2026-09-24)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import evidence_pipeline, experience, research_objective
from app.contracts import plan_by_rules


def test_applies_to_research_turns_and_while_an_objective_stands():
    assert research_objective.applies("research", {"kind": "open_research"}, None)
    assert research_objective.applies("general", {"kind": "recent_events"}, None)
    assert not research_objective.applies("research", {"kind": "market_ranking"}, None)
    assert research_objective.applies("research", {"kind": "market_ranking"}, "compare dual-token mechanisms")
    assert not research_objective.applies("portfolio", None, "compare dual-token mechanisms")


def test_update_keeps_the_previous_objective_when_the_model_is_unavailable(monkeypatch):
    from app.nodes import runtime

    async def down(program, **kw):
        raise RuntimeError("model down")

    monkeypatch.setattr(runtime, "_call_research_lm", down)
    out = asyncio.run(research_objective.update("compare dual-token mechanisms to Venice", "how it works in akash network", "Akash ...", intent="general", contract={"kind": "open_research"}))
    assert out == "compare dual-token mechanisms to Venice"


def test_update_takes_the_models_sentence_and_ends_on_none(monkeypatch):
    from app.nodes import runtime
    calls = []

    async def model(program, **kw):
        calls.append(kw)
        return SimpleNamespace(objective='"Compare dual-token mechanisms to Venice: own-token collateral, tradable second token"')

    monkeypatch.setattr(runtime, "_call_research_lm", model)
    out = asyncio.run(research_objective.update(None, "Research on projects which had 2 tokens like Venice VVV and DIEM", "Venice ...", intent="research", contract={"kind": "open_research"}))
    assert out.startswith("Compare dual-token mechanisms to Venice")
    assert calls[0]["previous_objective"] == "none"

    async def none(program, **kw):
        return SimpleNamespace(objective="none")

    monkeypatch.setattr(runtime, "_call_research_lm", none)
    assert asyncio.run(research_objective.update(out, "price of SOL", "SOL is ...", intent="research", contract={"kind": "other"})) is None


def test_update_is_off_by_setting(monkeypatch):
    monkeypatch.setattr(research_objective.settings, "research_objective_enabled", False)
    assert asyncio.run(research_objective.update("x", "y", "z", intent="research", contract={"kind": "open_research"})) == "x"


def test_attach_rides_with_the_resolved_request():
    ctx = {"research_objective": "compare dual-token mechanisms to Venice: own-token collateral, tradable second token"}
    out = research_objective.attach("how it works in akash network", ctx)
    assert out.startswith("how it works in akash network\nResearch objective of this conversation: compare dual-token")
    assert research_objective.attach(out, ctx) == out                       # idempotent
    assert research_objective.attach("price of SOL", {}) == "price of SOL"


def test_session_context_keeps_refines_and_ends_the_objective():
    ctx = experience.advance_session_context({}, "Research on projects with two tokens", None, "research", [], [], None, research_objective="compare dual-token mechanisms")
    assert ctx["research_objective"] == "compare dual-token mechanisms"
    ctx = experience.advance_session_context(ctx, "show my tasks", None, "general", [], [], None)          # a turn that does not touch it keeps it
    assert ctx["research_objective"] == "compare dual-token mechanisms"
    ctx = experience.advance_session_context(ctx, "price of SOL", None, "research", [], [], None, research_objective=None)
    assert "research_objective" not in ctx


def test_open_research_is_discovery_only_unless_a_contract_is_named(monkeypatch):
    from tests.test_contract_pipeline import FakeRouter
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    monkeypatch.setattr(evidence_pipeline.settings, "discovery_first_research", True)
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: False)

    async def synth(request, cards, trajectory, advice=False, research=False):
        return f"**Taken together**\n\nsummary\n\n---\n\n{cards}"

    monkeypatch.setattr(evidence_pipeline.composition, "synthesize", synth)
    web = "# From the web (dated, with sources)\n**Query**: q\n\nVenice mints DIEM by burning VVV. [1]\n\nSources:\n[1] [Venice blog](https://venice.ai/blog) · 2026-09-09"
    router = FakeRouter({"perplexity_web_search": web, "dexscreener_token_pairs": "# DEX pairs\n| DIEM/WETH |", "birdeye_token_overview": "# DIEM\nprice"})
    router.plan_across = lambda request, caps, chains, n: [SimpleNamespace(name="dexscreener_token_pairs"), SimpleNamespace(name="birdeye_token_overview")]
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "Research on projects which had 2 tokens like Venice project VVV and DIEM to understand how the second token is minted", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert out is not None and "perplexity_web_search" in router.calls
    assert not {"dexscreener_token_pairs", "birdeye_token_overview"} & set(router.calls)


def test_unsourced_examples_are_the_names_the_cards_do_not_carry():
    cards = "Sources:\n[1] [Venice blog: DIEM](https://venice.ai) · 2026-09-09\n[2] [Akash AEP-76](https://akash.network) · 2026-03-23"
    summary = "Venice mints DIEM by burning VVV [1]. Akash burns AKT to mint ACT [2]. Lido and Rocket Pool issue receipt tokens. However, Frax is different."
    assert evidence_pipeline.unsourced_examples(summary, cards) == ["Lido", "Rocket Pool", "Frax"]


def test_reasoning_models_get_their_kwargs(monkeypatch):
    from app.nodes import runtime
    monkeypatch.setattr(runtime.settings, "research_reasoning_effort", "high")
    assert runtime._reasoning_kwargs("openai/gpt-5.6-sol") == {"temperature": 1.0, "max_tokens": 16000, "reasoning_effort": "high"}
    assert runtime._reasoning_kwargs("openai/gpt-4.1-mini") == {}


def test_the_splitter_never_splits_the_notes():
    from app import composition
    request = "how it works in akash network\nResolved from conversation context: \"it\" is the previous question. Applied here.\nResearch objective of this conversation: compare dual-token mechanisms. Answer the current question in service of that objective; when the question is ambiguous, the objective settles what it means."
    assert composition.split_asks(request) == [request]
    two = "price of BONK. Also holders of WIF\nResearch objective of this conversation: memes. Answer the current question in service of that objective."
    parts = composition.split_asks(two)
    assert len(parts) == 2 and all("Research objective of this conversation" in p for p in parts)
    assert composition.split_notes(two)[0] == "price of BONK. Also holders of WIF"


def test_the_web_query_is_the_question_with_its_context():
    q = evidence_pipeline.research_query("In Akash Network: apart from staking how else can we tie dual token to the main token?\nResolved from conversation context: the user wrote \"how it works in akash network\".\nResearch objective of this conversation: compare dual-token mechanisms to Venice: own-token collateral, tradable second token. Answer the current question in service of that objective; when the question is ambiguous, the objective settles what it means.")
    assert q.startswith("In Akash Network: apart from staking how else can we tie dual token to the main token? (Context: this continues research on compare dual-token mechanisms to Venice")
    assert "Resolved from" not in q and "Answer the current question" not in q
    assert "crypto-first markets assistant" in evidence_pipeline.research_query("What is OPEN?")


def test_a_clarification_answer_leaves_the_objective_alone(monkeypatch):
    from app.nodes import runtime

    async def never(program, **kw):
        raise AssertionError("no model call on a clarification")

    monkeypatch.setattr(runtime, "_call_research_lm", never)
    out = asyncio.run(research_objective.update("compare dual-token mechanisms", "how it works in akash network",
                                                "Which token's unlock? Name it (a $ticker, the project name or its contract address) and I'll pull its vesting schedule.",
                                                intent="research", contract={"kind": "open_research"}))
    assert out == "compare dual-token mechanisms"


def test_an_unlock_word_in_the_notes_is_not_an_unlock_ask():
    from app import composition, token_unlocks
    request = "how it works in akash network\nResearch objective of this conversation: mechanisms including lock-to-mint and burn-to-unlock. Answer the current question in service of that objective."
    assert token_unlocks.UNLOCK_ASK.search(request)
    assert not token_unlocks.UNLOCK_ASK.search(composition.split_notes(request)[0])


def test_the_open_research_note_is_a_brief_without_internals():
    from app import fact_gate
    contract = plan_by_rules("how it works in akash network")
    assert contract.kind == "open_research"
    gate = fact_gate.check(contract, [], scope_satisfied=True)
    note = evidence_pipeline._contract_note(contract, gate, [], None)
    assert "Brief for the answer" in note and "organise by mechanism" in note
    for internal in ("Question contract", "window asked", "Accepted facts", "metric"):
        assert internal not in note


def test_a_multi_sentence_research_ask_is_one_ask():
    from app import composition, contracts
    request = ("Research on projects which had 2 tokens like Venice project VVV and DIEM token. and what mechinism should be used to mint "
               "the 2nd token. what are all the different speculative ideas in crypto to achieve this")
    clauses, _ = composition.plan_asks(request)
    assert len(clauses) >= 2                       # the splitter alone sees three sentences
    assert contracts.is_open_research(request)     # ... but the research node keeps it whole
    from app.nodes import research
    assert not research._DATA_ASK.search(research._ask(request))


def test_open_research_runs_before_a_ticker_is_resolved(monkeypatch):
    from app.nodes import research
    from app import evidence_pipeline as ep
    seen = {}

    async def fake_answer(state, request, chains, **kw):
        seen["request"] = request
        return {"answer": "web research", "trajectory": None}

    async def never(*a, **k):
        raise AssertionError("the ticker must not be resolved before open research")

    monkeypatch.setattr(ep, "answer", fake_answer)
    monkeypatch.setattr(research, "_resolve_named_token", never)
    monkeypatch.setattr(research.settings, "contract_pipeline_enabled", True, raising=False)
    monkeypatch.setattr(research, "_web_context_part", lambda state: None)
    state = {"request": "Research on projects which had 2 tokens like Venice project VVV and DIEM token. and what mechinism should be used to mint the 2nd token",
             "contextual_request": None, "capabilities": ["web_research"], "chains": [], "session_context": {}, "history": "", "routing_decision": {}}
    out = asyncio.run(research._research_node(state, {}))
    assert out.get("answer") == "web research" and "DIEM token" in seen["request"]


def test_every_line_the_user_wrote_reaches_the_search():
    q = evidence_pipeline.research_query("which other projects follow lock collateral -> mint a tradable second token\nOnly include transferable tokens\nExclude wrapped assets\nResearch objective of this conversation: compare dual-token mechanisms. Answer the current question in service of that objective; when the question is ambiguous, the objective settles what it means.")
    assert "Only include transferable tokens" in q and "Exclude wrapped assets" in q and "Answer the current question" not in q


def test_a_first_word_match_does_not_source_a_name():
    cards = "Sources:\n[1] [Nova market index](https://x) · 2026-09-09"
    assert evidence_pipeline.unsourced_examples("Nova Labs mints transferable ASTRA [1].", cards) == ["Nova Labs"]
    assert evidence_pipeline.unsourced_examples("Nova Labs mints transferable ASTRA [1].", cards + "\n[2] [Nova Labs docs](https://y)") == []


def test_a_which_other_projects_ask_runs_a_second_discovery_search(monkeypatch):
    from tests.test_contract_pipeline import FakeRouter
    from types import SimpleNamespace
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    monkeypatch.setattr(evidence_pipeline.settings, "discovery_first_research", True)
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: False)

    async def synth(request, cards, trajectory, advice=False, research=False):
        return f"**Taken together**\n\nsummary\n\n---\n\n{cards}"

    monkeypatch.setattr(evidence_pipeline.composition, "synthesize", synth)
    seen = []
    web = "# From the web (dated, with sources)\n**Query**: q\n\nSynthetix locks SNX to mint sUSD. [1]\n\nSources:\n[1] [Synthetix docs](https://docs.synthetix.io) · 2026-09-09"
    router = FakeRouter({"perplexity_web_search": web})
    original = router.invoke

    def invoke(name, request, chains=()):
        seen.append((name, request))
        return original(name, request, chains)

    router.invoke = invoke
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    searches = [r for n, r in seen if n == "perplexity_web_search"]
    assert len(searches) == 2 and "first-party source" in searches[1]
    assert out is not None and "Synthetix" in (out.get("answer") or "")
