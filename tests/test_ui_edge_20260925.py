"""Regressions from the live mobile UI edge-case run of 2026-09-25
(reports/ui-edge-2026-09-25/report.md): subject-and-scope corrections,
user-state questions, exact-state figures, wallet typing, quote fields."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import address_roles, context_entities, exit_controls, experience, product_actions
from app.contracts import plan_by_rules
from app.routing.subject_probe import _REFERENT, continues_subject


# --- corrections keep the subject and change the metric or the window --------

def test_a_metric_correction_keeps_the_subject():
    ctx = {"focus": {"kind": "token", "label": "BONK", "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "chain": "solana"},
           "last_request": "$BONK holders? Keep the answer to five rows and cite the source time."}
    resolved = context_entities.resolve_contextual_request("Actually, I meant liquidity, not holders. Show the pool and chain.", "", ctx)
    assert resolved.startswith("liquidity of BONK DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 on solana. Show the pool and chain")
    contract = plan_by_rules(resolved)
    assert contract.subject.symbol == "BONK" and contract.metric == "liquidity" and contract.kind == "market_ranking"
    assert context_entities.corrected_request("I meant liquidity, not holders.", "user: $BONK holders?\nassistant: ...", ctx) is None


def test_a_window_change_keeps_the_chain_the_kind_and_the_metric():
    ctx = {"focus": None, "last_contract": {"kind": "market_ranking", "subject": {"kind": "chain", "name": "base", "chain": "base"}, "venue": None,
                                            "scope": "venue_trades", "metric": "price_change", "window_hours": 24.0, "direction": "gainers", "filters": {}}}
    resolved = context_entities.resolve_contextual_request("Now make it seven days, keeping the same Base DEX scope", "", ctx)
    assert resolved.startswith("gainers on base: Now make it seven days")
    contract = plan_by_rules(resolved)
    assert (contract.kind, contract.subject.chain, contract.metric, contract.window_hours) == ("market_ranking", "base", "price_change", 168.0)


def test_a_venue_correction_still_rewrites_the_ask():
    ctx = {"focus": None, "last_contract": {"kind": "market_ranking", "subject": {"kind": "venue", "name": "hyperliquid"}, "venue": "hyperliquid",
                                            "scope": "venue_trades", "metric": "price_change", "window_hours": 24.0, "direction": "gainers", "filters": {"crypto_only": True}}}
    resolved = context_entities.resolve_contextual_request("I meant the past hour, not 24 hours.", "", ctx)
    assert resolved.startswith("gainers on hyperliquid (crypto only): I meant the past hour")
    assert plan_by_rules(resolved).window_hours == 1.0


# --- "which of those" points at the previous answer's items -----------------

def test_the_previous_answers_items_are_the_referent():
    answer = "**Taken together**\n\nThree things happened:\n1. **Solana** validators approved SIMD-0326 on 2026-09-23 [1]\n2. Jupiter launched perps v3 on 2026-09-24 [2]\n- time: plan 2s\n\n---\n\n# From the web\n- [3] something else"
    items = context_entities.answer_items(answer)
    assert items == ["Solana validators approved SIMD-0326 on 2026-09-23", "Jupiter launched perps v3 on 2026-09-24"]
    ctx = experience.advance_session_context({}, "What happened to Solana in the last 24 hours?", None, "research", [], [], None, answer=answer)
    assert ctx["last_items"] == items
    resolved = context_entities.resolve_contextual_request("Which of those items happened yesterday versus was only published yesterday?", "", ctx)
    assert '"those" are the items the previous answer listed -- (1) Solana validators' in resolved and "never substitute a different set" in resolved


def test_withheld_news_items_remain_the_referent_not_the_new_search_results():
    answer = ("I withheld the written summary: it stated figures no source card carries.\n\n---\n\n"
              "# From the web (dated, with sources)\n**Provider**: Perplexity\n\n"
              "1. Federal Reserve proposed new stablecoin rules\nPublication: September 25\n\n"
              "2. Senate vote on the CLARITY Act happened September 15\nPublication: September 16\n\n"
              "Sources:\n[1] A story\n[2] Another story")
    items = context_entities.answer_items(answer)
    assert items == ["Federal Reserve proposed new stablecoin rules", "Senate vote on the CLARITY Act happened September 15"]
    ctx = experience.advance_session_context({}, "What is the newest crypto market news?", None, "research", [], [], None, answer=answer)
    resolved = context_entities.resolve_contextual_request("Which of those events happened in the last 24 hours?", "", ctx)
    assert "Federal Reserve proposed new stablecoin rules" in resolved
    assert "Senate vote on the CLARITY Act happened September 15" in resolved
    assert "A story" not in resolved


def test_prose_summary_still_carries_numbered_items_from_the_visible_web_card():
    answer = ("**Taken together**\n\nThe newest news covers the Fed and a Senate vote.\n\n---\n\n"
              "# From the web (dated, with sources)\n**Provider**: Perplexity\n\n"
              "1. Federal Reserve proposed new stablecoin rules\nPublication: September 25\n\n"
              "2. Senate vote on the CLARITY Act happened September 15\nPublication: September 16\n\n"
              "Sources:\n[1] First source\n[2] Second source")
    ctx = experience.advance_session_context({}, "What is the newest crypto market news?", None, "research", [], [], None, answer=answer)
    assert ctx["last_items"] == ["Federal Reserve proposed new stablecoin rules", "Senate vote on the CLARITY Act happened September 15"]
    resolved = context_entities.resolve_contextual_request("Which of those events actually happened in the last 24 hours?", "", ctx)
    assert resolved.startswith("Which of those events actually happened in the last 24 hours?\nResolved from conversation context:")
    from app.composition import plan_asks
    assert len(plan_asks(resolved)[0]) == 1
    from app.evidence_pipeline import research_query
    query = research_query(resolved)
    assert "Federal Reserve proposed new stablecoin rules" in query
    assert "Senate vote on the CLARITY Act happened September 15" in query
    assert "UTC" in query and "exact previous 24 hours" in query


def test_inline_numbered_summary_uses_the_full_visible_card_list():
    answer = ("1. First event happened. 2. Second event happened. 3. Third event happened.\n\n---\n\n"
              "# From the web\n1. First event happened\n2. Second event happened\n3. Third event happened\nSources:\n[1] Source")
    assert context_entities.answer_items(answer) == ["First event happened", "Second event happened", "Third event happened"]


def test_written_list_remains_the_referent_when_card_lists_more_leads():
    answer = ("1. First verified event\n2. Second verified event\n\n---\n\n"
              "# From the web\n1. First verified event\n2. Second verified event\n3. Third discovery lead\nSources:\n[1] Source")
    assert context_entities.answer_items(answer) == ["First verified event", "Second verified event"]


def test_prior_item_amount_keeps_its_decimal_in_the_followup():
    ctx = {"last_items": ["ETF inflows reached $351.6M on September 24", "Bitget reported a breach"]}
    resolved = context_entities.resolve_contextual_request("Which of those happened today?", "", ctx)
    assert "$351.6M" in resolved
    from app.composition import plan_asks
    assert len(plan_asks(resolved)[0]) == 1
    from app.evidence_pipeline import research_query
    assert "$351.6M" in research_query(resolved)


def test_news_referent_does_not_route_a_whale_headline_to_holders():
    ctx = {"last_items": ["A whale address moved 4,500 BTC after four years of inactivity"]}
    resolved = context_entities.resolve_contextual_request("Which of those happened in the last 24 hours?", "", ctx)
    assert plan_by_rules(resolved).kind == "open_research"


def test_what_do_you_actually_know_points_back():
    assert _REFERENT.match("Do not substitute a token on Base. What do you actually know?") is None     # not at the start
    assert continues_subject("What do you actually know?")
    assert _REFERENT.match("What do you actually know?")


# --- exact-state figures come from state cards, never a discovery card -------

def test_holder_figures_from_a_web_card_are_unsupported(monkeypatch):
    from tests.test_contract_pipeline import FakeRouter
    from app import evidence_pipeline
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: False)

    async def synth(request, cards, trajectory, advice=False, research=False):
        return f"**Taken together**\n\nThe largest holder holds 49.23% and the top ten 67.13%.\n\n---\n\n{cards}"

    monkeypatch.setattr(evidence_pipeline.composition, "synthesize", synth)
    web = "# From the web (dated, with sources)\n**Query**: q\n\nA tracker page says the largest holder holds 49.23% and the top ten 67.13%. [1]\n\nSources:\n[1] [tracker](https://x) · 2026-09-24"
    router = FakeRouter({"perplexity_web_search": web})
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "Top 10 holders of MUSEBOOK on Robinhood Chain", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert out is not None and "49.23%" not in (out.get("answer") or "").split("\n---\n")[0]


# --- an address the request types as a wallet is a wallet ------------------

def test_the_requests_own_wording_types_an_address():
    roles = address_roles.roles_in_request("What does public wallet 3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS hold now? Treat it as a wallet, not the ANSEM mint.")
    assert roles == {"3ahlqhsvw3gpxnq1fveyg6p3pccxkgo3etskqge4kzks": "wallet"}
    assert address_roles.roles_in_request("holders of token DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263") == {"dezxaz8z7pnrnrjjz3wxborgixca6xjnb7yab1ppb263": "token"}
    token = address_roles.bind_turn({"focus": {"kind": "token", "address": "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS"}}, "Treat 3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS as a wallet, not a token")
    try:
        assert address_roles.role_of("3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS") == "wallet"
        assert "is a wallet in this conversation" in address_roles.not_a_token("3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS")
    finally:
        address_roles.end_turn(token)


def test_the_token_overview_refuses_a_wallet_typed_address(monkeypatch):
    from app import market_providers
    provider = next((p for p in [market_providers.BirdeyeProvider()] if hasattr(p, "token_overview")), None) if hasattr(market_providers, "BirdeyeProvider") else None
    if provider is None:
        pytest.skip("Birdeye provider class not importable here")
    token = address_roles.bind_turn({}, "public wallet 3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS")
    try:
        with pytest.raises(ValueError, match="is a wallet in this conversation"):
            provider.token_overview("token overview 3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS on solana")
    finally:
        address_roles.end_turn(token)


# --- the user's own watches, and what a deterioration alert means ----------

def test_is_there_an_exit_watch_reads_the_users_watches(monkeypatch):
    async def none(user_id):
        return []
    monkeypatch.setattr(exit_controls.exit_monitor, "list_for", none)
    assert exit_controls.is_exit_control("Is there already an ANSEM exit watch? If yes, show its threshold and last quote time.")
    answer = asyncio.run(exit_controls.handle("Is there already an ANSEM exit watch? If yes, show its threshold and last quote time.", {"id": "u"}, None))
    assert answer.startswith("No, there is no active exit watch on ANSEM")

    async def one(user_id):
        return [{"status": "active", "symbol": "ANSEM", "mint": "9cRC", "wallet": "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS", "created_at": "2026-09-24T10:00:00", "rules": {}, "entry": {}}]
    monkeypatch.setattr(exit_controls.exit_monitor, "list_for", one)
    monkeypatch.setattr(exit_controls, "_rules_text", lambda rules: "")
    answer = asyncio.run(exit_controls.handle("do I have an exit watch on ANSEM?", {"id": "u"}, None))
    assert answer.startswith("Yes -- active exit watch") and "ANSEM" in answer


def test_a_deterioration_alert_is_explained_on_proceeds():
    q = "What would a 20 percent deterioration alert mean for a $2 position? Do not create another watch."
    assert product_actions.is_product_question(q)
    answer = product_actions.answer(q)
    assert "quoted exit proceeds" in answer and "$1.60" in answer and "not the token price" in answer and "Nothing was created" in answer


# --- limits, ambiguity, the simulation card -------------------------------

def test_number_words_are_limits():
    assert plan_by_rules("$BONK holders? Keep the answer to five rows and cite the source time.").limit == 5
    assert plan_by_rules("top 3 holders of BONK").limit == 3


def test_a_bare_binance_venue_is_a_question():
    contract = plan_by_rules("Meme tokens on Binance are moving.")
    assert contract.ambiguity and "Binance-listed spot" in contract.ambiguity and "BNB Chain" in contract.ambiguity
    assert plan_by_rules("Binance-listed spot tokens moving today").ambiguity is None
    assert plan_by_rules("top movers on Aster").ambiguity is None


def test_the_simulation_card_carries_minimum_out_route_and_time(monkeypatch):
    from app.nodes import portfolio
    quote = {"otherAmountThreshold": "5620000", "slippageBps": 50, "routePlan": [{"swapInfo": {"label": "Orca"}}, {"swapInfo": {"label": "Raydium"}}]}
    class Info(SimpleNamespace):
        def model_dump(self):
            return dict(self.__dict__)
    sim = {"input_token": Info(symbol="SOL", decimals=9), "output_token": Info(symbol="USDC", decimals=6), "input_amount": 0.05,
           "input_value_usd": 5.67, "output_amount": 5.65, "output_value_usd": 5.65, "price_impact_pct": 0.0, "quote": quote}

    async def fake_sim(a, b, c):
        return sim
    monkeypatch.setattr(portfolio, "simulate_swap", fake_sim)
    monkeypatch.setattr(portfolio, "complete_swap_fields", lambda *a, **k: ("So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 50_000_000, None))
    state = {"request": "Price-check selling 0.05 SOL into USDC at 50 bps on Solana. Show quoted proceeds, minimum out, route, and quote time; do not place a trade.",
             "contextual_request": None, "capabilities": ["trade_simulation"], "chains": [], "session_context": {}, "history": "", "wallet_address": ""}
    out = asyncio.run(portfolio._portfolio_node(state))
    answer = out["answer"]
    assert "**Minimum out**: 5.62 USDC at 50 bps slippage" in answer and "**Route**: Orca → Raydium" in answer and "**Quote time**: 2026-" in answer
    assert "simulation only" in answer


# --- the live replay of 2026-09-25 (reports/ui-edge-2026-09-25/rerun-*) -----

def test_listing_binances_meanings_chooses_none_of_them():
    q = "Meme tokens on Binance are moving. Do you mean Binance-listed spot tokens, BNB Chain tokens, or Aster perps? Ask before ranking if the venue is unclear."
    contract = plan_by_rules(q)
    assert contract.ambiguity and contract.direction == "gainers" and contract.venue == "binance"
    assert plan_by_rules("top gainers on BNB Chain via Binance").subject.chain == "bsc"
    assert plan_by_rules("Binance perps movers").filters.get("perps") and plan_by_rules("Binance perps movers").ambiguity is None


def test_the_reply_to_the_binance_question_keeps_the_ranking_and_rules_out_bnb_chain():
    ctx = {"focus": None, "last_contract": {"kind": "market_ranking", "subject": {"kind": "venue", "name": "binance"}, "venue": "binance",
                                            "scope": "venue_trades", "metric": "price_change", "window_hours": 24.0, "direction": "gainers", "filters": {}}}
    resolved = context_entities.resolve_contextual_request("I mean Binance-listed spot tokens, not BNB Chain. What can you verify now?", "", ctx)
    assert resolved.startswith("gainers on binance: I mean Binance-listed spot tokens")
    contract = plan_by_rules(resolved)
    assert (contract.kind, contract.venue, contract.subject.chain, contract.scope, contract.ambiguity) == ("market_ranking", "binance", None, "venue_trades", None)
    assert contract.filters.get("exclude") == ["bsc"]


def test_the_gainers_tool_never_reads_a_ruled_out_chain(monkeypatch):
    from app import additional_providers
    seen = {}

    class Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            seen.update(params or {})
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [])
    monkeypatch.setattr(additional_providers.httpx, "Client", Client)
    provider = additional_providers.CoinGeckoProvider.__new__(additional_providers.CoinGeckoProvider)
    out = provider.gainers_losers("gainers on binance: I mean Binance-listed spot tokens, not BNB Chain. What can you verify now?")
    assert "category" not in seen and "market-wide" in out


def test_venue_only_trades_are_never_the_global_ranking():
    q = "Top gainers on Base in the last 24h. Use only trades executed on Base, not global prices of Base-ecosystem tokens."
    assert plan_by_rules(q).scope == "venue_trades"
    assert plan_by_rules("Base-ecosystem tokens up the most in the last 24h (global prices)").scope == "global"


def test_a_trailing_referent_question_is_the_ask():
    from app.routing.subject_probe import ask_sentences, is_referent
    q = "Do not substitute a token on Base or an app-listed Robinhood stock. What do you actually know?"
    assert ask_sentences(q) == ["What do you actually know?"] and is_referent(q)
    assert not is_referent("Top gainers on Base. What changed?")
    contract = plan_by_rules(q + "\nResolved from canonical session context: token Token mint 0x91A2DAe9699f0B82540B5886b0d8759C22820bA3 on robinhood.")
    assert contract.kind == "open_research" and contract.subject.id == "0x91A2DAe9699f0B82540B5886b0d8759C22820bA3" and contract.subject.chain == "robinhood"
    assert contract.subject.name is None and not contract.filters.get("stocks_only")


def test_the_previous_answer_is_audited_from_the_session_not_the_web():
    from app import answer_audit
    from app.routing import resolver
    q = "For the previous answer, which fact was observed live and which part was an inference?"
    assert answer_audit.is_provenance_ask(q) and not answer_audit.is_provenance_ask("Which part was in the SEC order and which is your inference?")
    route = asyncio.run(resolver.resolve({"request": q, "session_context": {}, "history": ""}, None))
    assert route["intent"] == "general" and route["routing_decision"]["reason"] == "answer_audit"
    previous = ("**Taken together**\n\nBONK has about $9.3 million of liquidity per Birdeye. Most of it sits on Orca. The largest pool holds $12.0M.\n\n---\n\n"
                "# Bonk (Bonk)\n**Provider**: Birdeye · **Data freshness**: 2026-09-25 06:02 UTC\n| Metric | Value |\n|---|---|\n| Liquidity | $9.3M |\n")
    text = answer_audit.audit(previous)
    assert "- **Bonk (Bonk)** -- Provider: Birdeye · Data freshness: 2026-09-25 06:02 UTC" in text
    assert '"BONK has about $9.3 million of liquidity per Birdeye." -- the figure appears in a card, but this audit has not verified' in text
    assert '"Most of it sits on Orca." -- interpretation or paraphrase' in text
    assert '"The largest pool holds $12.0M." -- no card carries exactly $12.0M' in text
    assert "no previous answer" in answer_audit.audit(None)
    ctx = experience.advance_session_context({}, "liquidity of BONK", None, "research", [], [], None, answer=previous)
    assert ctx["last_answer"] == previous


def test_a_matching_number_cannot_verify_a_different_metric():
    from app.answer_audit import audit
    previous = ("**Taken together**\n\nThe largest BONK pool holds $9.7M.\n\n---\n\n"
                "# BONK overview\n**Provider**: Birdeye\n| Metric | Value |\n|---|---|\n| Overall liquidity | $9.7M |")
    result = audit(previous)
    assert "the figure appears in a card, but this audit has not verified that the source supports this specific claim" in result
    assert "direct source passage reproduced" not in result


def test_a_conditional_instruction_is_not_a_second_ask():
    from app import composition
    clauses, note = composition.plan_asks("Top 10 holders of MUSEBOOK on Robinhood Chain. If several contracts share the ticker, tell me which contract you selected.")
    assert clauses == ["Top 10 holders of MUSEBOOK on Robinhood Chain"] and "tell me which contract you selected" in note
    assert not composition.is_format_clause("If BONK drops 10%, tell me when it happens")
    clauses, note = composition.plan_asks("Give three crypto headlines from today. Number them; for each distinguish publication time from event time.")
    assert len(clauses) == 1
    assert "Number them" in note
    assert composition.pipeline_of([{"answer": "a", "pipeline": "contract", "contract": {"kind": "holders"}}, {"answer": "b", "pipeline": "contract"}]) == {"pipeline": "contract", "contract": {"kind": "holders"}}
    assert composition.pipeline_of([{"answer": "a", "pipeline": "research_loop", "contract": {"kind": "open_research"}}, {"answer": "b", "pipeline": "contract"}]) == {"pipeline": "research_loop", "contract": {"kind": "open_research"}}
    assert composition.pipeline_of([{"answer": "a", "pipeline": "contract"}, {"answer": "b"}]) == {}


# --- the trust re-run on 2807c358 (reports/trust-rerun-2807c358) --------------

def test_a_topic_follow_up_is_never_asked_which_token(monkeypatch):
    from tests.test_routing_rules_20260918 import _security_state, _stub_downstream
    from app.nodes import research as research_mod
    seen = {}
    _stub_downstream(monkeypatch, seen)
    monkeypatch.setattr(research_mod.settings, "contract_pipeline_enabled", True)
    calls = []

    async def piped(state, request, chains, context=""):
        calls.append(request)
        return {"answer": "**Taken together**\n\nfunding rounds from the web", "trajectory": None, "pipeline": "contract"} if "funding" in request else None
    monkeypatch.setattr(research_mod.evidence_pipeline, "answer", piped)
    note = "\nResolved from conversation context: this continues the discussion about EigenLayer (the subject of the previous turns: a protocol, company or topic, not a token symbol to look up)."
    focus = {"kind": "topic", "label": "EigenLayer"}
    out = asyncio.run(research_mod.research_node(_security_state("List funding rounds by date, amount, instrument, investors and primary source. Mark database-only claims." + note, session_context={"focus": focus})))
    assert "funding rounds from the web" in out["answer"]
    out = asyncio.run(research_mod.research_node(_security_state("Separate deposits or TVL, fees paid, protocol revenue, incentives and value accruing to token holders." + note, session_context={"focus": focus})))
    assert "Which token should I check" not in out["answer"]                    # the pipeline did not plan it; the legacy path reads the note
    out = asyncio.run(research_mod.research_node(_security_state("is it audited?")))
    assert "Which token should I check" in out["answer"]                        # nothing in the conversation: still asked


def test_a_headline_tap_is_research_on_the_story_not_its_words():
    contract = plan_by_rules("What does this mean for the market: S&P 500 falls 0.75% as yields hit multi-decade highs")
    assert contract.kind == "open_research" and contract.subject.name == "S&P 500 falls 0.75% as yields hit multi-decade highs"
    assert plan_by_rules("What does this mean for memecoins: Solana gainers lead as BONK rallies").kind == "open_research"
    assert plan_by_rules("yields on base for USDC").kind == "yields"


# --- the expanded journeys x3 on 2807c358 (reports/journeys-expanded-2807c358) ---

def test_a_generic_capitalised_word_is_not_a_subject():
    from app.routing.subject_probe import subject_of
    assert subject_of("Anything breaking for meme traders in the past 24h?") is None
    assert subject_of("Something is off with BONK") == "BONK"


def test_an_exact_state_ask_in_question_form_is_not_planned_before_its_ticker_resolves():
    from app import contracts
    from app.nodes import research
    q = "Back to BONK: what percent do the top 10 hold, and what counts in that denominator?"
    assert research._DATA_ASK.search(q) and contracts.plan_by_rules(q).kind == "holders"
    assert contracts.plan_by_rules("Research projects with two tokens like Venice VVV and DIEM").kind == "open_research"


def test_a_hyphenated_minute_span_is_not_an_unsupported_figure():
    from app import fact_gate
    assert fact_gate.unsupported_figures("Over a 48-minute span from 07:04 to 07:52 UTC, NIL rose 8.07%.", [], evidence_text="| NIL/USDC | +8.07% |") == []


def test_a_wallet_bound_holdings_follow_up_is_a_portfolio_read():
    from app import contracts
    q = "Which assets does it hold?\nResolved from canonical session context: wallet 3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS on solana."
    c = contracts.plan_by_rules(q)
    assert (c.kind, c.subject.kind, c.subject.id, c.subject.chain) == ("portfolio", "wallet", "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS", "solana")
    assert contracts.plan_by_rules("Which assets does it hold?").kind != "portfolio"                       # nothing bound: not a wallet read
    assert contracts.plan_by_rules("Who holds the most?\nResolved from canonical session context: wallet 3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS on solana.").kind != "portfolio"


def test_an_epoch_trade_time_from_mobula_is_a_stamp_not_a_crash(monkeypatch):
    import importlib.util
    from app import evidence
    assert evidence.tx("0xabc", "mobula", "solana", at=1790325099906).at == "1790325099906"
    # conftest replaces app.mobula_meme.token_trades with an offline stub; a fresh load of the module keeps the real function.
    spec = importlib.util.spec_from_file_location("mobula_meme_fresh", "app/mobula_meme.py")
    fresh = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = fresh                          # dataclasses resolve the defining module through sys.modules
    spec.loader.exec_module(fresh)
    row = {"transactionHash": "5xyz", "date": 1790325099906, "type": "buy", "baseTokenAmountUSD": 12.5, "platform": "Raydium",
           "tokenAmount": 100, "tokenPrice": 0.125, "maker": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"}
    monkeypatch.setattr(fresh, "_get", lambda path, params: [row])
    monkeypatch.setattr(fresh, "_subject", lambda request: ("9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", "solana"))
    out = fresh.token_trades("recent trades for 9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump on solana")
    assert "2026-09-25" in out and "buy" in out.lower()


def test_an_events_answer_may_quote_figures_from_its_web_card(monkeypatch):
    from tests.test_contract_pipeline import FakeRouter
    from app import evidence_pipeline
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: False)

    async def synth(request, cards, trajectory, advice=False, research=False):
        return f"**Taken together**\n\nOn 2026-09-24 spot Solana ETFs recorded $13.77 million in net inflows and SOL open interest reached $6.94B, up 8.54%. [1]\n\n---\n\n{cards}"

    monkeypatch.setattr(evidence_pipeline.composition, "synthesize", synth)
    web = ("# From the web (dated, with sources)\n**Query**: q\n\nOn September 24, 2026 U.S. spot Solana ETF products recorded $13.77 million in net inflows; "
           "SOL futures open interest reached $6.94B, up 8.54% over seven days. [1]\n\nSources:\n[1] [report](https://x) · 2026-09-24")
    router = FakeRouter({"perplexity_web_search": web})
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, "What happened to Solana in the last 24 hours? Give event times, not just article update times.", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert out is not None and out["contract"]["kind"] == "recent_events"
    assert "withheld" not in (out.get("answer") or "") and "$13.77 million" in (out.get("answer") or "").split("\n---\n")[0]
