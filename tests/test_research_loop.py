"""The bounded evidence loop for open research (app/research_loop.py), with
the research tier faked: plan, gap review and example-support answers come
from a script, the tools from a replay router. The brief:
docs/engineering/claude-code-evidence-loop.md."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import evidence_pipeline, research_loop
from app.contracts import plan_by_rules
from tests.test_contract_pipeline import FakeRouter

WEB1 = ("# From the web (dated, with sources)\n**Query**: q1\n\nVenice locks sVVV to mint DIEM, a tradable ERC-20. [1]\n\n"
        "Sources:\n[1] [Venice docs](https://venice.ai/docs) · 2026-09-09")
WEB2 = ("# From the web (dated, with sources)\n**Query**: q2\n\nSynthetix locks SNX to mint sUSD, which trades freely. [1] Aave mints aTokens as deposit receipts. [2]\n\n"
        "Sources:\n[1] [Synthetix docs](https://docs.synthetix.io) · 2026-09-09\n[2] [Aave docs](https://docs.aave.com) · 2026-09-01")


def test_attributed_question_uses_bounded_author_source_path(monkeypatch):
    from app import perplexity_tools
    question = "What is the nature of GROK's tokenized SpaceX exposure mentioned by @stitchdegen?"
    source_url = "https://site.twstalker.com/stitchdegen"
    monkeypatch.setattr(perplexity_tools, "perplexity_search_with_sources", lambda query: {
        "text": "A search lead", "sources": [{"title": "Stitch @stitchdegen", "url": source_url}]})
    monkeypatch.setattr(research_loop, "read_page", lambda url: {"url": url, "provenance": "page",
        "text": "Stitch @stitchdegen says GROK is paired with SPCXx, a tokenized SpaceX asset. "
                "The paired asset creates a narrative, while a transfer tax may fund SPCXx rewards."})
    async def synthesize(request, cards, trajectory, research=False):
        assert "paired with SPCXx" in cards and research
        return "**Taken together**\n\nStitch describes a trading pair and possible rewards, not direct stock ownership.\n\n---\n\n" + cards
    monkeypatch.setattr(research_loop.composition, "synthesize", synthesize)
    async def wrong(*args, **kwargs):
        raise AssertionError("Attributed claim should not enter the multi-round candidate loop")
    monkeypatch.setattr(research_loop, "_run", wrong)
    result = asyncio.run(research_loop.run({}, question, plan_by_rules(question), None, ()))
    assert result["pipeline"] == "attributed_research"
    assert "not direct stock ownership" in result["answer"]
    assert result["trajectory"]["research_progress"]["sources"][0]["url"] == source_url


def test_attributed_question_abstains_if_author_page_does_not_support_topic(monkeypatch):
    from app import perplexity_tools
    question = "What is the nature of GROK's tokenized SpaceX exposure mentioned by @stitchdegen?"
    monkeypatch.setattr(perplexity_tools, "perplexity_search_with_sources", lambda query: {
        "text": "Unverified lead", "sources": [{"title": "Stitch @stitchdegen", "url": "https://example.org/stitchdegen"}]})
    monkeypatch.setattr(research_loop, "read_page", lambda url: {"url": url, "provenance": "page", "text": "Stitch writes about SOL."})
    result = asyncio.run(research_loop.run({}, question, plan_by_rules(question), None, ()))
    assert "could not verify" in result["answer"]
    assert "ticker" in result["answer"]


def test_no_candidate_names_still_inspects_only_cited_pages():
    card = ("# Web\nClaim from the first page [1].\n\nSources:\n"
            "[1] [Primary report](https://example.com/report) · 2026-09-25\n"
            "[2] [Uncited home page](https://example.com/) · 2026-09-25")
    assert research_loop._cited_source_leads(card) == [("Primary report", "https://example.com/report")]
    assert "Claim from the first page" in research_loop._cited_source_context(card)["https://example.com/report"]
    assert "https://example.com/" not in research_loop._cited_source_context(card)


def test_headline_research_opens_with_independent_event_and_market_searches():
    request = "What does this mean for the market: Fed proposes payment stablecoin rules"
    contract = plan_by_rules(request)
    assert contract.kind == "open_research" and contract.subject.name == "Fed proposes payment stablecoin rules"
    plan = SimpleNamespace(question=request, queries="web_discovery: Fed stablecoin news")
    calls = research_loop._opening_calls(plan, contract, request)
    assert len(calls) == 2 and all(capability == "web_discovery" for capability, _ in calls)
    assert "original announcement" in calls[0][1] and "event date" in calls[0][1]
    assert "market reaction" in calls[1][1] and "alternative drivers" in calls[1][1]
    assert "Fed proposes payment stablecoin rules" in calls[0][1] and "Fed proposes payment stablecoin rules" in calls[1][1]


def test_headline_uses_request_when_model_contract_lacks_event_fields():
    from app.contracts import QuestionContract, Subject
    request = "What does this mean for the market: Bond yields hit highest level since 2007, pressuring stocks"
    contract = QuestionContract(kind="open_research", subject=Subject(kind="topic"))
    assert research_loop._news_headline(contract, request) == "Bond yields hit highest level since 2007, pressuring stocks"


def test_headline_contract_does_not_delegate_to_model_planner(monkeypatch):
    from app import contracts
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: True)

    async def unexpected(*args, **kwargs):
        raise AssertionError("Home headline should not reach the model planner")

    monkeypatch.setattr(runtime, "_call_planner_lm", unexpected)
    request = "What does this mean for the market: Bond yields hit highest level since 2007, pressuring stocks"
    contract = asyncio.run(contracts.plan(request))
    assert contract.kind == "open_research" and contract.metric == "events"
    assert contract.subject.name == "Bond yields hit highest level since 2007, pressuring stocks"


def test_headline_leads_exclude_old_market_response_and_diversify_fallbacks():
    from datetime import date, timedelta
    recent = (date.today() - timedelta(days=2)).isoformat()
    old = (date.today() - timedelta(days=800)).isoformat()
    assert research_loop._recent_news_lead({"date": recent})
    assert not research_loop._recent_news_lead({"date": old})
    leads = [
        {"axis": 0, "url": "https://reuters.com/one", "title": "Market event", "date": recent},
        {"axis": 0, "url": "https://reuters.com/two", "title": "Market event", "date": recent},
        {"axis": 0, "url": "https://finance.yahoo.com/three", "title": "Market event", "date": recent},
    ]
    assert research_loop._news_fallbacks(leads, 0, {"https://reuters.com/one"})[0] == "https://finance.yahoo.com/three"


def test_concept_research_keeps_its_planned_discovery_calls():
    request = "Which protocols mint tradable tokens against native collateral?"
    contract = plan_by_rules(request)
    plan = SimpleNamespace(question=request, queries="web_discovery: native collateral mint designs")
    assert research_loop._opening_calls(plan, contract, request) == [("web_discovery", "native collateral mint designs")]
    named = "Who are the investors backing EigenLayer?"
    assert research_loop._opening_calls(plan, plan_by_rules(named), named) == [("web_discovery", "native collateral mint designs")]


def test_typed_discovery_expands_distinct_facets_with_bounded_options(monkeypatch):
    monkeypatch.setattr(research_loop.settings, "research_query_expansion_enabled", True)
    request = "Which projects lock their own token to mint a tradable second token?"
    plan = SimpleNamespace(queries="web_discovery: legacy fallback", sub_queries=json.dumps([
        {"facet": "find designs", "capability": "web_discovery", "query": "protocols lock native token mint transferable asset", "topic": "general", "domains": []},
        {"facet": "check Venice", "capability": "web_discovery", "query": "Venice sVVV collateral DIEM original documentation", "topic": "general", "domains": ["docs.venice.ai", "http://unsafe"]},
        {"facet": "market news", "capability": "web_discovery", "query": "latest trading status of minted token", "topic": "news", "days": 500},
        {"facet": "duplicate", "capability": "web_discovery", "query": "protocols lock native token mint transferable asset"},
        {"facet": "over limit", "capability": "web_discovery", "query": "fifth distinct search"},
    ]))
    calls = research_loop._expanded_calls(plan, plan_by_rules(request), request)
    assert len(calls) == 3
    assert calls[0].facet == "find designs" and calls[0].days is None
    assert calls[1].domains == ("docs.venice.ai",)
    assert calls[2].topic == "news" and calls[2].days == 30
    assert research_loop._expanded_calls(SimpleNamespace(queries="web_discovery: legacy fallback", sub_queries="bad"),
                                         plan_by_rules(request), request)[0].query == "legacy fallback"
    monkeypatch.setattr(research_loop.settings, "research_query_expansion_enabled", False)
    assert [call.query for call in research_loop._expanded_calls(plan, plan_by_rules(request), request)] == ["legacy fallback"]


def test_expansion_uses_a_separate_planner_signature():
    assert "sub_queries" not in research_loop.EvidencePlan.output_fields
    assert "sub_queries" in research_loop.ExpandedEvidencePlan.output_fields
    assert research_loop._plan_program.signature is research_loop.EvidencePlan
    assert research_loop._expanded_plan_program.signature is research_loop.ExpandedEvidencePlan


def test_expanded_discovery_merges_same_url_as_one_page_lead():
    url = "https://docs.example.com/mechanism"
    cards = (f"# First\nMechanism [1].\n\nSources:\n[1] [Docs]({url})\n\n---\n\n"
             f"# Second\nTransferability [1].\n\nSources:\n[1] [Docs]({url})")
    assert research_loop._cited_source_leads(cards) == [("Docs", url)]


def test_headline_loop_converges_on_two_directly_read_source_pages(loop):
    from app.nodes import runtime
    request = "What does this mean for the market: Fed proposes payment stablecoin rules"
    event_url = "https://www.federalreserve.gov/news/stablecoin-proposal"
    pdf_url = "https://www.federalreserve.gov/news/stablecoin-proposal.pdf"
    market_url = "https://example.com/market-report"
    event_quote = "The Board requested comment on proposed stablecoin reserve standards on September 24, 2026."
    market_quote = "Bitcoin traded at $84,000 on September 25, 2026 as Treasury yields rose."
    audits = []
    event_card = (f"# Web\nThe Board proposed reserve standards [1][2].\n\nSources:\n"
                  f"[1] [Official proposal PDF]({pdf_url}) · 2026-09-24\n"
                  f"[2] [Federal Reserve release]({event_url}) · 2026-09-24")
    market_card = f"# Web\nBitcoin and yields moved [1].\n\nSources:\n[1] [Market report]({market_url}) · 2026-09-25"

    async def fake(program, **kw):
        if "request" in kw and "catalog" in kw:
            return SimpleNamespace(subject="Fed stablecoin proposal", question="What changed and how did markets react?",
                                   constraints="none", required_facts="proposal terms\nmarket response",
                                   queries="web_discovery: one topical summary")
        if "headline" in kw and "sources" in kw:
            return SimpleNamespace(event_url=pdf_url, market_url=market_url)
        if "facet" in kw and "page" in kw:
            return SimpleNamespace(passages=event_quote if kw["facet"] == "event" else market_quote)
        if "summary" in kw and "evidence" in kw:
            audits.append(kw["summary"])
            return SimpleNamespace(unsupported_claims="The Fed proposal caused the Bitcoin move" if len(audits) == 1 else "none")
        raise AssertionError(kw)

    class Rotating(FakeRouter):
        def invoke(self, name, query, chains=()):
            self.calls.append(name)
            return SimpleNamespace(output=event_card if len(self.calls) == 1 else market_card, tool=name, provider="fake")

    loop.setattr(runtime, "_call_research_loop_lm", fake)
    async def synth(prompt, cards, trajectory, advice=False, research=False):
        if "Remove or qualify" in prompt:
            return f"The Fed proposed rules; the market response has a separate source.\n\n{cards}"
        return f"The Fed proposal caused the Bitcoin move.\n\n{cards}"
    loop.setattr(research_loop.composition, "synthesize", synth)
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "", "text": "" if url == pdf_url else event_quote if url == event_url else market_quote,
                                                          "provenance": "unreadable" if url == pdf_url else "page", "fetched_at": "replay"})
    router = Rotating({})
    loop.setattr(evidence_pipeline, "get_provider_router", lambda: router)
    out = asyncio.run(evidence_pipeline.answer({}, request, ()))
    assert out["pipeline"] == "research_loop"
    assert router.calls == ["perplexity_web_search", "perplexity_web_search"]
    trace = out["trajectory"]["research_loop"]
    assert "original announcement" in trace["calls"][0] and "market reaction" in trace["calls"][1]
    assert trace["coverage"]["event"] and trace["coverage"]["market"]
    assert trace["coverage"]["selected"]["event"] == event_url
    assert f"page_read: {pdf_url}" in trace["calls"] and f"page_read: {event_url}" in trace["calls"]
    assert event_quote in out["answer"] and market_quote in out["answer"]
    assert len(audits) == 2 and "caused the Bitcoin move" not in out["answer"]
    assert out["gate"]["ok"] is True


def test_home_headline_reads_its_own_source_before_search_replacements(loop):
    from app import home_highlights
    from app.nodes import runtime
    request = "What does this mean for the market: Fed proposes tougher stablecoin rules as crypto stays volatile"
    card_url = "https://example.com/original-fed-story"
    other_url = "https://example.org/related-fed-story"
    market_url = "https://market.example.net/market-reaction"
    card = {"prompt": request, "source_url": card_url, "title": "Fed proposes tougher stablecoin rules", "date": ""}
    loop.setattr(home_highlights, "cached_card_for_prompt", lambda prompt: card if prompt == request else None)

    async def fake(program, **kw):
        if "headline" in kw and "sources" in kw:
            return SimpleNamespace(event_url=other_url, market_url=market_url)
        if "facet" in kw and "page" in kw:
            return SimpleNamespace(passages="The Fed proposed stablecoin rules." if kw["facet"] == "event" else "Crypto prices fluctuated on Thursday.")
        if "summary" in kw and "evidence" in kw:
            return SimpleNamespace(unsupported_claims="none")
        raise AssertionError(kw)

    class TwoSearches(FakeRouter):
        def invoke(self, name, query, chains=()):
            self.calls.append(name)
            url = other_url if len(self.calls) == 1 else market_url
            return SimpleNamespace(output=f"# Web\nReport [1].\n\nSources:\n[1] [Report]({url}) · 2026-09-25", tool=name, provider="fake")

    loop.setattr(runtime, "_call_research_loop_lm", fake)
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "text": "The Fed proposed stablecoin rules." if url == card_url else "Crypto prices fluctuated on Thursday.",
                                                         "provenance": "page", "fetched_at": "replay"})
    async def synth(*args, **kwargs):
        return "The Fed proposed stablecoin rules; the observed market move was separate."
    loop.setattr(research_loop.composition, "synthesize", synth)
    loop.setattr(research_loop, "_unsupported_claims", lambda *args, **kwargs: asyncio.sleep(0, result=[]))
    loop.setattr(evidence_pipeline, "get_provider_router", lambda: TwoSearches({}))
    out = asyncio.run(evidence_pipeline.answer({}, request, ()))
    assert out["pipeline"] == "research_loop"
    assert out["trajectory"]["research_loop"]["coverage"]["selected"]["event"] == card_url
    assert f"page_read: {card_url}" in out["trajectory"]["research_loop"]["calls"]


def test_candidate_prefers_its_own_cited_page_over_an_aggregator():
    cited = [("Exchange article", "https://www.binance.com/en/academy/synthetix"),
             ("Protocol docs", "https://docs.synthetix.io/staking")]
    assert research_loop._candidate_first_party_url("Synthetix — SNX collateral", cited) == "https://docs.synthetix.io/staking"
    assert research_loop._candidate_first_party_url("Unrelated", cited) is None


def test_first_party_followup_prefers_page_about_missing_fact(monkeypatch):
    from app import perplexity_tools
    monkeypatch.setattr(perplexity_tools, "perplexity_available", lambda: True)
    monkeypatch.setattr(perplexity_tools, "perplexity_search_with_sources", lambda query: {
        "text": "Synthetix has documentation for both staking and transfers.",
        "sources": [
            {"url": "https://docs.synthetix.io/staking", "title": "SNX Staking"},
            {"url": "https://docs.aave.com/transfers", "title": "Other protocol transfers"},
            {"url": "https://blog.synthetix.io/how-a-synth-gets-transferred/", "title": "How a Synth Gets Transferred"},
        ],
    })
    assert research_loop.first_party_url("Synthetix", "sUSD transferable to another address") == (
        "https://blog.synthetix.io/how-a-synth-gets-transferred/")


def test_single_subject_claim_variants_share_one_candidate_and_page_budget(monkeypatch):
    cards = ("# Web\nSynthetix staking [1] and Synthetix synth trading [2].\n\nSources:\n"
             "[1] [Stake](https://docs.synthetix.io/staking) · 2026-09-25\n"
             "[2] [Trade](https://docs.synthetix.io/trading) · 2026-09-25")
    reads = []
    monkeypatch.setattr(research_loop.settings, "research_loop_inspect_pages", 4)
    monkeypatch.setattr(research_loop, "read_page", lambda url: (reads.append(url) or {
        "url": url, "title": "docs", "text": "Stakers lock SNX as collateral to mint sUSD, which trades on open markets.",
        "provenance": "page", "fetched_at": "replay"}))

    async def fake(program, **kw):
        if "evidence" in kw:
            return SimpleNamespace(candidates=("Historical Synthetix | https://docs.synthetix.io/staking\n"
                                               "Staker mechanism | https://docs.synthetix.io/trading"))
        if "page" in kw:
            return SimpleNamespace(verdict="qualifies", conditions_met="all", conditions_failed="none",
                                   quote="lock SNX as collateral to mint sUSD")
        raise AssertionError(kw)

    verdicts, _ = asyncio.run(research_loop._inspect_candidates(
        "How did Synthetix work?", "native collateral; separate token", cards,
        SimpleNamespace(_call_research_loop_lm=fake), None))
    assert len(verdicts) == 1 and verdicts[0]["name"] == "Synthetix"
    assert reads == ["https://docs.synthetix.io/staking"]


def test_named_claim_variants_group_after_first_party_resolution(monkeypatch):
    cards = "# Web\nSynthetix is discussed without numbered citations."
    reads = []
    monkeypatch.setattr(research_loop.settings, "research_loop_inspect_pages", 4)
    monkeypatch.setattr(research_loop, "first_party_url", lambda name, conditions: "https://docs.synthetix.io/staking")
    monkeypatch.setattr(research_loop, "read_page", lambda url: (reads.append(url) or {
        "url": url, "title": "docs", "text": "Stakers lock SNX as collateral to mint sUSD, which trades on open markets.",
        "provenance": "page", "fetched_at": "replay"}))

    async def fake(program, **kw):
        if "evidence" in kw:
            return SimpleNamespace(candidates="Historical Synthetix | none\nStaker mechanism | none")
        if "page" in kw:
            return SimpleNamespace(verdict="qualifies", conditions_met="all", conditions_failed="none",
                                   quote="lock SNX as collateral to mint sUSD")
        raise AssertionError(kw)

    verdicts, _ = asyncio.run(research_loop._inspect_candidates(
        "How did Synthetix work?", "native collateral; separate token", cards,
        SimpleNamespace(_call_research_loop_lm=fake), None))
    assert len(verdicts) == 1 and verdicts[0]["name"] == "Synthetix"
    assert reads == ["https://docs.synthetix.io/staking"]


def test_page_verdict_needs_a_literal_passage():
    page = "The board approved the proposal at 13:40 UTC on September 24, 2026."
    assert research_loop._verbatim_passage("approved the proposal at 13:40 UTC", page)
    assert research_loop._verbatim_passage("“approved the proposal at 13:40 UTC”", page)
    assert not research_loop._verbatim_passage("approved the proposal at 11:40 UTC", page)
    assert not research_loop._verbatim_passage("approved ... September 24", page)


def test_qualifying_conditions_require_a_page_passage_each():
    pages = [
        {"url": "https://docs.example/lock", "text": "Stakers lock NATIVE as collateral to mint UNIT.", "fetched_at": "replay"},
        {"url": "https://docs.example/trade", "text": "UNIT can be transferred and traded on open markets.", "fetched_at": "replay"},
    ]
    conditions = "native token collateral; separate tradable token"
    complete = json.dumps([
        {"condition": 1, "url": pages[0]["url"], "quote": "Stakers lock NATIVE as collateral to mint UNIT."},
        {"condition": 2, "url": pages[1]["url"], "quote": "UNIT can be transferred and traded on open markets."},
    ])
    assert len(research_loop._mapped_passages(complete, conditions, pages)) == 2
    assert research_loop._mapped_passages(json.dumps([json.loads(complete)[0]]), conditions, pages) is None
    assert research_loop._mapped_passages(complete.replace("transferred", "burned"), conditions, pages) is None
    partial = research_loop._validated_condition_map(json.dumps([json.loads(complete)[0]]), conditions, pages)
    assert [item["condition"] for item in partial] == [1]
    assert research_loop._missing_conditions(conditions, {"supported_condition_indices": [1],
                                                          "conditions_failed": "none"}) == "separate tradable token"


def test_independent_condition_audit_rejects_quote_that_does_not_prove_condition(monkeypatch):
    url = "https://docs.example.org/trading"
    quote = "Users may trade the minted UNIT on the protocol exchange."
    cards = f"# Web\nTrading is described [1].\n\nSources:\n[1] [Trading]({url}) · 2026-09-25"
    monkeypatch.setattr(research_loop.settings, "research_loop_inspect_pages", 1)
    monkeypatch.setattr(research_loop, "read_page", lambda _: {
        "url": url, "title": "Trading", "text": quote, "provenance": "page", "fetched_at": "replay"})

    async def fake(program, **kw):
        if "evidence" in kw:
            return SimpleNamespace(candidates=f"Example | {url}")
        if "page" in kw:
            return SimpleNamespace(verdict="qualifies", conditions_met="transferable", conditions_failed="none",
                                   quote=quote, evidence_map=json.dumps([{"condition": 1, "url": url, "quote": quote}]))
        if "evidence_map" in kw:
            return SimpleNamespace(unsupported_conditions="1")
        raise AssertionError(kw)

    verdicts, _ = asyncio.run(research_loop._inspect_candidates(
        "Could UNIT be sent to another address?", "wallet-to-wallet transferability", cards,
        SimpleNamespace(_call_research_loop_lm=fake), None))
    assert verdicts[0]["verdict"] == "not_established"
    assert "wallet-to-wallet transferability" in verdicts[0]["conditions_failed"]


def test_outer_search_does_not_recompose_a_research_loop_abstention(monkeypatch):
    from app.nodes import research as research_node
    monkeypatch.setattr(research_node.settings, "contract_pipeline_enabled", True)
    monkeypatch.setattr(research_node, "_web_context_part", lambda state: (_ for _ in ()).throw(AssertionError("outer search must not run")))
    monkeypatch.setattr(research_node, "_research_node", lambda state, sink: asyncio.sleep(0, result={
        "answer": "No cited page established the relationship.", "trajectory": None, "pipeline": "research_loop"}))
    out = asyncio.run(research_node.research_node({"request": "Research the historical Synthetix mechanism and its evidence",
                                                   "capabilities": ["web_research"], "chains": [], "history": "", "session_context": {}}))
    assert out["answer"] == "No cited page established the relationship."


def test_conceptual_trade_research_keeps_format_instructions_in_one_turn(monkeypatch):
    from app.nodes import research as research_node
    request = ("Which projects lock their native token to mint a separate token users can transfer to another wallet and trade? "
               "Give only examples whose original documentation establishes both steps, and label near misses.")
    seen = []
    monkeypatch.setattr(research_node.settings, "contract_pipeline_enabled", True)
    monkeypatch.setattr(research_node.token_pages, "parse", lambda request: None)
    monkeypatch.setattr(research_node, "_web_context_part", lambda state: (_ for _ in ()).throw(AssertionError("no outer search")))
    monkeypatch.setattr(research_node.composition, "plan_asks", lambda request: (["Which projects qualify?", "Give only examples..."], ""))

    async def fake(state, sink):
        seen.append(state["request"])
        return {"answer": "No fully verified match yet.", "trajectory": None, "pipeline": "research_loop"}

    monkeypatch.setattr(research_node, "_research_node", fake)
    out = asyncio.run(research_node.research_node({"request": request, "capabilities": ["web_research"], "chains": [],
                                                   "history": "", "session_context": {}}))
    assert seen == [request]
    assert out["answer"] == "No fully verified match yet."


def test_follow_up_targets_missing_condition_and_combines_page_evidence(monkeypatch):
    cards = ("# Web\nSynthetix could qualify [1].\n\nSources:\n"
             "[1] [Staking](https://docs.synthetix.io/staking) · 2026-09-25")
    pages = {
        "https://docs.synthetix.io/staking": "Stakers lock SNX as collateral to mint sUSD synthetic assets.",
        "https://docs.synthetix.io/susd": "sUSD is a transferable ERC-20 token traded on secondary markets.",
    }
    reads, searches = [], []
    monkeypatch.setattr(research_loop.settings, "research_loop_inspect_pages", 2)
    monkeypatch.setattr(research_loop, "read_page", lambda url: (reads.append(url) or {
        "url": url, "title": "docs", "text": pages[url], "provenance": "page", "fetched_at": "replay"}))

    def first_party(name, condition, exclude=None):
        searches.append((name, condition, exclude))
        return "https://docs.synthetix.io/susd"

    monkeypatch.setattr(research_loop, "first_party_url", first_party)

    async def fake(program, **kw):
        if "candidates" not in kw and "evidence" in kw:
            return SimpleNamespace(candidates="Synthetix | https://docs.synthetix.io/staking")
        if "page" in kw:
            if "transferable ERC-20" not in kw["page"]:
                return SimpleNamespace(verdict="not_established", conditions_met="own token collateral",
                                       conditions_failed="second token tradable", quote="none")
            assert "lock SNX as collateral" in kw["page"]
            return SimpleNamespace(verdict="qualifies", conditions_met="own token collateral; second token tradable",
                                   conditions_failed="none", quote="sUSD is a transferable ERC-20 token traded on secondary markets",
                                   supporting_passages="Stakers lock SNX as collateral to mint sUSD synthetic assets")
        raise AssertionError(kw)

    runtime = SimpleNamespace(_call_research_loop_lm=fake)
    verdicts, calls = asyncio.run(research_loop._inspect_candidates("which projects qualify", "own token collateral; second token tradable", cards, runtime, None))
    assert [v["verdict"] for v in verdicts] == ["qualifies"]
    assert verdicts[0]["url"] == "https://docs.synthetix.io/susd"
    assert {p["url"] for p in verdicts[0]["passages"]} == set(pages)
    assert searches == [("Synthetix", "second token tradable", {"https://docs.synthetix.io/staking"})]
    assert len(reads) == 2 and len([c for c in calls if c.startswith("page_read")]) == 2
    assert "_pages" not in verdicts[0]


def test_coverage_ledger_keeps_each_condition_and_exact_source():
    from app.research_ledger import CoverageLedger
    conditions = "native collateral; minted token transfers between wallets"
    verdicts = [{"name": "Example", "verdict": "not_established", "provenance": "page",
                 "condition_evidence": [{"condition": 1, "url": "https://example.org/lock",
                                         "quote": "NATIVE is locked to mint UNIT.", "fetched_at": "replay"}],
                 "note": "transfer evidence missing"}]
    ledger = CoverageLedger.from_verdicts(conditions, "collateral\ntransfer", verdicts)
    assert [record.status for record in ledger.records] == ["supported", "unknown"]
    assert ledger.records[0].source_url == "https://example.org/lock"
    assert ledger.missing("Example") == ["minted token transfers between wallets"]
    assert not ledger.complete("Example")
    assert "transfer evidence missing" in ledger.brief()
    assert "NATIVE is locked to mint UNIT." in ledger.brief()


def test_coverage_ledger_does_not_promote_search_or_unmapped_verdict():
    from app.research_ledger import CoverageLedger
    ledger = CoverageLedger.from_verdicts("native collateral", "", [{"name": "Example", "verdict": "qualifies",
                                                                      "provenance": "reader", "quote": "A search snippet says yes"}])
    assert ledger.records[0].status == "unknown"
    assert not ledger.complete("Example")


def test_research_turn_budget_counts_all_call_kinds():
    async def run():
        budget = research_loop.TurnBudget(20, model_limit=1)
        assert await budget.call("model", asyncio.sleep(0, result="ok"), 1) == "ok"
        assert await budget.call("provider", asyncio.sleep(0, result="web"), 1) == "web"
        assert await budget.call("page", asyncio.sleep(0, result="page"), 1) == "page"
        assert await budget.call("synthesis", asyncio.sleep(0, result="answer"), 1) == "answer"
        with pytest.raises(TimeoutError):
            await budget.call("model", asyncio.sleep(0), 1)
        assert budget.record()["calls"] == {"model": 1, "provider": 1, "page": 1, "synthesis": 1}
        assert budget.record()["exhausted"] == "model calls"
    asyncio.run(run())


def test_follow_up_uses_allowlisted_cited_page_before_a_new_search(monkeypatch):
    cards = ("# Web\nThe protocol documents staking [1] and sUSD trading [2].\n\nSources:\n"
             "[1] [Staking](https://docs.synthetix.io/staking) · 2026-09-25\n"
             "[2] [sUSD markets](https://docs.synthetix.io/susd) · 2026-09-25")
    pages = {
        "https://docs.synthetix.io/staking": "Stakers lock SNX as collateral to mint sUSD synthetic assets.",
        "https://docs.synthetix.io/susd": "sUSD is a transferable ERC-20 token traded on secondary markets.",
    }
    monkeypatch.setattr(research_loop.settings, "research_loop_inspect_pages", 2)
    monkeypatch.setattr(research_loop, "read_page", lambda url: {
        "url": url, "title": "docs", "text": pages[url], "provenance": "page", "fetched_at": "replay"})
    monkeypatch.setattr(research_loop, "first_party_url", lambda *args: (_ for _ in ()).throw(AssertionError("no extra search")))

    async def fake(program, **kw):
        if "evidence" in kw:
            return SimpleNamespace(candidates="Synthetix | https://docs.synthetix.io/staking")
        if "sources" in kw:
            assert kw["missing_condition"] == "second token tradable"
            return SimpleNamespace(url="https://docs.synthetix.io/susd")
        if "page" in kw:
            if "transferable ERC-20" not in kw["page"]:
                return SimpleNamespace(verdict="not_established", conditions_met="own token collateral",
                                       conditions_failed="second token tradable", quote="none")
            return SimpleNamespace(verdict="qualifies", conditions_met="own token collateral; second token tradable",
                                   conditions_failed="none", quote="sUSD is a transferable ERC-20 token traded on secondary markets")
        raise AssertionError(kw)

    verdicts, calls = asyncio.run(research_loop._inspect_candidates(
        "which projects qualify", "own token collateral; second token tradable", cards,
        SimpleNamespace(_call_research_loop_lm=fake), None))
    assert verdicts[0]["verdict"] == "qualifies"
    assert any(c.startswith("cited_source_selection:") for c in calls)


def test_followup_reads_rotate_between_unsettled_candidates(monkeypatch):
    cards = ("# Web\nAlpha [1] and Beta [2] are leads.\n\nSources:\n"
             "[1] [Alpha](https://alpha.org/one) · 2026-09-25\n"
             "[2] [Beta](https://beta.org/one) · 2026-09-25")
    monkeypatch.setattr(research_loop.settings, "research_loop_inspect_pages", 6)
    monkeypatch.setattr(research_loop, "read_page", lambda url: {
        "url": url, "title": "Docs", "text": "A document with a mechanism that does not yet settle the requested relationship.",
        "provenance": "page", "fetched_at": "replay"})
    searches = []

    def first_party(name, condition, exclude=None):
        searches.append(name)
        base = "alpha" if name == "Alpha" else "beta"
        for suffix in ("two", "three"):
            url = f"https://{base}.org/{suffix}"
            if url not in (exclude or set()):
                return url
        return None

    monkeypatch.setattr(research_loop, "first_party_url", first_party)

    async def fake(program, **kw):
        if "evidence" in kw:
            return SimpleNamespace(candidates="Alpha | https://alpha.org/one\nBeta | https://beta.org/one")
        if "page" in kw:
            return SimpleNamespace(verdict="not_established", conditions_met="none", conditions_failed="missing link", quote="none")
        raise AssertionError(kw)

    asyncio.run(research_loop._inspect_candidates(
        "Which qualifies?", "native collateral; transferable token", cards,
        SimpleNamespace(_call_research_loop_lm=fake), None, followup_reads=4))
    assert searches == ["Alpha", "Beta", "Alpha", "Beta"]


def test_page_check_keeps_later_cited_pages_in_context(monkeypatch):
    urls = [f"https://docs.example.com/{i}" for i in range(4)]
    cards = "# Web\nThe design is documented across several pages [1][2][3][4].\n\nSources:\n" + "\n".join(
        f"[{i + 1}] [Section {i + 1}]({url}) · 2026-09-25" for i, url in enumerate(urls))
    pages = {url: f"Section {i + 1}: " + "background " * 550 +
             ("The minted UNIT can be transferred to another wallet." if i == 3 else "")
             for i, url in enumerate(urls)}
    monkeypatch.setattr(research_loop.settings, "research_loop_inspect_pages", 4)
    monkeypatch.setattr(research_loop, "read_page", lambda url: {
        "url": url, "title": "docs", "text": pages[url], "provenance": "page", "fetched_at": "replay"})

    async def fake(program, **kw):
        if "evidence" in kw:
            return SimpleNamespace(candidates=f"Example | {urls[0]}")
        if "sources" in kw:
            already = kw["sources"]
            return SimpleNamespace(url=next(url for url in urls[1:] if url in already))
        if "page" in kw:
            if "The minted UNIT can be transferred" in kw["page"]:
                return SimpleNamespace(verdict="qualifies", conditions_met="all", conditions_failed="none",
                                       quote="The minted UNIT can be transferred to another wallet.")
            return SimpleNamespace(verdict="not_established", conditions_met="none", conditions_failed="transferability", quote="none")
        raise AssertionError(kw)

    verdicts, calls = asyncio.run(research_loop._inspect_candidates(
        "Can the minted token transfer?", "transferability", cards,
        SimpleNamespace(_call_research_loop_lm=fake), None))
    assert verdicts[0]["verdict"] == "qualifies"
    assert len([call for call in calls if call.startswith("page_read")]) == 4


def _script(plan_queries: str, reviews: list[str], support=("Synthetix", "Aave", "none")):
    """A fake research tier: answers by program, in order."""
    state = {"reviews": list(reviews)}

    async def call(program, **kw):
        name = type(program.signature).__name__ if hasattr(program, "signature") else ""
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice VVV/DIEM", question="which other projects lock their own token to mint a tradable second token",
                                   constraints="own token as collateral; second token tradable", required_facts="named projects\nmechanism per project\nfirst-party source",
                                   capabilities="web_discovery", queries=plan_queries)
        if "calls_made" in fields:
            nxt = state["reviews"].pop(0) if state["reviews"] else "stop"
            return SimpleNamespace(missing="named projects" if nxt != "stop" else "none", next_call=nxt, reason="test")
        if "answer" in fields and "evidence" in fields:
            if "not a match" in kw["answer"]:                       # the rewrite relabelled the flagged name
                return SimpleNamespace(supported=support[0], related_but_different="none", not_established="none")
            return SimpleNamespace(supported=support[0], related_but_different=support[1], not_established=support[2])
        return SimpleNamespace(summary="**Taken together**\n\nsummary")
    return call


@pytest.fixture
def loop(monkeypatch):
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    monkeypatch.setattr(evidence_pipeline.settings, "discovery_first_research", True)
    monkeypatch.setattr(evidence_pipeline.settings, "research_loop_enabled", True)
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: False)

    async def synth(request, cards, trajectory, advice=False, research=False):
        if "state each of them only as such" in request:          # the one rewrite after the support check flagged a name
            return f"**Taken together**\n\nVenice and Synthetix lock their own token. Aave is a receipt-token design, not a match.\n\n---\n\n{cards}"
        return f"**Taken together**\n\nVenice and Synthetix lock their own token; Aave issues receipts.\n\n---\n\n{cards}"

    monkeypatch.setattr(evidence_pipeline.composition, "synthesize", synth)
    monkeypatch.setattr(research_loop.composition, "synthesize", synth)
    return monkeypatch


def _run(monkeypatch, fake_lm, outputs, request="which other projects follow lock collateral → mint a tradable second token"):
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "_call_research_lm", fake_lm)
    monkeypatch.setattr(runtime, "_call_research_loop_lm", fake_lm)
    router = FakeRouter(outputs)
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, request, ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    return out, router


def test_the_loop_answers_open_research_when_the_flag_is_on(loop):
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "", "text": "", "provenance": "unreadable", "fetched_at": "replay"})
    class Rotating(FakeRouter):
        def invoke(self, name, request, chains=()):
            self.calls.append(name)
            out = WEB1 if len(self.calls) == 1 else WEB2
            return SimpleNamespace(output=out, tool=name, provider="fake")
    fake = _script("web_discovery: projects that lock their own token to mint a tradable token", ["web_discovery: first-party descriptions of lock-to-mint designs adapted from Venice"])
    from app.nodes import runtime
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Rotating({})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert out["pipeline"] == "research_loop"
    assert router.calls == ["perplexity_web_search", "perplexity_web_search"]        # the plan's query, then the gap review's second axis
    record = out["trajectory"]["research_loop"]
    assert len([c for c in record["calls"] if c.startswith("web_discovery")]) == 2 and record["constraints"].startswith("own token")
    assert "I withheld the written summary" in out["answer"]       # cited pages were unreadable, so the snippet is not proof
    assert "https://docs.synthetix.io" in out["answer"] and "page unreadable" in out["answer"]
    assert "Venice locks sVVV to mint DIEM" not in out["answer"]     # rejected search prose is diagnostic data, not a second answer


def test_the_loop_tries_a_distinct_source_axis_when_review_stops_without_page_support(loop):
    fake = _script("web_discovery: q1", ["stop"])
    out, router = _run(loop, fake, {"perplexity_web_search": WEB1})
    assert out["pipeline"] == "research_loop" and router.calls == ["perplexity_web_search", "perplexity_web_search"]
    assert any("historical projects" in call and "original primary documentation" in call
               for call in out["trajectory"]["research_loop"]["calls"])


def test_gap_review_uses_checked_page_state_not_search_prose(loop):
    from app.nodes import runtime

    seen = []
    inspections = []

    async def inspected(question, constraints, cards, runtime_arg, router, page_cache=None, followup_reads=None, budget=None, check_cache=None):
        inspections.append((cards, followup_reads))
        if len(inspections) == 1:
            return ([{"name": "Example", "url": "https://example.org/first", "verdict": "not_established",
                      "provenance": "page", "quote": "The first token is locked as collateral.",
                      "conditions_failed": "the minted token is transferable", "supported_condition_indices": [1],
                      "passages": [{"url": "https://example.org/first", "quote": "The first token is locked as collateral."}]}],
                    ["page_read (url_reader): https://example.org/first"])
        return ([], [])

    async def fake(program, **kw):
        if "request" in kw and "catalog" in kw:
            return SimpleNamespace(question="Which tokens qualify?", constraints="collateral token locked; the minted token is transferable",
                                   required_facts="lock mechanism\ntransferability", queries="web_discovery: broad search")
        if "calls_made" in kw:
            seen.append(kw["evidence"])
            return SimpleNamespace(next_call="web_discovery: Example minted token transfer documentation")
        raise AssertionError(kw)

    loop.setattr(research_loop, "_inspect_candidates", inspected)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    out, router = _run(loop, fake, {"perplexity_web_search": WEB1})
    assert len(router.calls) == 2
    assert [allowance for _, allowance in inspections] == [0, 4]
    assert "the minted token is transferable" in seen[0]
    assert "The first token is locked as collateral." in seen[0]
    assert "Venice locks sVVV to mint DIEM" not in seen[0]
    assert out["trajectory"]["research_loop"]["calls"][0].startswith("web_discovery")


def test_unproved_candidate_cannot_monopolize_the_second_search(loop):
    from app.nodes import runtime

    async def inspected(question, constraints, cards, runtime_arg, router, **kwargs):
        if "second query" in cards:
            return [], []
        return [{"name": "WeakLead", "url": "https://example.org/weak", "verdict": "not_established",
                 "provenance": "page", "quote": "none", "conditions_failed": "native collateral"}], []

    async def fake(program, **kw):
        if "request" in kw and "catalog" in kw:
            return SimpleNamespace(question="Which projects qualify?", constraints="own governance token collateral; minted transferable asset",
                                   required_facts="original proof", queries="web_discovery: first query")
        if "calls_made" in kw:
            return SimpleNamespace(next_call="web_discovery: WeakLead redemption documentation")
        raise AssertionError(kw)

    loop.setattr(research_loop, "_inspect_candidates", inspected)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    out, router = _run(loop, fake, {"perplexity_web_search": WEB1})
    assert len(router.calls) == 2
    assert any("own governance token collateral" in call and "original primary documentation" in call
               for call in out["trajectory"]["research_loop"]["calls"])
    assert not any("WeakLead redemption" in call for call in out["trajectory"]["research_loop"]["calls"])


def test_partially_supported_candidate_keeps_a_parallel_discovery_axis(loop):
    from app.nodes import runtime
    rounds = []

    async def inspected(question, constraints, cards, runtime_arg, router, **kwargs):
        rounds.append(1)
        if len(rounds) == 1:
            return [{"name": "PartialLead", "url": "https://example.org/one", "verdict": "not_established",
                     "provenance": "page", "condition_evidence": [{"condition": 1, "url": "https://example.org/one",
                                                               "quote": "Its native token is locked as collateral."}],
                     "conditions_failed": "minted token tradable"}], []
        return [], []

    async def fake(program, **kw):
        if "request" in kw and "catalog" in kw:
            return SimpleNamespace(question="Which projects qualify?", constraints="native collateral; minted token tradable",
                                   required_facts="original proof", queries="web_discovery: initial discovery")
        if "calls_made" in kw:
            return SimpleNamespace(next_call="web_discovery: PartialLead minted token trading original docs")
        raise AssertionError(kw)

    loop.setattr(research_loop, "_inspect_candidates", inspected)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    out, router = _run(loop, fake, {"perplexity_web_search": WEB1})
    assert len(router.calls) == 3
    calls = [call for call in out["trajectory"]["research_loop"]["calls"] if call.startswith("web_discovery")]
    assert "historical projects native collateral; minted token tradable" in calls[1]
    assert "PartialLead minted token trading" in calls[2]


def test_reinspection_reuses_page_read_across_rounds(monkeypatch):
    url = "https://example.org/protocol"
    cards = f"# Web\nProtocol documentation [1].\n\nSources:\n[1] [Docs]({url}) · 2026-09-25"
    reads = []
    monkeypatch.setattr(research_loop.settings, "research_loop_inspect_pages", 1)
    monkeypatch.setattr(research_loop, "read_page", lambda address: (reads.append(address) or {
        "url": address, "title": "Docs", "text": "The protocol token is locked to mint a separate transferable token.",
        "provenance": "page", "fetched_at": "replay"}))

    checks = []
    async def fake(program, **kw):
        if "evidence" in kw:
            return SimpleNamespace(candidates=f"Protocol | {url}")
        if "page" in kw:
            checks.append(1)
            return SimpleNamespace(verdict="qualifies", conditions_met="all", conditions_failed="none",
                                   quote="The protocol token is locked to mint a separate transferable token.")
        raise AssertionError(kw)

    cache, checked = {}, {}
    for _ in range(2):
        asyncio.run(research_loop._inspect_candidates("Does it qualify?", "separate transferable token", cards,
                                                       SimpleNamespace(_call_research_loop_lm=fake), None, page_cache=cache,
                                                       check_cache=checked))
    assert reads == [url]
    assert checks == [1]


def test_shallow_inspection_reserves_followup_budget_for_gap_search(monkeypatch):
    url = "https://example.org/partial"
    cards = f"# Web\nThe design is discussed [1].\n\nSources:\n[1] [Docs]({url}) · 2026-09-25"
    monkeypatch.setattr(research_loop.settings, "research_loop_inspect_pages", 4)
    monkeypatch.setattr(research_loop, "read_page", lambda address: {
        "url": address, "title": "Docs", "text": "The protocol locks its own token as collateral.",
        "provenance": "page", "fetched_at": "replay"})
    monkeypatch.setattr(research_loop, "first_party_url", lambda *args: (_ for _ in ()).throw(AssertionError("gap planner must go first")))

    async def fake(program, **kw):
        if "evidence" in kw:
            return SimpleNamespace(candidates=f"Protocol | {url}")
        if "page" in kw:
            return SimpleNamespace(verdict="not_established", conditions_met="collateral", conditions_failed="transferable minted token",
                                   quote="The protocol locks its own token as collateral.")
        raise AssertionError(kw)

    verdicts, calls = asyncio.run(research_loop._inspect_candidates(
        "Does it qualify?", "own-token collateral; transferable minted token", cards,
        SimpleNamespace(_call_research_loop_lm=fake), None, followup_reads=0))
    assert verdicts[0]["verdict"] == "not_established"
    assert not any(call.startswith("first_party_search") for call in calls)


def test_a_state_read_needs_a_resolved_contract(loop):
    fake = _script("market_state: DIEM pools\nweb_discovery: q1", ["stop"])
    out, router = _run(loop, fake, {"perplexity_web_search": WEB1, "dexscreener_token_pairs": "# DEX pairs\n| DIEM/WETH |"})
    assert "dexscreener_token_pairs" not in router.calls and "birdeye_token_overview" not in router.calls
    assert any("needs a contract address" in s for s in out["trajectory"]["research_loop"]["skipped"])


def test_the_loop_never_exceeds_its_call_budget(loop):
    fake = _script("web_discovery: q1\nweb_discovery: q2", ["web_discovery: q3", "web_discovery: q4", "web_discovery: q5", "web_discovery: q6"])
    out, router = _run(loop, fake, {"perplexity_web_search": WEB1})
    assert len(router.calls) <= research_loop.MAX_CALLS


def test_typed_plan_runs_four_independent_initial_searches(loop):
    from app.nodes import runtime
    loop.setattr(research_loop.settings, "research_query_expansion_enabled", True)
    searches = [
        {"facet": facet, "capability": "web_discovery", "query": query, "topic": "general", "domains": []}
        for facet, query in (("design", "lock native token mint transferable token designs"),
                             ("collateral", "protocol primary docs own token collateral"),
                             ("minted asset", "protocol primary docs minted asset transfer"),
                             ("redemption", "protocol primary docs redemption mechanism"))
    ]

    async def fake(program, **kw):
        if "request" in kw and "catalog" in kw:
            return SimpleNamespace(subject="dual-token designs", question="which designs qualify?",
                                   constraints="own-token collateral; transferable minted token", required_facts="original docs",
                                   queries="web_discovery: legacy", sub_queries=json.dumps(searches))
        if "calls_made" in kw:
            return SimpleNamespace(next_call="stop")
        return SimpleNamespace(candidates="none", supported="none", related_but_different="none", not_established="none")

    loop.setattr(runtime, "_call_research_loop_lm", fake)
    loop.setattr(research_loop, "_inspect_candidates", lambda *args, **kwargs: asyncio.sleep(0, result=([], [])))
    out, router = _run(loop, fake, {"perplexity_web_search": WEB1})
    calls = [call for call in out["trajectory"]["research_loop"]["calls"] if call.startswith("web_discovery")]
    assert len(calls) >= 4 and len(router.calls) <= research_loop.MAX_CALLS
    assert all(any(query in call for call in calls[:4]) for query in [row["query"] for row in searches])


def test_a_failed_plan_falls_back_to_the_fixed_sequence(loop):
    async def down(program, **kw):
        raise RuntimeError("model down")
    out, router = _run(loop, down, {"perplexity_web_search": WEB1})
    assert out is not None and out.get("pipeline") == "contract" and router.calls == ["perplexity_web_search"]


def test_the_flag_off_keeps_the_fixed_sequence(monkeypatch):
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    monkeypatch.setattr(evidence_pipeline.settings, "discovery_first_research", True)
    monkeypatch.setattr(evidence_pipeline.settings, "research_loop_enabled", False)
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: False)

    async def synth(request, cards, trajectory, advice=False, research=False):
        return f"**Taken together**\n\nsummary\n\n---\n\n{cards}"

    async def never(program, **kw):
        raise AssertionError("the loop must not run with the flag off")

    monkeypatch.setattr(evidence_pipeline.composition, "synthesize", synth)
    out, router = _run(monkeypatch, never, {"perplexity_web_search": WEB1})
    assert out["pipeline"] == "contract"


def test_tool_for_applies_the_deterministic_boundary():
    contract = plan_by_rules("which other projects follow lock collateral → mint a tradable second token")
    enabled = {"perplexity_web_search", "dexscreener_token_pairs", "mobula_wallet_portfolio", "search_verified_tokens"}
    assert research_loop.tool_for("web_discovery", contract, enabled) == ("perplexity_web_search", "eligible")
    assert research_loop.tool_for("market_state", contract, enabled)[0] is None
    assert research_loop.tool_for("wallet_state", contract, enabled)[0] is None
    assert research_loop.tool_for("token_identity", contract, enabled) == ("search_verified_tokens", "eligible")
    assert research_loop.tool_for("execution_quotes", contract, enabled)[0] is None
    resolved = contract.model_copy(update={"subject": contract.subject.model_copy(update={"kind": "token", "id": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "chain": "solana"})})
    assert research_loop.tool_for("market_state", resolved, enabled | {"birdeye_token_overview"}) == ("birdeye_token_overview", "eligible")
    assert research_loop.tool_for("market_state", resolved, {"coingecko_gainers_losers"})[0] is None      # a ranking tool serves rankings, not research


def test_the_capability_catalog_is_derived_from_the_tool_catalog():
    caps = research_loop.capabilities()
    assert "perplexity_web_search" in caps["web_discovery"]["tools"] and "mobula_token_holders" in caps["on_chain_ownership"]["tools"]
    assert caps["execution_quotes"]["tools"] == []
    assert "web_discovery" in research_loop.catalog_summary()


def test_candidates_are_checked_from_their_pages_and_the_trail_says_why(loop):
    from app.nodes import runtime
    pages = {"https://docs.synthetix.io": "Stakers lock SNX as collateral to mint sUSD, which trades on open markets.",
             "https://docs.aave.com": "Depositors receive aTokens representing their supplied assets."}
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "docs", "text": pages.get(url, ""), "provenance": "page" if url in pages else "unreadable", "fetched_at": "2026-09-24 12:00 UTC"})
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)
    state = {"reviews": ["stop"]}

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="which other projects lock their own token to mint a tradable second token",
                                   constraints="own token as collateral; second token tradable", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "candidates" not in fields and "answer" not in fields:
            return SimpleNamespace(candidates="Synthetix | https://docs.synthetix.io\nAave | https://docs.aave.com")
        if "page" in fields:
            if kw["candidate"] == "Synthetix":
                return SimpleNamespace(verdict="qualifies", conditions_met="own token as collateral; second token tradable", conditions_failed="none", quote="lock SNX as collateral to mint sUSD")
            return SimpleNamespace(verdict="related_but_different", conditions_met="none", conditions_failed="own token as collateral", quote="aTokens representing their supplied assets")
        if "answer" in fields:
            return SimpleNamespace(supported="Synthetix", related_but_different="Aave", not_established="none")
        return SimpleNamespace()

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": WEB2})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    rec = out["trajectory"]["research_loop"]
    assert [v["verdict"] for v in rec["verdicts"]] == ["qualifies", "related_but_different"]
    assert "**Research trail**" in out["answer"] and "Synthetix (https://docs.synthetix.io; page read, 2026-09-24 12:00 UTC): qualifies" in out["answer"]
    assert "Aave (https://docs.aave.com; page read, 2026-09-24 12:00 UTC): related but a different design: own token as collateral" in out["answer"]
    assert {"plan", "retrieval", "inspect", "synthesis", "total"} <= set(rec["timings"])



def test_a_snippet_that_asserts_what_the_page_does_not_is_not_established(loop):
    # The search result says Nova Labs locks its token to mint ASTRA; the page says nothing of the kind.
    from app.nodes import runtime
    snippet = ("# From the web (dated, with sources)\n**Query**: q\n\nNova Labs locks NOVA to mint tradable ASTRA credits. [1] Venice locks sVVV to mint DIEM. [2]\n\n"
               "Sources:\n[1] [Nova Labs blog](https://novalabs.example/blog) · 2026-09-10\n[2] [Venice docs](https://venice.ai/docs) · 2026-09-09")
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "Nova Labs", "text": "Nova Labs announces ASTRA, a token for its compute marketplace. Details of the token model will follow.", "provenance": "page", "fetched_at": "2026-09-24 12:00 UTC"}
                 if "novalabs" in url else {"url": url, "title": "", "text": "", "provenance": "unreadable", "fetched_at": ""})
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="which other projects lock their own token to mint a tradable second token",
                                   constraints="own token as collateral; second token tradable", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            return SimpleNamespace(candidates="Nova Labs | https://novalabs.example/blog")
        if "page" in fields:
            assert "compute marketplace" in kw["page"]                 # the page, not the snippet, is what is checked
            return SimpleNamespace(verdict="not_established", conditions_met="none", conditions_failed="none", quote="none")
        if "answer" in fields:
            return SimpleNamespace(supported="none", related_but_different="none", not_established="Nova Labs")
        return SimpleNamespace()

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": snippet})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert out["trajectory"]["research_loop"]["verdicts"][0]["verdict"] == "not_established"
    assert "I withheld the written summary" in out["answer"] and "Nova Labs" in out["answer"]      # the claim never stands, the trail shows why
    assert "Nova Labs (https://novalabs.example/blog; page read" in out["answer"] and "not established by its page" in out["answer"]


def test_an_unreadable_page_is_a_lead_not_evidence(loop):
    from app.nodes import runtime
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "", "text": "", "provenance": "unreadable", "fetched_at": "2026-09-24 12:00 UTC"})
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="own token as collateral", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            return SimpleNamespace(candidates="Synthetix | https://docs.synthetix.io")
        if "page" in fields:
            raise AssertionError("an unreadable page is never checked")
        if "answer" in fields:
            return SimpleNamespace(supported="none", related_but_different="none", not_established="Synthetix")
        return SimpleNamespace()

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": WEB2})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    v = out["trajectory"]["research_loop"]["verdicts"][0]
    assert v["verdict"] == "not_established" and v["provenance"] == "unreadable"
    assert "page unreadable" in out["answer"] and "search lead" in out["answer"] and "I withheld the written summary" in out["answer"]


def test_an_unsettled_candidate_gets_one_first_party_read(loop):
    from app.nodes import runtime
    pages = {"https://www.gate.com/blog/synthetix": "Synthetix is a derivatives protocol. It has a token called SNX.",
             "https://docs.synthetix.io/staking": "Stakers lock SNX as collateral to mint sUSD, an ERC-20 that trades on open markets; burning sUSD releases the SNX."}
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "t", "text": pages.get(url, ""), "provenance": "page" if url in pages else "unreadable", "fetched_at": "replay"})
    loop.setattr(research_loop, "first_party_url", lambda name, conditions, exclude=None: "https://docs.synthetix.io/staking" if name == "Synthetix" else None)
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)
    cited_blog = WEB2 + "\n[3] [Synthetix on Gate](https://www.gate.com/blog/synthetix) · 2026-09-02"

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="own token as collateral; second token tradable", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            return SimpleNamespace(candidates="Synthetix | https://www.gate.com/blog/synthetix")
        if "page" in fields:
            ok = "mint sUSD" in kw["page"]
            return SimpleNamespace(verdict="qualifies" if ok else "not_established", conditions_met="own token as collateral; second token tradable" if ok else "none", conditions_failed="none", quote="lock SNX as collateral to mint sUSD" if ok else "none")
        if "answer" in fields:
            return SimpleNamespace(supported="Synthetix", related_but_different="none", not_established="none")
        return SimpleNamespace()

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": cited_blog})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    v = out["trajectory"]["research_loop"]["verdicts"][0]
    assert v["verdict"] == "qualifies" and v["url"] == "https://docs.synthetix.io/staking" and "first-party page found by a follow-up search" in v["note"]
    assert "Synthetix (https://docs.synthetix.io/staking; page read, replay): qualifies" in out["answer"]


def test_a_candidate_named_without_a_url_gets_its_page_from_a_search(loop):
    from app.nodes import runtime
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "t", "text": "Stakers lock SNX as collateral to mint sUSD, which trades on open markets.", "provenance": "page", "fetched_at": "replay"})
    loop.setattr(research_loop, "first_party_url", lambda name, conditions, exclude=None: "https://docs.synthetix.io/staking" if name == "Synthetix" else None)
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="own token as collateral; second token tradable", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            return SimpleNamespace(candidates="- Synthetix\n- none")
        if "page" in fields:
            return SimpleNamespace(verdict="qualifies", conditions_met="own token as collateral; second token tradable", conditions_failed="none", quote="lock SNX as collateral to mint sUSD")
        if "answer" in fields:
            return SimpleNamespace(supported="Synthetix", related_but_different="none", not_established="none")
        return SimpleNamespace()

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": WEB2})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    v = out["trajectory"]["research_loop"]["verdicts"][0]
    assert v["name"] == "Synthetix" and v["url"] == "https://docs.synthetix.io/staking" and v["verdict"] == "qualifies"



def test_the_reader_refuses_private_and_local_destinations(monkeypatch):
    from app import url_reader
    for bad in ("http://127.0.0.1/", "http://localhost:8000/x", "http://10.0.0.5/admin", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd", "http://[::1]/"):
        assert not url_reader.public_destination(bad), bad
    # A public destination is decided by the resolved address. Keep this
    # assertion independent of the test runner's outbound DNS policy.
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))])
    assert url_reader.public_destination("https://docs.synthetix.io/staking")


def test_a_fetch_connects_to_the_address_that_passed_the_check(monkeypatch):
    from app import url_reader
    connected = []

    def resolve(url):
        return ("public.example", 443, "203.0.113.7", "https") if "public.example" in url else None

    def get(url, host, port, address, scheme):
        connected.append((host, address))
        return 200, {"content-type": "text/html; charset=utf-8"}, "<html><title>t</title><body>" + "<p>Stakers lock SNX as collateral to mint sUSD.</p>" * 40 + "</body></html>"

    monkeypatch.setattr(url_reader, "resolve_public", resolve)
    monkeypatch.setattr(url_reader, "_get_pinned", get)
    got = url_reader.fetch("https://public.example/page")
    assert got and got[0] == "t" and "lock SNX" in got[1]
    assert connected == [("public.example", "203.0.113.7")]          # the request went to the checked address, no second lookup


def test_a_redirect_to_a_private_destination_is_refused(monkeypatch):
    from app import url_reader
    seen = []

    def resolve(url):
        return ("public.example", 443, "203.0.113.7", "https") if "public.example" in url else None

    def get(url, host, port, address, scheme):
        seen.append(url)
        return 302, {"location": "http://127.0.0.1/secret"}, ""

    monkeypatch.setattr(url_reader, "resolve_public", resolve)
    monkeypatch.setattr(url_reader, "_get_pinned", get)
    assert url_reader.fetch("https://public.example/page") is None
    assert seen == ["https://public.example/page"]                  # the private hop was never requested


def test_the_reader_fallback_never_gets_a_rejected_url(monkeypatch):
    from app import perplexity_tools, url_reader
    monkeypatch.setattr(url_reader, "resolve_public", lambda url: None)
    monkeypatch.setattr(url_reader, "fetch", lambda url: None)
    monkeypatch.setattr(perplexity_tools, "perplexity_available", lambda: True)
    monkeypatch.setattr(perplexity_tools, "perplexity_fetch_url", lambda url: (_ for _ in ()).throw(AssertionError("fallback must not run")))
    page = research_loop.read_page("http://169.254.169.254/latest")
    assert page["provenance"] == "unreadable" and page["note"] == "not a public destination"


def test_only_cited_urls_are_read_and_the_page_budget_holds(loop):
    from app.nodes import runtime
    reads = []
    loop.setattr(research_loop, "read_page", lambda url: (reads.append(url) or {"url": url, "title": "t", "text": "Depositors receive aTokens representing their supplied assets.", "provenance": "page", "fetched_at": "replay"}))
    loop.setattr(research_loop, "first_party_url", lambda name, conditions, exclude=None: None)
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 2, raising=False)

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="own token as collateral", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            # one cited URL, one URL the model made up, and a name with none
            return SimpleNamespace(candidates="Aave | https://docs.aave.com\nEvil | http://169.254.169.254/latest\nSynthetix | https://docs.synthetix.io\nNova | none")
        if "page" in fields:
            return SimpleNamespace(verdict="not_established", conditions_met="none", conditions_failed="none", quote="none")
        if "answer" in fields:
            return SimpleNamespace(supported="none", related_but_different="none", not_established="none")
        return SimpleNamespace()

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": WEB2})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert "http://169.254.169.254/latest" not in reads                    # a URL the model wrote is never fetched
    assert len(reads) <= 2 and all(u in ("https://docs.aave.com", "https://docs.synthetix.io") for u in reads)


def test_a_failed_support_check_withholds_the_summary(loop):
    from app.nodes import runtime
    async def inspected(*args, **kwargs):
        return ([{"name": "Synthetix", "url": "https://docs.synthetix.io", "verdict": "qualifies", "provenance": "page",
                  "quote": "Stakers lock SNX as collateral to mint sUSD, which trades on open markets.",
                  "conditions_met": "own token collateral", "conditions_failed": "none", "fetched_at": "replay",
                  "condition_evidence": [{"condition": 1, "url": "https://docs.synthetix.io",
                                          "quote": "Stakers lock SNX as collateral to mint sUSD, which trades on open markets.",
                                          "fetched_at": "replay"}]}], ["page_read (url_reader): https://docs.synthetix.io"])
    loop.setattr(research_loop, "_inspect_candidates", inspected)

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="none", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "answer" in fields:
            raise RuntimeError("model down")
        return SimpleNamespace()

    out, router = _run(loop, fake, {"perplexity_web_search": WEB2})
    assert out["pipeline"] == "research_loop" and "I withheld the written summary: the example check could not run" in out["answer"]


def test_the_conversation_lock_outlives_a_research_turn(monkeypatch):
    from app import sessions
    seen = {}

    class Lock:
        async def acquire(self):
            return True
        async def release(self):
            return None

    class Redis:
        def lock(self, name, timeout, blocking_timeout):
            seen["timeout"] = timeout
            return Lock()

    async def redis():
        return Redis()

    monkeypatch.setattr(sessions, "get_redis", redis)
    monkeypatch.setattr(sessions.settings, "chat_execution_timeout_seconds", 120.0)
    monkeypatch.setattr(sessions.settings, "research_loop_timeout_seconds", 240.0, raising=False)
    monkeypatch.setattr(sessions.settings, "research_loop_enabled", True, raising=False)
    asyncio.run(sessions.acquire_session_turn("s1"))
    assert seen["timeout"] == 255
    monkeypatch.setattr(sessions.settings, "research_loop_enabled", False, raising=False)
    asyncio.run(sessions.acquire_session_turn("s2"))
    assert seen["timeout"] == 135



def test_a_page_verdict_gates_the_summary_whatever_the_snippet_said(loop):
    # The search snippet says Nova Labs qualifies; its page did not establish it; the support model, reading snippets, sees no problem.
    from app.nodes import runtime
    loop.setattr(research_loop, "read_page", lambda url: {"url": url, "title": "Nova", "text": "Nova Labs announces ASTRA. Details will follow.", "provenance": "page", "fetched_at": "replay"})
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)
    snippet = ("# From the web (dated, with sources)\n**Query**: q\n\nNova Labs locks NOVA to mint tradable ASTRA credits. [1]\n\n"
               "Sources:\n[1] [Nova Labs blog](https://novalabs.example/blog) · 2026-09-10")

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="own token as collateral; second token tradable", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            return SimpleNamespace(candidates="Nova Labs | https://novalabs.example/blog")
        if "page" in fields:
            return SimpleNamespace(verdict="not_established", conditions_met="none", conditions_failed="none", quote="none")
        if "answer" in fields:
            return SimpleNamespace(supported="Nova Labs", related_but_different="none", not_established="none")      # the snippet convinced it
        return SimpleNamespace()

    async def synth(request, cards, trajectory, advice=False, research=False):
        return f"**Taken together**\n\nNova Labs locks NOVA to mint tradable ASTRA, a match.\n\n---\n\n{cards}"     # the rewrite does not relabel it either

    loop.setattr(evidence_pipeline.composition, "synthesize", synth)
    loop.setattr(research_loop.composition, "synthesize", synth)

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": snippet})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert "I withheld the written summary" in out["answer"] and "Nova Labs" in out["answer"]
    assert research_loop.unlabelled_names("Nova Labs is related but a different design.", [{"name": "Nova Labs", "verdict": "not_established"}]) == []
    assert any(c.startswith("page_read (url_reader): https://novalabs.example/blog") for c in out["trajectory"]["research_loop"]["calls"])


def test_a_failed_candidate_listing_withholds_the_summary(loop):
    from app.nodes import runtime
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 4, raising=False)

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="none", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            raise RuntimeError("model down")
        raise AssertionError("nothing runs after the listing fails")

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": WEB2})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert "I withheld the written summary: the candidate check could not run" in out["answer"]
    assert out["trajectory"]["research_loop"]["withheld"] == "candidate check failed"


def test_a_disclaimer_covers_only_its_own_clause():
    verdicts = [{"name": "Nova Labs", "verdict": "not_established"}, {"name": "Aave", "verdict": "related_but_different"}]
    assert research_loop.unlabelled_names("Nova Labs is an exact match, but Aave is not established.", verdicts) == ["Nova Labs"]
    assert research_loop.unlabelled_names("Nova Labs and Aave are not established by their pages.", verdicts) == []
    assert research_loop.unlabelled_names("Aave is a receipt design; Nova Labs qualifies.", verdicts) == ["Nova Labs"]
    assert research_loop.unlabelled_names("Kava is related_but_different; Synthetix is not_established.",
                                          [{"name": "Kava", "verdict": "related_but_different"},
                                           {"name": "Synthetix", "verdict": "not_established"}]) == []


def test_correctly_labelled_exclusions_survive_support_classification(monkeypatch):
    async def fake(program, **kw):
        return SimpleNamespace(related_but_different="Kava", not_established="Synthetix")

    async def no_rewrite(*args, **kwargs):
        raise AssertionError("a labelled exclusion needs no rewrite")

    monkeypatch.setattr(research_loop.composition, "synthesize", no_rewrite)
    answer = "Kava is related_but_different; Synthetix is not_established. No full match was verified."
    verdicts = [{"name": "Kava", "verdict": "related_but_different"},
                {"name": "Synthetix", "verdict": "not_established"}]
    got = asyncio.run(research_loop._verify_examples("which matches?", answer, "pages", "request", "note", {},
                                                     SimpleNamespace(_call_research_loop_lm=fake), verdicts))
    assert got == answer


def test_example_checker_claim_phrases_do_not_masquerade_as_project_names(monkeypatch):
    async def fake(program, **kw):
        return SimpleNamespace(related_but_different="none",
                               not_established="the deprecation date, whether sUSD could be sent to every arbitrary address")

    async def no_rewrite(*args, **kwargs):
        raise AssertionError("the claim audit, not the name gate, reviews claim phrases")

    monkeypatch.setattr(research_loop.composition, "synthesize", no_rewrite)
    answer = "Synthetix historically minted sUSD; current mechanics are not established by these pages."
    got = asyncio.run(research_loop._verify_examples(
        "How did it work?", answer, "pages", "request", "note", {},
        SimpleNamespace(_call_research_loop_lm=fake),
        [{"name": "Synthetix", "verdict": "qualifies"}]))
    assert got == answer


def test_claim_audit_flags_current_mechanics_absent_from_reviewed_passages():
    async def fake(program, **kw):
        assert "current v3 vaults" in kw["summary"].lower()
        assert "SNX into a smart contract" in kw["evidence"]
        return SimpleNamespace(unsupported_claims="Current V3 vaults mint sUSD through delta-neutral strategies.")

    got = asyncio.run(research_loop._unsupported_claims(
        "How did the mechanism change?",
        "Current V3 vaults mint sUSD through delta-neutral strategies.\n\n---\n\n# Cards",
        "SNX into a smart contract", SimpleNamespace(_call_research_loop_lm=fake)))
    assert got == ["Current V3 vaults mint sUSD through delta-neutral strategies."]


def test_withheld_summary_surfaces_only_reviewed_passages_up_front():
    cards = ("# Reviewed source-page passages\n\n"
             "- **Synthetix** — qualifies · [page](https://blog.synthetix.io/a-mintroduction/) · fetched replay: "
             "“SNX was locked to mint Synths.”")
    answer = research_loop._withheld("the full claim was not established", cards)
    lead, _, sources = answer.partition("\n\n---\n\n")
    assert "Directly checked passages" in lead
    assert "SNX was locked to mint Synths" in lead
    assert "full requested conclusion remains unverified" in lead
    assert not sources                         # reviewed passage is shown once, not repeated as a raw card


def test_candidates_the_budget_leaves_unread_are_not_established(loop):
    from app.nodes import runtime
    reads = []
    loop.setattr(research_loop, "read_page", lambda url: (reads.append(url) or {"url": url, "title": "t", "text": "Depositors receive aTokens.", "provenance": "page", "fetched_at": "replay"}))
    loop.setattr(research_loop, "first_party_url", lambda name, conditions, exclude=None: None)
    loop.setattr(research_loop.settings, "research_loop_inspect_pages", 3, raising=False)      # one initial read, two follow-ups reserved
    cards = WEB2 + "\n[3] [Nova docs](https://docs.nova.example) · 2026-09-01\n[4] [Kaizen docs](https://docs.kaizen.example) · 2026-09-01"

    async def fake(program, **kw):
        fields = set(kw)
        if "catalog" in fields and "request" in fields:
            return SimpleNamespace(subject="Venice", question="q", constraints="own token as collateral", required_facts="named projects", capabilities="web_discovery", queries="web_discovery: q1")
        if "calls_made" in fields:
            return SimpleNamespace(missing="none", next_call="stop", reason="test")
        if "evidence" in fields and "answer" not in fields:
            return SimpleNamespace(candidates="Aave | https://docs.aave.com\nNova | https://docs.nova.example\nKaizen | https://docs.kaizen.example")
        if "page" in fields:
            return SimpleNamespace(verdict="related_but_different", conditions_met="none", conditions_failed="own token as collateral", quote="aTokens")
        if "answer" in fields:
            return SimpleNamespace(supported="Nova, Kaizen", related_but_different="Aave", not_established="none")      # the snippets convinced it
        return SimpleNamespace()

    async def synth(request, cards, trajectory, advice=False, research=False):
        return f"**Taken together**\n\nNova and Kaizen are matches; Aave is a receipt design, not a match.\n\n---\n\n{cards}"

    loop.setattr(evidence_pipeline.composition, "synthesize", synth)
    loop.setattr(research_loop.composition, "synthesize", synth)

    class Router(FakeRouter):
        replay = False
    loop.setattr(runtime, "_call_research_lm", fake)
    loop.setattr(runtime, "_call_research_loop_lm", fake)
    router = Router({"perplexity_web_search": cards})
    router.plan_across = lambda request, caps, chains, n: []
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "which other projects follow lock collateral → mint a tradable second token", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    verdicts = {v["name"]: v for v in out["trajectory"]["research_loop"]["verdicts"]}
    assert reads == ["https://docs.aave.com"]
    assert verdicts["Nova"]["provenance"] == "uninspected" and verdicts["Nova"]["verdict"] == "not_established"
    assert verdicts["Kaizen"]["provenance"] == "uninspected"
    assert "I withheld the written summary" in out["answer"]                 # Nova and Kaizen were presented as matches without a page verdict
    assert "Nova (https://docs.nova.example; not inspected): not established by its page (not inspected: page budget reached" in out["answer"]
