"""The three answer-quality gaps closed on the 5b85ec5a candidate (2026-09-28):
concentration figures keep their source and basis in the deep dive, "new
today" is judged by event date with recaps labelled, and a bare short ticker
on a lesser coin asks which asset is meant."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app import contracts, equities_data, fact_gate, tequity, token_deepdive, why_moving

EVIDENCE = ("# Token holders\n**Top 10 positions hold 38.37% of supply** (of the 50 largest positions indexed, pools, exchanges and burn addresses included)\n\n"
            "# Rug risk\n## Holders\n- **Top 10**: 30.92% of supply (Mobula's security profile, with its own exclusions; the holders table counts every position) · **Top 50**: 17.47% · **Top 100**: 22.10%\n")


def test_a_merged_or_unsourced_concentration_claim_is_removed_and_the_sourced_figures_stated():
    answer = ("Bonk is a verified SPL token.\n"
              "| Holder Structure | Top 10 hold 38.37%, top 50 hold 17.47% | mobula_token_holders | Elevated |\n"
              "- Top 10 holders hold approx. 38.37% (Mobula index), or 30.92% (Jupiter audit); elevated but not extreme concentration.\n"
              "- Top 50 hold ~50%+ combined.\n"
              "Liquidity is pullable.")
    assert token_deepdive.concentration_conflicts(answer) == ["| Holder Structure | Top 10 hold 38.37%, top 50 hold 17.47% | mobula_token_holders | Elevated |"]
    audited = token_deepdive.audit_concentration(answer, EVIDENCE)
    assert "top 50 hold 17.47%" not in audited and "Top 50 hold ~50%+" not in audited            # merged bases, and a figure no card carries
    assert "Top 10 holders hold approx. 38.37% (Mobula index), or 30.92% (Jupiter audit)" in audited   # both figures are the cards', with their sources
    assert "Bonk is a verified SPL token." in audited and "Liquidity is pullable." in audited
    assert "**Holder concentration, as the sources state it**" in audited
    assert "Top 10 positions hold 38.37% of supply (of the 50 largest positions indexed" in audited and "Top 50: 17.47%" in audited
    assert "removed from the written analysis: 2 statements" in audited
    assert token_deepdive.audit_concentration("No concentration figure here.", EVIDENCE) == "No concentration figure here."
    assert "never merge figures from two sources" in token_deepdive.ANALYSIS_RULES


def test_older_events_told_as_new_are_recaps_by_event_date():
    now = datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc)      # a Saturday
    answer = ("**Taken together**\n\nIn the last 24 hours on 2026-09-27, Bitcoin continued a strong rally with an intraday peak of $87,265 on Wednesday [1]. "
              "The biggest security event was a major hack on Bitget on Thursday, with losses of $387.5 million [1]. "
              "Bitcoin ETFs saw $2.4 billion in weekly net inflows through 25 September [8]. "
              "Recap: the SEC postponed its ETF options decision on September 24 [8]. "
              "CoinDesk reported Vitalik Buterin's Ethereum 2030 vision on September 27 [2].\n\n---\n\n# From the web\n[1] x · 2026-09-27")
    found = fact_gate.recap_sentences(answer, 24, now)
    assert [f.split(":")[0] for f in found] == ["2026-09-23", "2026-09-24", "2026-09-25"]
    assert all("Vitalik" not in f and "Recap" not in f for f in found)
    assert fact_gate.recap_sentences(answer, 24 * 30, now) == []            # a month's window: nothing is a recap
    plan = contracts.plan_by_rules("What's actually new in crypto today?")
    assert plan.kind == "recent_events" and plan.window_hours == 24
    gate = fact_gate.check(plan, [], answer, scope_satisfied=True, now=now)
    assert len(gate.recap) == 3
    assert fact_gate.prose_event_dates("it happened on Sunday", now)[0].date() == now.date()          # 2026-09-27 is a Sunday: today
    assert fact_gate.prose_event_dates("it happened on Saturday", now)[0].date() == datetime(2026, 9, 26, tzinfo=timezone.utc).date()


def test_a_bare_short_ticker_on_a_lesser_coin_asks_which_asset(monkeypatch):
    from app import symbol_registry
    monkeypatch.setattr(symbol_registry, "listed", lambda sym: [{"name": "Backpack", "symbol": "BP", "rank": 135}])
    monkeypatch.setattr(symbol_registry, "leader", lambda rows: rows[0])
    monkeypatch.setattr(equities_data, "enabled", lambda: True)
    monkeypatch.setattr(equities_data, "is_listed_ticker", lambda sym: False)      # the gateway's allowlist cannot say BP plc does not exist
    monkeypatch.setattr(tequity, "resolve_pair", lambda venue, sym: None)
    ask, hedge = asyncio.run(why_moving.namesake("BP", "Why is BP moving today?"))
    assert ask.startswith("**BP** can mean a listed company (ticker BP) or the token **Backpack**") and hedge is None
    assert asyncio.run(why_moving.namesake("BP", "why is BP token moving")) == (None, None)
    monkeypatch.setattr(symbol_registry, "listed", lambda sym: [{"name": "Ansem", "symbol": "ANSEM", "rank": 1400}])
    ask, hedge = asyncio.run(why_moving.namesake("ANSEM", "Why is ANSEM moving?"))
    assert ask is None and hedge.startswith("_ANSEM here is the token **Ansem**")        # five letters, no stock signal: the coin, under a lead line


def test_the_recap_check_reads_the_summary_never_the_cards_or_source_lists():
    now = datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc)
    answer = ("**Taken together**\n\nBitget resumed withdrawals on September 27 [1].\n[8] Cryptocurrency News · 2026-09-24\n\n---\n\n"
              "# From the web\nOn Wednesday Bitcoin peaked [1].\n\nSources:\n[1] [x](https://x) · 2026-09-23\n[9] Top News · 2024-12-09")
    assert fact_gate.recap_sentences(answer, 24, now) == []


def test_a_rounded_share_or_a_threshold_stands_and_only_the_offending_sentence_goes():
    answer = ("Bonk has a large holder base, but the top 10 holders control around 31–38% of supply, indicating elevated concentration (top 10 >30%). "
              "Top 50 hold ~50%+ combined. Liquidity is pullable.")
    audited = token_deepdive.audit_concentration(answer, EVIDENCE)
    assert "top 10 holders control around 31–38% of supply" in audited and "Liquidity is pullable." in audited
    assert "Top 50 hold ~50%+" not in audited and "removed from the written analysis: 1 statement " in audited


# --- browser UAT of 260faaf7 (2026-09-28) ---

def test_an_exit_watch_after_a_named_wallet_exit_analysis_reads_that_wallet(monkeypatch):
    from app import exit_controls
    seen = {}

    async def resolve_token(token):
        return "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", "ANSEM", None

    async def position_of(wallet, mint):
        seen["wallet"] = wallet
        return None
    monkeypatch.setattr(exit_controls, "resolve_token", resolve_token)
    monkeypatch.setattr(exit_controls.exit_monitor, "position_of", position_of)
    focus = {"kind": "wallet", "label": "Wallet", "address": "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS", "chain": "solana"}
    reply = asyncio.run(exit_controls.handle("Watch my exit on ANSEM.", {"id": "u1"}, None, focus=focus))
    assert seen["wallet"] == focus["address"] and not reply.startswith("Connect a Solana wallet first")
    reply = asyncio.run(exit_controls.handle("Watch my exit on ANSEM.", {"id": "u1"}, None, focus={"kind": "token", "address": "x"}))
    assert reply.startswith("Connect a Solana wallet first")


def test_the_deployer_answer_names_the_contracts_symbol_not_the_placeholder(monkeypatch):
    from tests.test_routing_rules_20260918 import _security_state, _stub_downstream
    from app.nodes import research as research_mod
    _stub_downstream(monkeypatch, {})
    seen = {}

    async def fake(mint, chain, symbol=None):
        seen["symbol"] = symbol
        return "ok", {"tool_name_0": "mobula_token_holders"}
    monkeypatch.setattr(research_mod.deployer_check, "answer", fake)
    mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    ctx = {"focus": {"kind": "token", "label": "TOKEN", "address": mint, "chain": "solana"},
           "last_contract": {"kind": "holders", "subject": {"kind": "token", "id": mint, "symbol": "BONK", "chain": "solana"}}}
    asyncio.run(research_mod.research_node(_security_state("Is the largest account the deployer or funded by it?", session_context=ctx)))
    assert seen["symbol"] == "BONK"


def test_quoted_material_in_a_note_is_never_read_by_a_tool_matcher():
    from app import mobula_wallet, provider_router
    request = ('What is the live price versus the reported catalyst?\nResolved from canonical session context: token SOL mint So11111111111111111111111111111111111111112 on solana.\n'
               'Resolved from conversation context: "the reported catalyst" is what the previous answer reported: «SOL moved higher as traders sold the news; wallet activity and '
               'transactions in this wallet address show whales bought $86.67 million in one session.»')
    assert mobula_wallet.matches(request)                                   # the excerpt alone would make it a wallet ask
    assert not mobula_wallet.matches(provider_router._matchable(request))   # the router never lets a matcher read it
    assert "«" not in provider_router._matchable(request) and "live price" in provider_router._matchable(request)


def test_what_happened_with_nothing_to_point_at_asks_which_story():
    from app import context_entities
    q = "What happened, and what does it mean for the market?"
    assert context_entities.event_question(q, {}).startswith("Which event or headline do you mean?")
    assert context_entities.event_question(q, {"focus": {"kind": "topic", "label": "Fed proposes stablecoin rules", "source": "home_headline"}}) is None
    assert context_entities.event_question(q, {"last_answer": "# Why is SOL moving? ..."}) is None
    assert context_entities.event_question("What happened to Solana in the last 24 hours?", {}) is None      # names its subject
    assert context_entities.event_question("What does this mean for the market: Fed hikes", {}) is None      # a tap carries its headline


def test_a_missing_part_that_the_previous_answer_reported_is_restated_not_called_uncovered(monkeypatch):
    from app import answer_gate
    question = ('What is the live price versus the reported catalyst?\nResolved from canonical session context: token SOL mint So11111111111111111111111111111111111111112 on solana.\n'
                'Resolved from conversation context: "the reported catalyst" is what the previous answer reported; restate it from that answer, never from a new search, and set the live figure against it: «SOL is moving on the Alpenglow upgrade and ETF inflows.»')
    async def check(q, a):
        return {"verdict": "missing", "subject": "SOL", "missing": "the reported catalyst comparison"}
    async def web(q):
        return None
    monkeypatch.setattr(answer_gate, "check", check)
    monkeypatch.setattr(answer_gate, "_web_answer", web)
    monkeypatch.setattr(answer_gate, "eligible", lambda answer, result: True)
    out = asyncio.run(answer_gate.gate(question, {"answer": "# Solana (SOL)\nPrice: $119.39", "trajectory": {"tool_name_0": "mobula_token_details"}}))
    assert "**The reported catalyst, from the previous answer:** SOL is moving on the Alpenglow upgrade and ETF inflows." in out["answer"]
    assert "Not covered by the sources this turn" not in out["answer"] and out["answer_gate"]["resolved_by"] == "prior_answer"
    out = asyncio.run(answer_gate.gate("What is the live price versus funding?", {"answer": "# Solana (SOL)\nPrice: $119.39", "trajectory": {"tool_name_0": "x"}}))
    assert "Not covered by the sources this turn" in out["answer"]


# --- "Analyze the liquidation clusters" after "why crypto market is down today?" is BTC's (live 2026-09-28) ---

def test_the_answers_declared_subject_becomes_the_focus_and_a_subjectless_follow_up_continues_it():
    """The general rules: the focus falls back to the subject the answer
    declares in its own card headings, and a message that names nothing of
    its own continues the subject unless it is a general or product question
    (BTC after "why crypto market is down today?", then "Analyze the
    liquidation clusters", live 2026-09-28)."""
    from app import experience, context_entities
    from app.routing import subject_probe
    market = ("_The market here is the crypto market, led by BTC; say \"stock market\" for equities._\n\n# Crypto Market Overview\n**Retrieved** x\n\n---\n\n"
              "# Why is Bitcoin (BTC) moving?\n**As of** x · crypto\n")
    assert context_entities.answer_subject(market)["label"] == "BTC"
    assert context_entities.answer_subject("# Hyperliquid market data\n**Provider**: GoldRush\n\n## NEAR\n**Mark Price (USD)**: $5")["label"] == "NEAR"
    assert context_entities.answer_subject("# Exit analysis — ANSEM\n**Wallet**: x")["label"] == "ANSEM"
    assert context_entities.answer_subject("# Backpack (BP)\n**Provider**: Birdeye · **Contract**: `9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump` · **Chain**: solana") == {
        "kind": "token", "label": "BP", "symbol": "BP", "address": "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", "chain": "solana", "confidence": 0.7, "source": "answer"}
    assert context_entities.answer_subject("# Crypto market today\n**Checked** x\n\n## Dated reporting\n- x") is None
    ctx = experience.advance_session_context({}, "why crypto market is down today?", None, "research", [], [], None, answer=market)
    assert ctx["focus"]["label"] == "BTC" and ctx["focus"]["source"] == "answer"
    for q in ("Analyze the liquidation clusters", "and open interest?", "Can you also check the funding rate?", "Build bull, base and bear cases"):
        assert subject_probe.continues_subject(q), q
    for q in ("what is a liquidation cluster?", "how do funding rates work?", "Do you support Base?", "Can I see a wallet without giving you control of it?",
              "What does this mean for the market: Nasdaq closes at record", "top gainers on base", "thanks"):
        assert not subject_probe.continues_subject(q), q
    resolved = context_entities.resolve_contextual_request("Analyze the liquidation clusters", "user: why crypto market is down today?\nassistant: ...", ctx)
    assert "this continues the discussion about BTC (the coin the previous answer was about; no contract address)" in resolved
    ctx = experience.advance_session_context(ctx, "Analyze the liquidation clusters", None, "research", [], [], None, answer="**Taken together**\n\nBTC clusters…")
    assert ctx["focus"]["label"] == "BTC"                       # a continuation keeps the focus
    ctx = experience.advance_session_context(ctx, "How's the crypto market today?", None, "research", [], [], None, answer="# Crypto market today\n**Checked** x")
    assert ctx["focus"] is None                                 # a fresh ask whose answer declares no subject clears it


def test_a_derivatives_ask_about_a_coin_carries_the_venues_live_state(monkeypatch):
    from app import evidence_pipeline
    from tests.test_contract_pipeline import FakeRouter
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: False)
    seen = {}

    async def synth(req, cards, trajectory, advice=False, research=False):
        seen["request"] = req
        return f"**Taken together**\n\nA read.\n\n---\n\n{cards}"
    monkeypatch.setattr(evidence_pipeline.composition, "synthesize", synth)
    web = "# From the web (dated, with sources)\n**Query**: q\n\nCoinGlass shows dense long liquidations below $80K. [1]\n\nSources:\n[1] [CoinGlass](https://www.coinglass.com/x) · 2026-09-23"
    venue = "# Hyperliquid market data\n**Provider**: GoldRush · **Checked**: 2026-09-28 10:10 UTC\n\n## BTC\n**Mark Price (USD)**: $82,700\n**Funding Rate**: 0.0010% (per hour)\n**Open Interest**: 12,000.00 BTC\n"
    router = FakeRouter({"perplexity_web_search": web, "goldrush_hyperliquid_market": venue})
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        request = "Analyze the liquidation clusters\nResolved from conversation context: this continues the discussion about BTC (the coin the previous answer was about; no contract address)."
        out = asyncio.run(evidence_pipeline.answer({"session_context": {"focus": {"kind": "token", "label": "BTC", "address": None, "chain": None}}}, request, ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert out is not None and "goldrush_hyperliquid_market" in router.calls
    assert "# Hyperliquid market data" in out["answer"] and "Open Interest" in out["answer"]
    assert "Liquidation levels, clusters and heatmaps have no live source in this product" in seen["request"] and "BTC's live derivatives" in seen["request"]


def test_a_general_derivatives_question_gets_no_venue_card(monkeypatch):
    from app import evidence_pipeline
    from tests.test_contract_pipeline import FakeRouter
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: False)

    async def synth(req, cards, trajectory, advice=False, research=False):
        return f"**Taken together**\n\nA definition.\n\n---\n\n{cards}"
    monkeypatch.setattr(evidence_pipeline.composition, "synthesize", synth)
    web = "# From the web (dated, with sources)\n**Query**: q\n\nA liquidation cluster is a zone. [1]\n\nSources:\n[1] [x](https://x.y) · 2026-09-27"
    router = FakeRouter({"perplexity_web_search": web, "goldrush_hyperliquid_market": "# Hyperliquid market data\n## SOL\n**Open Interest**: 1"})
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({"session_context": {"focus": {"kind": "topic", "label": "SOL"}}}, "what is a liquidation cluster?\nResolved from conversation context: this continues the discussion about SOL.", ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    assert out is not None and "goldrush_hyperliquid_market" not in router.calls
