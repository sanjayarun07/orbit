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
    assert '"BONK has about $9.3 million of liquidity per Birdeye." -- figures observed' in text
    assert '"Most of it sits on Orca." -- interpretation' in text
    assert '"The largest pool holds $12.0M." -- no card carries exactly $12.0M' in text
    assert "no previous answer" in answer_audit.audit(None)
    ctx = experience.advance_session_context({}, "liquidity of BONK", None, "research", [], [], None, answer=previous)
    assert ctx["last_answer"] == previous


def test_a_conditional_instruction_is_not_a_second_ask():
    from app import composition
    clauses, note = composition.plan_asks("Top 10 holders of MUSEBOOK on Robinhood Chain. If several contracts share the ticker, tell me which contract you selected.")
    assert clauses == ["Top 10 holders of MUSEBOOK on Robinhood Chain"] and "tell me which contract you selected" in note
    assert not composition.is_format_clause("If BONK drops 10%, tell me when it happens")
    assert composition.pipeline_of([{"answer": "a", "pipeline": "contract", "contract": {"kind": "holders"}}, {"answer": "b", "pipeline": "contract"}]) == {"pipeline": "contract", "contract": {"kind": "holders"}}
    assert composition.pipeline_of([{"answer": "a", "pipeline": "contract"}, {"answer": "b"}]) == {}
