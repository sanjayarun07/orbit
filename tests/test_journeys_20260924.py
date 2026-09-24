"""Regressions from the expanded journeys re-run (2026-09-24, run 2):
context carry across product answers and controls, a fresh chat with a
remembered user, corrections of the previous subject, and a token mint that
must never be read as a wallet."""
from __future__ import annotations

import asyncio
import re

import pytest

from app import address_roles, context_entities, dexscreener_tools, exit_controls, experience, tequity, user_memory
from app.contracts import plan_by_rules
from app.routing.resolver import _bare_referent
from app.routing.subject_probe import _REFERENT, continues_subject


# --- a fresh chat with a remembered user is still a fresh chat ---------------

def test_memory_block_is_not_conversation_history():
    history = user_memory.with_block("", [{"fact": "Holds SOL and ANSEM"}])
    assert user_memory.conversation_only(history) == ""
    assert _bare_referent("What did it do today?", {"session_context": {}, "history": history})


def test_conversation_after_memory_block_is_kept():
    history = user_memory.with_block("user: Check BONK.\nassistant: BONK is ...", [{"fact": "Holds SOL"}])
    assert user_memory.conversation_only(history).startswith("user: Check BONK.")
    assert not _bare_referent("What did it do today?", {"session_context": {}, "history": history})


# --- a referent question is not a list of events ----------------------------

def test_referent_question_is_open_research_without_a_window():
    contract = plan_by_rules("Does that automatically authorize Robinhood Chain today?")
    assert contract.kind == "open_research"
    assert contract.window_hours is None


def test_events_ask_still_plans_events():
    assert plan_by_rules("What happened with Aave this week?").kind == "recent_events"


def test_since_then_points_at_the_previous_answer():
    assert _REFERENT.match("What is different since then?")
    assert continues_subject("What is different since then?")
    assert not _REFERENT.match("What is the price of BONK?")


# --- stock negations: an exclusion is crypto only; "perps, not Nasdaq shares" keeps stocks

@pytest.mark.parametrize("text", ["Aster movers excluding the US equities", "crypto only on Aster", "Hyperliquid gainers, no stocks"])
def test_stock_negation_with_a_market_name(text):
    assert tequity.stock_filter(text) is False


def test_perps_not_shares_keeps_the_tokenized_stock_reading():
    # "Which Aster stocks are moving?" -> "I mean Aster perpetual contracts, not Nasdaq shares": the perps on stocks, not crypto (review of bc40f724)
    assert tequity.stock_filter("I mean Aster perpetual contracts, not Nasdaq shares.") is True
    assert plan_by_rules("Aster perpetual contracts moving most, not Nasdaq shares").filters == {"stocks_only": True}


def test_stock_words_alone_still_mean_stocks():
    assert tequity.stock_filter("Which Aster stocks are moving?") is True


# --- a dollar size is a USD amount, never "000 POSITION" --------------------

def test_dollar_position_is_a_usd_amount():
    contract = plan_by_rules("And which has a better exit for a $1,000 position?\nResolved from canonical session context: token TOKEN mint DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 on solana.")
    assert contract.filters.get("amount_usd") == 1000.0
    assert contract.filters.get("input_token") != "POSITION"
    assert contract.filters.get("amount") != "000"


def test_token_amount_still_parses():
    contract = plan_by_rules("estimate selling 0.05 SOL to USDC")
    assert (contract.filters["amount"], contract.filters["input_token"], contract.filters["output_token"]) == ("0.05", "SOL", "USDC")


# --- a sized exit is a read-only sizing, an order is not --------------------

@pytest.mark.parametrize("text, amount, token", [
    ("And which has a better exit for a $1,000 position?", "$1,000", None),
    ("What would exiting $500 of BONK cost?", "$500", "BONK"),
    ("What would a $1,000 exit of BONK return?", "$1,000", "BONK"),
])
def test_sized_exit_grammar(text, amount, token):
    m = exit_controls._SIZED_EXIT.match(text)
    assert m and m.group("amount") == amount and m.group("t") == token
    assert exit_controls.is_exit_control(text) and exit_controls.is_public_control(text)


@pytest.mark.parametrize("text", ["sell $2k of WIF", "How do I sell $100 of SOL?", "What is the price of $BONK?", "How much is $1,000 of SOL?"])
def test_orders_and_prices_are_not_sized_exits(text):
    assert exit_controls._SIZED_EXIT.match(text) is None


def test_sized_exit_takes_the_focus_token_and_says_so(monkeypatch):
    async def fake_resolve(token):
        return "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "BONK", None

    async def fake_sizes(mint, amounts):
        return [{"ok": True, "usd": amounts[0], "tokens": 1.0, "entry_price": 1.0, "entry_impact_pct": 0.1, "exit_usd": amounts[0] * 0.99, "round_trip_pct": -1.0, "routes": "Raydium"}]

    monkeypatch.setattr(exit_controls, "resolve_token", fake_resolve)
    monkeypatch.setattr(exit_controls.exit_monitor, "size_comparison", fake_sizes)
    monkeypatch.setattr(exit_controls.exit_monitor, "render_sizes", lambda symbol, mint, rows: f"# Sizing — {symbol}")
    focus = {"kind": "token", "label": "TOKEN", "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "chain": "solana"}
    answer = asyncio.run(exit_controls.handle("And which has a better exit for a $1,000 position?", None, None, focus=focus))
    assert "Read-only sizing" in answer and "# Sizing — BONK" in answer
    assert "Only BONK was sized" in answer and "name the other token" in answer


def test_sized_exit_without_a_token_asks():
    answer = asyncio.run(exit_controls.handle("What would exiting $500 cost?", None, None, focus=None))
    assert answer.startswith("Which token")


# --- a token mint is never read as a wallet ---------------------------------

def test_address_roles_bind_the_focus_token():
    mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    token = address_roles.bind_turn({"focus": {"kind": "token", "address": mint}})
    try:
        assert address_roles.role_of(mint) == "token"
        assert "not a wallet" in address_roles.not_a_wallet(mint)
        assert address_roles.not_a_wallet("3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS") is None
    finally:
        address_roles.end_turn(token)
    assert address_roles.role_of(mint) is None


def test_mobula_portfolio_refuses_the_focus_token(monkeypatch):
    from app import mobula_wallet
    mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    monkeypatch.setattr(mobula_wallet, "_get", lambda *a, **k: pytest.fail("must not call Mobula"))
    token = address_roles.bind_turn({"focus": {"kind": "token", "address": mint}})
    try:
        with pytest.raises(ValueError, match="not a wallet"):
            mobula_wallet.portfolio(f"{mint} wallet portfolio")
    finally:
        address_roles.end_turn(token)


# --- an address quoted in the answer is not the conversation's wallet -------

def test_wallet_quoted_in_a_passage_is_not_the_focus():
    answer = "Forum passage: My address is 0x96f33f234603f3cc01b063e0563f550c504ac0b7, can anyone help?"
    capsules = experience.build_context_capsules("What did it do today?", "", answer, None, None)
    assert not [c for c in capsules if c.kind == "wallet"]


def test_wallet_in_the_answer_counts_for_a_wallet_ask():
    answer = "Top holder wallet: 0x96f33f234603f3cc01b063e0563f550c504ac0b7 on ethereum"
    capsules = experience.build_context_capsules("Which whale wallets hold the most?", "", answer, None, None)
    assert [c for c in capsules if c.kind == "wallet"]


# --- controls and product answers set the subject ---------------------------

def test_exit_control_names_the_token_as_focus():
    context = experience.advance_session_context({}, "Can I exit ANSEM?", None, "general", [], [], None)
    assert context["focus"]["label"] == "ANSEM"


def test_product_question_keeps_the_focus():
    context = experience.advance_session_context({"focus": {"kind": "topic", "label": "ANSEM"}}, "What can I do here?", None, "general", [], [], None)
    assert context["focus"]["label"] == "ANSEM"


def test_headline_pick_becomes_the_focus(monkeypatch):
    from app import home_highlights
    monkeypatch.setattr(home_highlights, "get_highlights", lambda: {"cards": [{"title": "Bitcoin ETFs pull in $999 million", "kind": "crypto"}], "meme_cards": []})
    context = experience.advance_session_context({}, "Summarize the first Home market headline.", None, "general", [], [], None)
    assert context["focus"] == {"kind": "topic", "label": "Bitcoin ETFs pull in $999 million", "address": None, "chain": None, "confidence": 0.9, "source": "home_headline"}


# --- a correction of the subject re-runs the previous request ---------------

def test_subject_correction_reruns_the_previous_request():
    history = "user: Check SPX.\nassistant: The ticker SPX primarily denotes the S&P 500 Index..."
    resolved = context_entities.resolve_contextual_request("I mean SPX6900 the meme token, not the index.", history, {"focus": {"kind": "topic", "label": "SPX"}})
    assert resolved.startswith("Check the SPX6900 token.")
    assert "corrected the subject of the previous request from SPX to SPX6900" in resolved


def test_correction_without_a_previous_request_is_left_alone():
    assert context_entities.corrected_request("I mean SPX6900 the meme token.", "") is None


def test_pair_search_names_the_token_not_its_description():
    request = "I mean SPX6900 the meme token, not the index."
    named = next((m for m in re.finditer(r"\b([A-Za-z][A-Za-z0-9._-]{1,15})\s+(?:(?:the|a|an)\s+)?(?:(?:meme|defi|ai|gaming|utility|governance|new|solana|base)\s+)?(?:token|coin|memecoin)\b", request, re.I)
                  if m.group(1).lower() not in dexscreener_tools._DESCRIPTORS), None)
    assert named and named.group(1) == "SPX6900"


# --- family fixes: period movers, HL alias, durations, index namesakes -------

def test_no_stocks_period_movers_keep_crypto_only(monkeypatch):
    from app import tequity_ledger
    rows = {"venue": "hyperliquid", "from": None, "to": None, "pairs": 2,
            "gainers": [{"symbol": "AAPL/USDC", "is_stock": True, "change_between_pct": 3.0, "price_then": 1, "last_price": 1.03},
                        {"symbol": "ACE/USDC", "is_stock": False, "change_between_pct": 2.0, "price_then": 1, "last_price": 1.02}],
            "losers": [], "coverage_gap": None}
    seen = {}

    async def fake(venue, start, end, *, stocks_only=False, limit=15, max_gap=None):
        seen["stocks_only"] = stocks_only
        return dict(rows)

    monkeypatch.setattr(tequity_ledger, "movers_between", fake)
    monkeypatch.setattr(tequity, "_sync", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(tequity.evidence, "complete", lambda *a, **k: None)
    out = tequity.period_movers("Top Hyperliquid crypto perp gainers in the last hour, no stocks.")
    assert seen["stocks_only"] is False
    assert "ACE/USDC" in out and "AAPL/USDC" not in out


def test_hl_is_the_venue_not_a_coin():
    from app.nodes import research
    assert research._named_tickers("Which HL coin perps have pumped most over 60m?") == []
    assert research._named_tickers("Check ASTER token") == ["ASTER"]


def test_a_duration_is_not_a_comparison():
    from app import fact_gate
    assert fact_gate.contradictions("PUMP earned $14.97M over 30 days.") == []
    assert fact_gate.contradictions("BTC at $86,000 is above $90,000.")


def test_index_ticker_that_is_a_listed_coin_asks(monkeypatch):
    from app import listed_asset, symbol_registry
    monkeypatch.setattr(symbol_registry, "listed", lambda symbol: [{"id": "spx6900", "name": "SPX6900", "symbol": "SPX", "rank": 126}])
    ask = asyncio.run(listed_asset.index_namesake_ask("What is SPX doing?"))
    assert ask and "SPX6900" in ask and "index" in ask
    assert asyncio.run(listed_asset.index_namesake_ask("What is the SPX index doing?")) is None
    assert asyncio.run(listed_asset.index_namesake_ask("Check BONK")) is None


def test_a_venue_bound_source_is_not_near_another_venue():
    from app import evidence_pipeline
    contract = plan_by_rules("Meme tokens on Binance are moving.")
    assert contract.venue == "binance"
    ranked = [("tequity_period_movers", "venue binance not covered"), ("tequity_movers", "venue binance not covered")]
    assert evidence_pipeline._near_tools(contract, ranked) == []


def test_contract_describes_itself_in_plain_words():
    assert plan_by_rules("Show BASE gainers.").describe() == "gainers on Base (trades on that venue) over 24h"
    assert plan_by_rules("Top ANSEM holders").describe().startswith("holders for ANSEM")


# --- run 3: headline follow-ups, exit estimates, read-only after an exit ----

_HEADLINE_FOCUS = {"kind": "topic", "label": "Bitcoin falls below $84,000 as yields rise", "address": None, "chain": None, "confidence": 0.9, "source": "home_headline"}


def test_headline_followup_is_research_on_the_story_not_its_words():
    note = context_entities.resolve_contextual_request("When did that event happen, versus when was the source published?", "", {"focus": _HEADLINE_FOCUS})
    assert 'Home news headline "Bitcoin falls below $84,000 as yields rise"' in note
    contract = plan_by_rules(note)
    assert contract.kind == "open_research" and contract.subject.name == _HEADLINE_FOCUS["label"] and contract.window_hours is None


def test_headline_followup_routes_to_web_research():
    from app.routing import resolver

    async def no_model(*a, **k):
        raise AssertionError("the rule decides before the model")

    state = {"request": "What is different since then?", "session_context": {"focus": _HEADLINE_FOCUS}, "history": "user: Summarize the first Home market headline.\nassistant: ..."}
    update = asyncio.run(resolver.resolve(state, no_model))
    assert update["intent"] == "research" and update["routing_decision"]["reason"] == "headline_followup"
    assert "web_research" in update["capabilities"]


@pytest.mark.parametrize("text, token", [
    ("For the ANSEM amount there, estimate a full exit to USDC; no trade.", "ANSEM"),
    ("Estimate a full exit from BONK", "BONK"),
])
def test_exit_estimate_in_any_wording_is_an_exit_control(text, token):
    assert exit_controls.is_exit_control(text)
    est = exit_controls._EXIT_ESTIMATE.search(text)
    assert (exit_controls._clean(est.group("t")) if est.group("t") else exit_controls._token_mention(text, None)) == token


def test_a_stated_amount_is_a_simulation_not_a_position_exit():
    text = "Price-check my 0.05 SOL exit into USDC, no wallet signature."
    assert not exit_controls.is_exit_control(text)
    assert asyncio.run(exit_controls.handle(text, {"id": "u"}, "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS")) is None


def test_exit_estimate_without_a_wallet_falls_through_to_the_size_ask():
    assert asyncio.run(exit_controls.handle("For the ANSEM amount there, estimate a full exit to USDC; no trade.", {"id": "u"}, None)) is None


def test_read_only_after_an_exit_analysis_says_it_already_was():
    answer = asyncio.run(exit_controls.handle("I only want a read-only estimate.", {"id": "u"}, None, last_capabilities=["exit_control"]))
    assert answer and "already read-only" in answer
    assert asyncio.run(exit_controls.handle("I only want a read-only estimate.", {"id": "u"}, None, last_capabilities=[])) is None


def test_token_mention_prefers_the_sentence_then_the_focus():
    assert exit_controls._token_mention("For the ANSEM amount there", None) == "ANSEM"
    assert exit_controls._token_mention("estimate a full exit", {"kind": "token", "label": "TOKEN", "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}) == "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    assert exit_controls._token_mention("estimate a full exit to USDC", None) is None


# --- run 4: bare "what changed", holders in more words, crypto-only wording,
# the headline list as focus, the next eligible source, the feed's own coverage

def test_bare_what_changed_points_back_and_asks_when_nothing_is_there():
    assert _REFERENT.match("What changed?") and continues_subject("What changed?")
    assert not _REFERENT.match("What happened with Aave this week?")
    assert _bare_referent("What changed?", {"session_context": {}, "history": ""})


def test_top_ten_hold_is_a_holders_ask():
    assert plan_by_rules("Back to BONK: what percent do the top 10 hold, and what counts in that denominator?").kind == "holders"


@pytest.mark.parametrize("text", ["Top Hyperliquid crypto perp gainers today.", "Which HL coin perps have pumped most over 60m?", "Past-hour winners on Hyperliquid, crypto contracts only."])
def test_crypto_perp_wording_is_crypto_only(text):
    assert tequity.stock_filter(text) is False
    assert plan_by_rules(text).filters.get("crypto_only") is True


def test_headline_list_becomes_the_focus(monkeypatch):
    from app import home_highlights
    monkeypatch.setattr(home_highlights, "get_highlights", lambda: {"cards": [{"title": "A story", "kind": "crypto"}, {"title": "B story", "kind": "stocks"}], "meme_cards": []})
    context = experience.advance_session_context({}, "Summarize today's Home headlines; which source published each and when?", None, "general", [], [], None)
    assert context["focus"]["label"] == "A story; B story" and context["focus"]["source"] == "home_headline"


def test_corrected_subject_travels_as_a_token():
    from app.nodes import research
    resolved = context_entities.corrected_request("I mean SPX6900 the meme token, not the index.", "user: Check SPX.\nassistant: ...")
    assert resolved.startswith("Check the SPX6900 token.")
    assert research._named_tickers(resolved) == ["SPX6900"]


def test_registry_lists_a_coin_by_name(monkeypatch):
    from app import symbol_registry
    symbol_registry.reset()
    monkeypatch.setattr(symbol_registry, "_cg", lambda path, params: {"coins": [{"id": "spx6900", "name": "SPX6900", "symbol": "SPX", "market_cap_rank": 125}]})
    rows = symbol_registry._search("SPX6900")            # the suite stubs listed(); the search is the part that reads the name
    assert rows and rows[0]["id"] == "spx6900" and rows[0]["symbol"] == "SPX"


def test_a_minute_span_is_not_an_unsupported_figure():
    from app import fact_gate
    assert fact_gate.unsupported_figures("The window ran 1 hour and 42 minutes; volume was $57.", [], "From tick 08:42 to 10:24 UTC") == ["$57"]


def test_sized_exit_label_has_no_dollar_sign(monkeypatch):
    async def fake_resolve(token):
        return "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", None, None

    async def fake_sizes(mint, amounts):
        return []

    monkeypatch.setattr(exit_controls, "resolve_token", fake_resolve)
    monkeypatch.setattr(exit_controls.exit_monitor, "size_comparison", fake_sizes)
    monkeypatch.setattr(exit_controls.exit_monitor, "render_sizes", lambda symbol, mint, rows: f"# Sizing — {symbol}")
    answer = asyncio.run(exit_controls.handle("And which has a better exit for a $1,000 position?", None, None, focus={"kind": "topic", "label": "$WIF"}))
    assert "# Sizing — WIF" in answer and "$WIF" not in answer


def test_feed_coverage_question_is_a_product_question():
    from app import product_actions
    assert product_actions.asks_feed_coverage("Can your feed really support that interval?")
    assert product_actions.is_product_question("Can your feed really support that interval?")
    assert not product_actions.asks_feed_coverage("Top movers on Hyperliquid in the last hour")



# --- review of bc40f724: task numbers, excluded venues, hour baselines, "50 bps", verified mints

def test_filtered_task_list_keeps_the_full_list_numbers(monkeypatch):
    from app import tasks, tasks_nl
    rows = [{"id": "a", "title": "Price alert", "schedule": {"every": "5m"}, "status": "active", "next_run_at": None},
            {"id": "b", "title": "Morning brief", "schedule": {"daily": "08:00"}, "status": "paused", "next_run_at": None}]

    async def fake_list(user_id, include_done=False):
        return rows

    monkeypatch.setattr(tasks, "list_tasks", fake_list)
    monkeypatch.setattr(tasks, "describe_schedule", lambda schedule, tz: "daily")
    monkeypatch.setattr(tasks_nl, "_when", lambda task: "soon")
    listing = asyncio.run(tasks_nl._render_list({"id": "u"}, status="paused"))
    assert "2. **Morning brief**" in listing and "1. **Morning brief**" not in listing
    assert "resume task 1" not in listing and "task 2" in listing


def test_an_excluded_venue_is_never_the_venue():
    contract = plan_by_rules("Top stock perps not on Hyperliquid but on Aster")
    assert contract.venue == "aster" and contract.filters.get("exclude") == ["hyperliquid"]
    assert plan_by_rules("Top movers on Hyperliquid, not Aster").venue == "hyperliquid"


def test_an_hour_window_takes_the_nearest_tick_after_a_far_baseline(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import tequity_ledger
    now = datetime(2026, 9, 24, 10, 24, tzinfo=timezone.utc)
    start = now - timedelta(hours=1)
    far = [{"symbol": "X", "taken_at": start - timedelta(minutes=42), "last_price": 100.0}]
    near = [{"symbol": "X", "taken_at": start + timedelta(minutes=3), "last_price": 101.0}]
    end_rows = [{"symbol": "X", "taken_at": now, "last_price": 103.0}]

    async def tick_at(channel, moment):
        return end_rows if moment == now else far

    async def first_tick_from(channel, moment):
        return near

    monkeypatch.setattr(tequity_ledger, "tick_at", tick_at)
    monkeypatch.setattr(tequity_ledger, "first_tick_from", first_tick_from)
    out = asyncio.run(tequity_ledger.movers_between("hyperliquid", start, now))
    assert out["from"] == near[0]["taken_at"]
    assert out["coverage_gap"] is None
    assert round(out["gainers"][0]["change_between_pct"], 2) == round((103 - 101) / 101 * 100, 2)


def test_a_far_baseline_with_nothing_nearer_is_named(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import tequity_ledger
    now = datetime(2026, 9, 24, 10, 24, tzinfo=timezone.utc)
    start = now - timedelta(hours=1)
    far = [{"symbol": "X", "taken_at": start - timedelta(minutes=42), "last_price": 100.0}]
    end_rows = [{"symbol": "X", "taken_at": now, "last_price": 103.0}]

    async def tick_at(channel, moment):
        return end_rows if moment == now else far

    async def first_tick_from(channel, moment):
        return []

    monkeypatch.setattr(tequity_ledger, "tick_at", tick_at)
    monkeypatch.setattr(tequity_ledger, "first_tick_from", first_tick_from)
    out = asyncio.run(tequity_ledger.movers_between("hyperliquid", start, now))
    assert out["coverage_gap"] == "baseline tick 42 min before the window start"
    card = tequity.render_period_movers("hyperliquid", out, start, stocks_only=False, losers=False)
    assert "spans 102 minutes, not the 60 asked" in card


def test_an_overlong_span_is_said_first():
    from app import evidence_pipeline
    cards = "**From tick**: 2026-09-24 08:42 UTC · **To tick**: 2026-09-24 10:24 UTC"
    assert evidence_pipeline._covered_hours(cards) is not None


def test_bps_is_not_a_stated_amount():
    assert exit_controls._STATED_AMOUNT.search("Estimate a full exit of ANSEM with 50 bps slippage") is None
    assert exit_controls._STATED_AMOUNT.search("Estimate selling 1000 ANSEM to USDC")
    assert exit_controls.is_exit_control("Estimate a full exit of ANSEM with 50 bps slippage")


def test_verified_mint_never_takes_an_unverified_lone_match(monkeypatch):
    from app.nodes import portfolio
    from app.jupiter import jupiter

    async def search(symbol):
        return [{"id": "Mint1111111111111111111111111111111111111111", "symbol": "ANSEM", "tags": [], "organicScore": 90}]

    monkeypatch.setattr(jupiter, "search_tokens", search)
    assert asyncio.run(portfolio._verified_mint("ANSEM")) is None

    async def search_verified(symbol):
        return [{"id": "Mint1111111111111111111111111111111111111111", "symbol": "ANSEM", "tags": ["verified"], "organicScore": 90}]

    monkeypatch.setattr(jupiter, "search_tokens", search_verified)
    assert asyncio.run(portfolio._verified_mint("ANSEM")) == "Mint1111111111111111111111111111111111111111"


# --- a Home tile tap explains its referent after the colon (live, 2026-09-24) ---

@pytest.mark.parametrize("text", [
    "What does this mean for the market: Bitcoin and Ethereum ETFs see $592 million outflows",
    "What does this mean for the market: Nasdaq closes at record",
])
def test_a_tile_tap_is_never_a_bare_referent(text):
    from app.routing.subject_probe import is_referent
    assert not is_referent(text)
    assert not continues_subject(text)
    assert not _bare_referent(text, {"session_context": {}, "history": ""})


def test_a_bare_pronoun_question_still_asks():
    from app.routing.subject_probe import is_referent
    assert is_referent("What does this mean?")
    assert _bare_referent("What does this mean?", {"session_context": {}, "history": ""})


def test_since_yesterday_starts_at_yesterdays_midnight():
    from datetime import datetime, timezone
    from app.contracts import _window_hours
    hours = _window_hours("Only events since yesterday, not pages updated today.")
    now = datetime.now(timezone.utc)
    assert hours is not None and 24.0 < hours <= 48.0
    assert abs(hours - (now.hour + now.minute / 60 + 24)) < 0.1


def test_a_holders_ask_about_a_listed_coin_is_not_the_listing_path():
    from app import contracts
    assert contracts._HOLDERS.search("Who owns ANSEM supply?")


def test_a_holders_ask_resolves_its_ticker_whatever_the_capabilities(monkeypatch):
    from app.nodes import research

    def mint(ticker):
        return "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump" if ticker == "ANSEM" else None

    async def no_leader(ticker):
        return None

    monkeypatch.setattr(research, "_jupiter_solana_mint", mint)
    monkeypatch.setattr(research, "_listed_leader", no_leader)
    monkeypatch.setattr(research, "token_candidates", lambda ticker: [])
    resolution = asyncio.run(research._resolve_named_token("Any whales in ANSEM?", {"wallet_intelligence", "web_research"}, ()))
    assert "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump" in resolution.request and resolution.chain == "solana"


# --- a theme carries into "how it works in X" (Akash vs Minara, live 2026-09-24) ---

def test_a_lowercase_name_beside_its_kind_word_is_a_subject():
    from app.routing.subject_probe import subject_of
    assert subject_of("how it works in akash network") == "Akash Network"
    assert subject_of("compare render network and akash") == "Render Network"
    assert subject_of("which protocol is best") is None
    assert subject_of("the main protocol token") is None


def test_it_after_a_concept_question_is_that_theme():
    history = "user: apart from staking how else can we tie dual token to the main token\nassistant: Burn-to-mint, fee burns..."
    resolved = context_entities.resolve_contextual_request("how it works in akash network", history, {"focus": None, "last_contract": {"kind": "open_research", "subject": {"kind": "topic"}}})
    assert resolved.startswith("In Akash Network: apart from staking how else can we tie dual token to the main token?")
    assert '"it" is what the previous question was about' in resolved


def test_it_after_a_question_with_its_own_subject_is_left_to_the_focus():
    history = "user: How does Aave V3 liquidation work?\nassistant: ..."
    assert context_entities.themed_followup("how it works in akash network", history) is None


def test_open_research_names_a_lowercase_subject():
    contract = plan_by_rules("how it works in akash network")
    assert contract.kind == "open_research" and contract.subject.name == "Akash Network"
    assert plan_by_rules("apart from staking how else can we tie dual token to the main token").subject.name is None


def test_the_theme_yields_to_a_focus():
    history = "user: what does the order authorize?\nassistant: ..."
    ctx = {"focus": {"kind": "topic", "label": "SEC tokenized-stock order"}}
    assert context_entities.themed_followup("Does that automatically authorize Robinhood Chain today?", history, ctx) is None


def test_the_previous_request_survives_a_long_answer():
    # The bounded history text drops the user line after a long answer; the session context remembers it.
    ctx = experience.advance_session_context({}, "apart from staking how else can we tie dual token to the main token", None, "research", ["web_research"], [], None)
    assert ctx["last_request"] == "apart from staking how else can we tie dual token to the main token"
    history = "assistant: **Taken together**\n\nBurn-to-mint..." 
    resolved = context_entities.resolve_contextual_request("how it works in akash network", history, {"focus": None, "last_request": ctx["last_request"]})
    assert resolved.startswith("In Akash Network: apart from staking")
    corrected = context_entities.corrected_request("I mean SPX6900 the meme token, not the index.", "assistant: ...", {"last_request": "Check SPX."})
    assert corrected.startswith("Check the SPX6900 token.")


def test_the_general_research_hook_reads_the_resolved_request(monkeypatch):
    from app.nodes import general
    from app import evidence_pipeline
    seen = {}

    async def fake_answer(state, request, chains, **kw):
        seen["request"] = request
        return {"answer": "ok", "trajectory": None}

    monkeypatch.setattr(evidence_pipeline, "answer", fake_answer)
    monkeypatch.setattr(general.settings, "contract_pipeline_enabled", True, raising=False)
    state = {"request": "how it works in akash network", "contextual_request": "In Akash Network: apart from staking how else can we tie dual token to the main token?\nResolved from conversation context: ...",
             "routing_decision": {"speech_act": "explain", "domain": "crypto"}, "session_context": {}, "history": "", "capabilities": []}
    out = asyncio.run(general.general_node(state))
    assert seen.get("request", "").startswith("In Akash Network:"), seen


def test_a_pattern_question_is_scoped_to_the_mechanism_not_the_tokens_market():
    from app.routing.subject_probe import market_scoped
    q = "Which other projects follow the Venice VVV → DIEM pattern: lock the project token, mint a tradable second token?"
    scoped = market_scoped(q)
    assert "mechanism or design pattern" in scoped and "never answer with that token's price, vesting or unlock" in scoped
    plain = market_scoped("What is VVV?")
    assert "mechanism or design pattern" not in plain and "Read \"VVV\"" in plain
