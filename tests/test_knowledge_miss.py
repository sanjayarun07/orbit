"""Transcript of 2026-09-18: "Who are the investors backing EigenLayer?" was
answered with "the provided passages do not include information about the
specific investors" and four EigenLayer forum links. The knowledge base had
passages about the project, not about the question. A knowledge miss is
never the answer the user reads: the web answers, and a missed card never
enters a composition."""
import asyncio
from types import SimpleNamespace

import pytest

from app.nodes import research


MISS_PROSE = ("The provided passages do not include information about the specific investors backing EigenLayer. They mainly cover the technical "
              "concepts, mechanisms, and potential use cases of EigenLayer such as restaking.\n\nTherefore, details on who the investors are backing "
              "EigenLayer are missing from the given knowledge base.\n\nSources\n[1] EigenCloud · Forum")
ANSWERED = ("EigenLayer's restaking lets ETH stakers opt in to secure additional services (AVSs) [1]; operators register and restakers delegate to them [2].\n\n"
            "## Sources\n[1] EigenLayer docs")


@pytest.mark.parametrize("text,missed", [
    (MISS_PROSE, True),
    ("NOT COVERED: the passages do not name EigenLayer's investors or funding rounds.", True),
    ("The knowledge base does not mention any funding rounds for EigenLayer.", True),
    (ANSWERED, False),
    ("EigenLayer raised $100M from a16z in February 2024 [3]; earlier rounds were led by Blockchain Capital [4].", False),
])
def test_a_knowledge_miss_is_recognised(text, missed):
    assert research.knowledge_missed(text) is missed


def test_a_miss_is_answered_from_the_web_with_the_gap_stated(monkeypatch):
    async def kb(request, passages, history, *, stream=True):
        return MISS_PROSE

    monkeypatch.setattr(research, "_knowledge_answer", kb)
    monkeypatch.setattr(research, "perplexity_available", lambda: True)
    asked = []
    monkeypatch.setattr(research, "perplexity_web_search", lambda q: asked.append(q) or "EigenLayer raised $100M in a Series B led by a16z (Feb 2024); earlier backers include Blockchain Capital, Coinbase Ventures and Polychain.\n\nSources:\n- [a16z](https://a16zcrypto.com)")
    out = asyncio.run(research._synthesize_knowledge("Who are the investors backing EigenLayer?", "[1] passages", "", stream=False))
    assert out.startswith("EigenLayer raised $100M") and "answered from the web" in out
    assert "provided passages do not include" not in out
    assert asked and asked[0].startswith("Who are the investors backing EigenLayer?") and "Crypto / Web3 context" in asked[0]


def test_an_answered_question_is_left_alone_and_a_dark_web_search_keeps_the_miss(monkeypatch):
    async def kb(request, passages, history, *, stream=True):
        return ANSWERED

    monkeypatch.setattr(research, "_knowledge_answer", kb)
    monkeypatch.setattr(research, "perplexity_web_search", lambda q: (_ for _ in ()).throw(AssertionError("no web search for an answered question")))
    assert asyncio.run(research._synthesize_knowledge("How does EigenLayer restaking work?", "[1] p", "", stream=False)) == ANSWERED

    async def miss(request, passages, history, *, stream=True):
        return MISS_PROSE

    monkeypatch.setattr(research, "_knowledge_answer", miss)
    monkeypatch.setattr(research, "perplexity_available", lambda: False)
    assert asyncio.run(research._synthesize_knowledge("Who are the investors backing EigenLayer?", "[1] p", "", stream=False)) == MISS_PROSE


def test_a_missed_knowledge_card_never_enters_a_composition(monkeypatch):
    """The planned multi-tool path: a knowledge card that says "not covered" is
    dropped, and with one card left the plan yields to the single route."""
    from app import provider_registry as _pr

    class Tool:
        def __init__(self, name):
            self.name = name

    class Router:
        def plan_across(self, request, capabilities, chains):
            return [Tool("knowledge_base_search"), Tool("rootdata_project_search")]

        def invoke(self, name, request, chains):
            return SimpleNamespace(tool=name, failures=[], attempted=[name], output="[1] restaking passages" if name == "knowledge_base_search" else "# RootData projects\n| EigenLayer |")

    monkeypatch.setattr(research, "get_provider_router", lambda: Router())

    async def kb(request, passages, history, *, stream=True):
        return "NOT COVERED: no investor information in the passages."

    monkeypatch.setattr(research, "_knowledge_answer", kb)
    out = asyncio.run(research._gather_planned("Who are the investors backing EigenLayer?", ("knowledge", "vcs"), (), {"history": ""}))
    assert out is None, "one usable card is not a composition"
