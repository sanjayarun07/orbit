"""The UAT preflight of build aa7fb46d (reports/uat-preflight-aa7fb46d/report.md,
2026-09-27): three wrong answers and the follow-up context failures, each a
deterministic reading rule here."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import context_entities, contracts, equities_data, exit_controls, tasks_nl, tequity, why_moving
from app.routing import subject_probe

WALLET = "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS"


# --- P0: "What is the live price versus the reported catalyst?" after SOL looked up a token called CATALYST ---

def test_a_comparison_word_after_a_metric_is_never_a_name_and_the_follow_up_continues_sol():
    q = "What is the live price versus the reported catalyst?"
    assert subject_probe._lower_name(q) is None and not subject_probe.has_own_subject(q) and subject_probe.continues_subject(q)
    ctx = {"focus": {"kind": "topic", "label": "SOL", "address": None, "chain": None}}
    resolved = context_entities.resolve_contextual_request(q, "user: Why is SOL moving today?\nassistant: SOL is up 2%...", ctx)
    assert "this continues the discussion about SOL" in resolved
    assert not contracts.is_open_research(q)                 # a live price is exact state, never a web read
    assert subject_probe._lower_name("price of pepe") == "pepe"   # a real lowercase name still is one


# --- P0: "Why is BP moving today?" answered for Backpack (BP) ---

def test_a_ticker_that_is_a_listed_company_and_a_lesser_coin_is_a_question(monkeypatch):
    from app import symbol_registry
    monkeypatch.setattr(symbol_registry, "listed", lambda sym: [{"name": "Backpack", "symbol": "BP", "rank": 412}])
    monkeypatch.setattr(symbol_registry, "leader", lambda rows: rows[0])
    monkeypatch.setattr(equities_data, "enabled", lambda: True)
    monkeypatch.setattr(equities_data, "is_listed_ticker", lambda sym: sym == "BP")
    ask = asyncio.run(why_moving.stock_namesake_ask("BP", "Why is BP moving today?"))
    assert ask.startswith("**BP** can mean a listed company (ticker BP) or the token **Backpack** (CoinGecko rank 412)")
    assert "`why is BP stock moving`" in ask and "`why is BP token moving`" in ask
    assert asyncio.run(why_moving.stock_namesake_ask("BP", "why is BP stock moving")) is None       # the words settle it
    assert asyncio.run(why_moving.stock_namesake_ask("BP", "why is BP token moving")) is None
    monkeypatch.setattr(symbol_registry, "listed", lambda sym: [{"name": "Solana", "symbol": "SOL", "rank": 6}])
    assert asyncio.run(why_moving.stock_namesake_ask("SOL", "Why is SOL moving today?")) is None     # a top-100 coin owns its ticker
    monkeypatch.setattr(symbol_registry, "listed", lambda sym: [{"name": "Backpack", "symbol": "BP", "rank": 412}])
    monkeypatch.setattr(equities_data, "is_listed_ticker", lambda sym: False)
    assert asyncio.run(why_moving.stock_namesake_ask("BP", "Why is BP moving today?"))                # a bare short ticker still asks (user decision 2026-09-28)
    assert why_moving.match("why is BP stock moving") == ("BP", "moving") and why_moving.prefers_stock("why is BP stock moving")


def test_the_equities_ticker_check_is_cached_and_honest_about_the_gateway(monkeypatch):
    calls = []
    monkeypatch.setattr(equities_data, "enabled", lambda: True)

    def run(name, request):
        calls.append(request)
        if "BP " in request:
            return "# card"
        raise equities_data.NoData("none")
    monkeypatch.setattr(equities_data, "run", run)
    equities_data._ticker_cache.clear()
    assert equities_data.is_listed_ticker("BP") is True and equities_data.is_listed_ticker("BP") is True and calls == ["BP company profile"]
    assert equities_data.is_listed_ticker("ZZZQ") is False
    monkeypatch.setattr(equities_data, "enabled", lambda: False)
    assert equities_data.is_listed_ticker("AAPL") is None


# --- P0: "For wallet <addr>, can I exit my ANSEM position? Read-only." went to the portfolio summary ---

def test_a_wallet_named_ahead_of_an_exit_control_is_the_wallet_to_read(monkeypatch):
    message = f"For wallet {WALLET}, can I exit my ANSEM position? Read-only, do not prepare or submit anything."
    assert exit_controls.split_wallet(message) == (WALLET, "can I exit my ANSEM position? Read-only, do not prepare or submit anything.")
    assert exit_controls.is_exit_control(message) and exit_controls.is_exit_control("can I exit my ANSEM position?")
    assert not exit_controls.is_exit_control(f"For wallet {WALLET}, what do I hold?")
    seen = {}

    async def resolve_token(token):
        return "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", "ANSEM", None

    async def position_of(wallet, mint):
        seen["wallet"] = wallet
        return None
    monkeypatch.setattr(exit_controls, "resolve_token", resolve_token)
    monkeypatch.setattr(exit_controls.exit_monitor, "position_of", position_of)
    reply = asyncio.run(exit_controls.handle(message, {"id": "u1"}, None))
    assert seen["wallet"] == WALLET and reply.startswith(f"`{WALLET[:6]}…{WALLET[-4:]}` holds no ANSEM")


# --- P1: the event-date follow-up listed the user's tasks ---

def test_an_event_scheduled_question_is_not_a_task_inventory_ask():
    assert not tasks_nl._inventory_ask("Which date and timezone is the next high-impact event scheduled for?")
    assert tasks_nl._inventory_ask("show my tasks") and tasks_nl._inventory_ask("what's scheduled for me?") and tasks_nl._inventory_ask("do I have any alerts running?")


def test_the_next_high_impact_event_points_at_the_calendar_just_shown():
    card = ("# Market events · 2026-09-27 → 2026-10-04\n**As of** 2026-09-27 20:51 UTC · 3 scheduled events\n\n\n## Wed 30 Sep\n"
            "- 🏛 **GDP Third Estimate, Q2 2026** · 08:30 ET · **HIGH** · [www.bea.gov](https://www.bea.gov/news/schedule)\n  BEA will publish.\n\n"
            "## Fri 2 Oct\n- 📊 **Employment Situation, September** · 08:30 ET · **HIGH** · [www.bls.gov](https://www.bls.gov)\n")
    items = context_entities.answer_items(card)
    assert items[0].startswith("Wed 30 Sep · 🏛 GDP Third Estimate, Q2 2026 · 08:30 ET · HIGH") and items[1].startswith("Fri 2 Oct · 📊 Employment Situation")
    note = context_entities.items_note("Which date and timezone is the next high-impact event scheduled for?", {"last_items": items})
    assert note and "(1) Wed 30 Sep · 🏛 GDP Third Estimate" in note
    assert context_entities.items_note("Which of those are exchanges?", {"last_items": ["a · b"]})       # the old form still works


# --- P1: the earnings follow-up became a Hyperliquid AAPL quote ---

def test_the_quote_matcher_reads_the_ask_never_the_notes():
    resolved = ("What came from the filing versus analyst estimates?\nResolved from conversation context: this continues the discussion about AAPL "
                "(the subject of the previous turns: a protocol, company or topic, not a token symbol to look up).")
    assert not tequity.quote_matches(resolved) and tequity.ask_of(resolved) == "What came from the filing versus analyst estimates?"
    assert tequity.quote_matches("How is ASTER trading on Hyperliquid?\nResolved subject: token ASTER.") and tequity.pair_of("hyperliquid btc price\nResearch objective of this conversation: x") == ("hyperliquid", "BTC")


# --- P1: "Only the 24-hour quote-volume field." after the Hyperliquid stock table searched for a definition ---

def test_a_field_only_follow_up_reruns_the_previous_table_for_that_field():
    ctx = {"last_contract": {"kind": "market_ranking", "subject": {"kind": "venue", "symbol": None, "id": None, "chain": None}, "venue": "hyperliquid",
                             "scope": "venue_trades", "metric": "price_change", "window_hours": 24, "direction": "gainers", "filters": {"stocks_only": True}}}
    resolved = context_entities.resolve_contextual_request("Only the 24-hour quote-volume field.", "user: which tokenized stocks are moving on hyperliquid?\nassistant: table", ctx)
    assert resolved.startswith("most traded pairs on hyperliquid (tokenized stocks only) over the last 24 hours: Only the 24-hour quote-volume field.")
    assert "this asks for only the 24-hour quote-volume of that same table" in resolved
    plan = contracts.plan_by_rules(resolved)
    assert plan.kind == "market_ranking" and plan.venue == "hyperliquid" and plan.metric == "volume" and plan.filters.get("stocks_only")
    assert context_entities.field_only_request("Only the 24-hour quote-volume field.", {}) is None      # nothing shown before: nothing to narrow


# --- P1: NEAR funding and open interest "now" went to the web ---

def test_venue_perp_state_now_is_exact_state_not_open_research():
    q = "How are NEAR funding and open interest on Hyperliquid now?"
    assert not contracts.is_open_research(q) and contracts.plan_by_rules(q).kind != "open_research"
    assert contracts.is_open_research("How do funding rates work on perpetual exchanges?")     # a concept question stays open research


def test_a_lesser_coin_our_stock_data_cannot_check_answers_under_a_lead_line(monkeypatch):
    from app import symbol_registry
    monkeypatch.setattr(symbol_registry, "listed", lambda sym: [{"name": "Moodeng", "symbol": "MOODENG", "rank": 210}])
    monkeypatch.setattr(symbol_registry, "leader", lambda rows: rows[0])
    monkeypatch.setattr(equities_data, "enabled", lambda: True)
    monkeypatch.setattr(equities_data, "is_listed_ticker", lambda sym: False)      # no stock signal for a long ticker
    monkeypatch.setattr(tequity, "resolve_pair", lambda venue, sym: None)
    ask, hedge = asyncio.run(why_moving.namesake("MOODENG", "Why is MOODENG moving today?"))
    assert ask is None and hedge is None                                          # seven letters: no company reading, no lead line
    monkeypatch.setattr(symbol_registry, "listed", lambda sym: [{"name": "Ansem", "symbol": "ANSEM", "rank": 1400}])
    ask, hedge = asyncio.run(why_moving.namesake("ANSEM", "Why is ANSEM moving today?"))
    assert ask is None and hedge.startswith("_ANSEM here is the token **Ansem** (CoinGecko rank 1400)") and "`why is ANSEM stock moving`" in hedge
    monkeypatch.setattr(tequity, "resolve_pair", lambda venue, sym: "ANSEMUSDC" if venue == "hyperliquid" else None)
    ask, hedge = asyncio.run(why_moving.namesake("ANSEM", "Why is ANSEM moving today?"))
    assert ask and ask.startswith("**ANSEM** can mean a listed company") and hedge is None      # the venue feed lists it as a stock: a question


def test_the_ticker_check_tries_the_profile_before_the_price_snapshot(monkeypatch):
    calls = []
    monkeypatch.setattr(equities_data, "enabled", lambda: True)

    def run(name, request):
        calls.append(name)
        if name == "equities_ticker_details" and request.startswith("AAPL"):
            return "# profile"
        raise equities_data.NoData("none")
    monkeypatch.setattr(equities_data, "run", run)
    equities_data._ticker_cache.clear()
    assert equities_data.is_listed_ticker("AAPL") is True and calls == ["equities_ticker_details"]
    assert equities_data.is_listed_ticker("BP") is False and calls[1:] == ["equities_ticker_details", "equities_price_snapshot"]


def test_the_latest_reported_quarter_has_no_rolling_window():
    plan = contracts.plan_by_rules("How did AAPL's latest reported quarter compare with expectations?")
    assert plan.kind == "recent_events" and plan.window_hours is None
    assert contracts.plan_by_rules("AAPL news in the last 7 days").window_hours == 24 * 7
    assert contracts.plan_by_rules("What happened with JUP this week?").window_hours


def test_the_reported_catalyst_is_taken_from_the_previous_answer():
    last = ("# Why is Solana (SOL) moving?\n**As of** 2026-09-27 21:12 UTC · crypto\n\n## Market\n- **Price**: $122.89 · **24h**: +1.85%\n\n"
            "## What's driving it\nSOL is moving higher today on ETF-inflow headlines and the Alpenglow testnet milestone [1].\n\n## Sources\n- [1] x")
    ctx = {"focus": {"kind": "topic", "label": "SOL", "address": None, "chain": None}, "last_answer": last}
    resolved = context_entities.resolve_contextual_request("What is the live price versus the reported catalyst?", "user: why is SOL moving today?\nassistant: ...", ctx)
    assert "this continues the discussion about SOL" in resolved
    assert '"the reported catalyst" is what the previous answer reported' in resolved and "ETF-inflow headlines and the Alpenglow testnet milestone" in resolved
    assert "## Sources" not in resolved
    assert context_entities.prior_reference_note("What is the live price?", ctx) is None


def test_a_shared_unit_range_is_carried_by_the_card():
    from app import fact_gate
    card = "consensus was roughly **$108.65 billion–$108.86 billion** for revenue and **$1.89** for EPS"
    assert fact_gate.unsupported_figures("consensus of $108.65–$108.86 billion for revenue", [], evidence_text=card) == []
    assert fact_gate.unsupported_figures("consensus of $108.65 to $108.86 billion", [], evidence_text=card) == []
    assert fact_gate.unsupported_figures("a range of 40-45% of supply", [], evidence_text="holds 40%–45% of supply") == []
    assert fact_gate.unsupported_figures("consensus of $118.65 billion", [], evidence_text=card) == ["$118.65 billion"]


def test_the_planner_merge_keeps_the_latest_quarter_without_a_window(monkeypatch):
    from types import SimpleNamespace
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: True)

    async def planner(program, **kw):
        return SimpleNamespace(contract='{"kind": "recent_events", "subject": {"kind": "topic", "symbol": "AAPL"}, "window_hours": 720, "metric": "events", "confidence": 0.8}')
    monkeypatch.setattr(runtime, "_call_planner_lm", planner)
    plan = asyncio.run(contracts.plan("How did AAPL's latest reported quarter compare with expectations?", ""))
    assert plan.kind == "recent_events" and plan.window_hours is None
    plan = asyncio.run(contracts.plan("AAPL news in the last 7 days", ""))
    assert plan.window_hours == 24 * 7


def test_a_latest_ask_accepts_the_newest_past_event_whenever_it_was():
    from datetime import datetime, timedelta, timezone
    from app import fact_gate
    from app.facts import Fact
    old = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
    row = Fact(kind="event", subject="Apple fiscal Q3 results", attrs={"date": old}, source="perplexity_web_search", event_date=old)
    latest = contracts.plan_by_rules("How did AAPL's latest reported quarter compare with expectations?")
    assert fact_gate.check(latest, [row], scope_satisfied=True).ok
    windowed = contracts.plan_by_rules("AAPL news in the last 7 days")
    gate = fact_gate.check(windowed, [row], scope_satisfied=True)
    assert not gate.ok and any("inside the last 7 days" in m for m in gate.missing)
