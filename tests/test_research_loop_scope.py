"""The loop's withhold gate, scoped (2026-09-27): a comparison with stated
conditions is the candidate lane (a name stands only on a page passage that
meets every condition, else the summary is withheld); an ordinary question
is the fact lane (written from the dated search cards and the passages read,
audited against both, withheld only when a claim still fails after one
repair); a headline tap falls back to the fixed sequence when no page could
be read, and carries the market pulse card on the loop lane."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import evidence_pipeline, market_overview, research_loop
from tests.test_contract_pipeline import FakeRouter
from tests.test_research_loop import loop  # noqa: F401  (the fixture: flags on, synthesis faked)

SEC_CARD = ("# From the web (dated, with sources)\n**Query**: q1\n\nThe SEC's September 17 order lets registered broker-dealers trade tokenized stocks under existing rules. [1]\n\n"
            "Sources:\n[1] [SEC release](https://www.sec.gov/newsroom/press-releases/2026-140) · 2026-09-17")
PULSE = "# Crypto market pulse\n**Retrieved** 2026-09-27 10:00 UTC\n\n## Core quotes\n| Asset | Price | 24h |\n|---|---:|---:|\n| BTC | $84,000.00 | -1.20% |"


def _fact_lane_lm(audits: list, unsupported_first: str = "none"):
    async def call(program, **kw):
        if "catalog" in kw and "request" in kw:
            return SimpleNamespace(subject="SEC tokenized-stock order", question="what the SEC's September 17 order authorizes",
                                   constraints="none", required_facts="the order's terms\nits date", capabilities="web_discovery",
                                   queries="web_discovery: SEC September 17 tokenized stock order text")
        if "calls_made" in kw:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in kw and "question" in kw and "summary" not in kw and "answer" not in kw:
            return SimpleNamespace(candidates="SEC release | https://www.sec.gov/newsroom/press-releases/2026-140")
        if "summary" in kw and "evidence" in kw:
            audits.append(kw["evidence"])
            return SimpleNamespace(unsupported_claims=unsupported_first if len(audits) == 1 else "none")
        if "answer" in kw and "evidence" in kw:
            raise AssertionError("the example gate is the candidate lane's")
        return SimpleNamespace(summary="summary")
    return call


def _run_fact(loop, lm, request="What does the SEC's September 17 tokenized-stock order actually authorize?", synth=None):
    from app.nodes import runtime
    loop.setattr(runtime, "_call_research_lm", lm)
    loop.setattr(runtime, "_call_research_loop_lm", lm)
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "", "text": "", "provenance": "unreadable", "fetched_at": "replay"})
    if synth is not None:
        loop.setattr(research_loop.composition, "synthesize", synth)
    router = FakeRouter({"perplexity_web_search": SEC_CARD})
    router.plan_across = lambda request, caps, chains, n: []
    loop.setattr(evidence_pipeline, "get_provider_router", lambda: router)
    return asyncio.run(evidence_pipeline.answer({}, request, ())), router


def test_an_ordinary_question_is_written_from_the_cards_when_no_page_could_be_read(loop):
    audits: list = []

    async def synth(prompt, cards, trajectory, advice=False, research=False):
        assert "Write from the dated search cards and the reviewed page passages" in prompt and SEC_CARD in cards
        return f"**Taken together**\n\nThe order lets registered broker-dealers trade tokenized stocks under existing rules [1].\n\n---\n\n{cards}"
    out, router = _run_fact(loop, _fact_lane_lm(audits), synth=synth)
    assert out["pipeline"] == "research_loop" and router.calls == ["perplexity_web_search"]
    trace = out["trajectory"]["research_loop"]
    assert trace["lane"] == "fact" and trace["constraints"] == "none"
    assert "I withheld" not in out["answer"] and "broker-dealers" in out["answer"] and "[SEC release]" in out["answer"]
    assert len(audits) == 1 and "SEC release" in audits[0]        # the claim audit ran, against the cards


def test_a_claim_the_cards_do_not_carry_is_repaired_once_then_withheld(loop):
    audits: list = []

    async def synth(prompt, cards, trajectory, advice=False, research=False):
        return f"**Taken together**\n\nThe order also lets retail investors trade tokenized stocks on DEXs [1].\n\n---\n\n{cards}"
    out, _ = _run_fact(loop, _fact_lane_lm(audits, unsupported_first="retail investors may trade tokenized stocks on DEXs"), synth=synth)
    assert out["trajectory"]["research_loop"]["lane"] == "fact"
    assert len(audits) == 2 and out["gate"]["ok"] is False
    assert "I withheld" not in out["answer"] and "retail investors" in out["answer"]       # the repair passed the second audit


def test_a_claim_that_still_fails_after_the_repair_is_withheld(loop):
    audits: list = []

    async def lm(program, **kw):
        if "summary" in kw and "evidence" in kw:
            audits.append(1)
            return SimpleNamespace(unsupported_claims="retail investors may trade tokenized stocks on DEXs")
        return await _fact_lane_lm(audits)(program, **kw)

    async def synth(prompt, cards, trajectory, advice=False, research=False):
        return f"**Taken together**\n\nThe order also lets retail investors trade tokenized stocks on DEXs [1].\n\n---\n\n{cards}"
    out, _ = _run_fact(loop, lm, synth=synth)
    assert out["answer"].startswith("**I withheld the written summary: the summary still asserted claims its reviewed passages do not establish")
    assert out["gate"]["ok"] is False


def test_a_comparison_with_conditions_keeps_the_candidate_lane(loop):
    from tests.test_research_loop import _run, _script
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "", "text": "", "provenance": "unreadable", "fetched_at": "replay"})
    out, _ = _run(loop, _script("web_discovery: q", []), {"perplexity_web_search": "# From the web\nSynthetix locks SNX. [1]\n\nSources:\n[1] [Synthetix](https://docs.synthetix.io) · 2026-09-09"})
    assert out["trajectory"]["research_loop"]["lane"] == "candidate" and "I withheld the written summary" in out["answer"]


def _tap_lm(event_passage: str | None):
    async def call(program, **kw):
        if "request" in kw and "catalog" in kw:
            return SimpleNamespace(subject="CME futures", question="what changed", constraints="none", required_facts="terms", queries="web_discovery: one")
        if "headline" in kw and "sources" in kw:
            return SimpleNamespace(event_url="https://www.cmegroup.com/media-room/press-releases/2026/9/26/cme.html", market_url="none")
        if "facet" in kw and "page" in kw:
            return SimpleNamespace(passages=event_passage or "")
        if "summary" in kw and "evidence" in kw:
            return SimpleNamespace(unsupported_claims="none")
        raise AssertionError(kw)
    return call


TAP = "What does this mean for the market: CME plans Bitcoin Cash and Uniswap futures"
EVENT_URL = "https://www.cmegroup.com/media-room/press-releases/2026/9/26/cme.html"
EVENT_CARD = (f"# From the web (dated, with sources)\n**Query**: q\n\nCME Group announced Bitcoin Cash and Uniswap futures on September 26. [1]\n\n"
              f"Sources:\n[1] [CME release]({EVENT_URL}) · 2026-09-26")


def test_a_tap_whose_event_page_yields_no_passage_falls_back_to_the_fixed_sequence(loop):
    from app.nodes import runtime
    loop.setattr(runtime, "_call_research_loop_lm", _tap_lm(None))
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "", "text": "", "provenance": "unreadable", "fetched_at": "replay"})
    loop.setattr(market_overview, "market_pulse_card", lambda: PULSE)
    router = FakeRouter({"perplexity_web_search": EVENT_CARD})
    router.plan_across = lambda request, caps, chains, n: []
    loop.setattr(evidence_pipeline, "get_provider_router", lambda: router)
    out = asyncio.run(evidence_pipeline.answer({}, TAP, ()))
    assert out["pipeline"] == "contract"                    # the fixed sequence answered, not a refusal
    assert "could not verify the original event" not in out["answer"] and "# Crypto market pulse" in out["answer"]


def test_a_tap_the_loop_verifies_carries_the_market_pulse_card(loop):
    from app.nodes import runtime
    passage = "CME Group today announced plans to launch Bitcoin Cash and Uniswap futures on October 13."
    loop.setattr(runtime, "_call_research_loop_lm", _tap_lm(passage))
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "", "text": passage, "provenance": "page", "fetched_at": "replay"})
    loop.setattr(market_overview, "market_pulse_card", lambda: PULSE)
    prompts: list = []

    async def synth(prompt, cards, trajectory, advice=False, research=False):
        prompts.append(prompt)
        return f"CME announced the futures on September 26.\n\n{cards}"
    loop.setattr(research_loop.composition, "synthesize", synth)
    router = FakeRouter({"perplexity_web_search": EVENT_CARD})
    router.plan_across = lambda request, caps, chains, n: []
    loop.setattr(evidence_pipeline, "get_provider_router", lambda: router)
    out = asyncio.run(evidence_pipeline.answer({}, TAP, ()))
    assert out["pipeline"] == "research_loop" and out["gate"]["ok"] is True
    assert passage in out["answer"] and "# Crypto market pulse" in out["answer"]
    assert "This is a Home headline tap" in prompts[0] and out["trajectory"]["research_loop"]["market_state"] == ["market_pulse"]


def test_a_failed_candidate_listing_leaves_the_cards_as_the_fact_lanes_evidence(loop):
    audits: list = []

    async def lm(program, **kw):
        if "evidence" in kw and "question" in kw and "summary" not in kw and "answer" not in kw:
            raise RuntimeError("model down")           # the candidate listing
        return await _fact_lane_lm(audits)(program, **kw)

    async def synth(prompt, cards, trajectory, advice=False, research=False):
        return f"**Taken together**\n\nThe order lets registered broker-dealers trade tokenized stocks [1].\n\n---\n\n{cards}"
    out, _ = _run_fact(loop, lm, synth=synth)
    assert out["trajectory"]["research_loop"]["lane"] == "fact" and "I withheld" not in out["answer"] and "broker-dealers" in out["answer"]
    assert len(audits) == 1
