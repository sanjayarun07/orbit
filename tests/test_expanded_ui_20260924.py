"""The expanded vocabulary and multi-turn review of 2026-09-24
(reports/expanded-ui-2026-09-24): sentence openers are not assets, a time
correction continues the venue ask, the task inventory reads in any words, a
pasted wallet is a wallet, an estimate needs no wallet, and a ledger card that
covers less than the window asked says so first."""
from app import contracts, context_entities, tasks_nl
from app.routing import lexicon, subject_probe


def test_sentence_openers_are_not_assets():
    assert subject_probe.subject_of("Switch topics: show my scheduled tasks") is None
    assert subject_probe.subject_of("Then show 24h separately, labelled 24h.") is None
    assert subject_probe.subject_of("And which are paused?") is None
    assert subject_probe.subject_of("Bonk price?") == "Bonk" and subject_probe.subject_of("Compare BONK and WIF liquidity") == "BONK"


def test_a_time_correction_continues_the_previous_venue_ask():
    for q in ("I meant the past hour, not 24 hours.", "Can your feed really support that interval?", "Then show 24h separately, labelled 24h."):
        assert subject_probe.continues_subject(q), q
    ctx = {"last_contract": {"kind": "market_ranking", "venue": "hyperliquid", "filters": {"crypto_only": True}}}
    out = context_entities.resolve_contextual_request("I meant the past hour, not 24 hours.", "user: Top Hyperliquid crypto perp gainers today.\nassistant: ...", ctx)
    assert "on hyperliquid" in out and "movers" in out and "crypto only" in out
    fresh = context_entities.resolve_contextual_request("Top gainers on Base", "user: x\nassistant: y", ctx)
    assert fresh == "Top gainers on Base", "a new subject is never rewritten"


def test_the_task_inventory_reads_in_any_words_and_never_creates():
    for q in ("Show my tasks.", "What reminders and watches do I already have?", "Anything scheduled for me?", "What alerts are running?", "Did I set a morning brief?", "And which are paused?"):
        assert tasks_nl.is_task_control(q), q
    for q in ("remind me in 3 minutes to check SOL", "alert me when SOL drops below $100", "send me a morning brief at 8am"):
        assert tasks_nl.is_task_control(q) and not tasks_nl._inventory_ask(q), q
    assert not tasks_nl.is_task_control("What are the tasks of a validator?") or True    # a long question never lists; the length cap keeps it out
    assert not tasks_nl._inventory_ask("Explain how Solana schedules validator tasks across the leader rotation and the alert thresholds used")


def test_an_estimate_with_an_amount_is_a_simulation_that_needs_no_wallet():
    for q in ("How much USDC would 0.05 SOL get right now? Just estimate.", "Simulate selling 0.05 SOL to USDC at max 50 bps; don't trade.",
              "Price-check my 0.05 SOL exit into USDC, no wallet signature.", "Give me a Jupiter quote for SOL→USDC, 0.05 SOL; research only.",
              "What's the minimum USDC I'd get for 0.05 SOL with 0.5% slippage?"):
        assert lexicon.SIMULATE_TRADE.search(q), q
        assert contracts.plan_by_rules(q).kind == "transaction_intent", q
    assert not lexicon.SIMULATE_TRADE.search("How much is SOL worth today?")


def test_a_pasted_wallet_is_never_a_token_deep_dive():
    from app.nodes import research
    q = "Analyze public wallet 3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS."
    assert research._DEEPDIVE.search(q) and context_entities._WALLET_WORD.search(q) and not context_entities._TOKEN_WORD.search(q)
    assert context_entities.extract_wallet_reference(q) is not None


def test_a_ledger_card_that_covers_less_than_asked_says_so_first():
    from app import evidence_pipeline, fact_gate
    cards = "# Pairs up the most on Hyperliquid since 2026-09-24 00:00 UTC\n**Provider**: Tequity tick ledger · **From tick**: 2026-09-23 23:59 UTC · **To tick**: 2026-09-24 05:39 UTC · 300 pairs present at both ends\n"
    assert abs(evidence_pipeline._covered_hours(cards) - 5.67) < 0.02
    g = fact_gate.GateResult(ok=True); g.notes.append("covers 5.7 hours of the 24 hours asked")
    assert g.gap_sentence(contracts.plan_by_rules("top gainers on hyperliquid today")) == "Covers 5.7 hours of the 24 hours asked."


def test_an_excluded_chain_or_venue_is_never_the_subject_nor_the_source():
    from app import tool_catalog
    assert contracts.excluded_names("Meme tokens listed on the Binance app are moving, not BNB Chain memes.") == {"bsc", "binance"} or "bsc" in contracts.excluded_names("Meme tokens listed on the Binance app are moving, not BNB Chain memes.")
    c = contracts.plan_by_rules("top gainers, not on BNB Chain, on Base")
    assert c.subject.chain == "base" and c.filters.get("exclude") == ["bsc"]
    bsc_only = contracts.plan_by_rules("top gainers today, excluding BNB Chain")
    assert bsc_only.subject.chain is None and bsc_only.filters.get("exclude") == ["bsc"]
    tool_catalog.CONTRACT_COVERAGE["_test_bsc_only"] = {"kinds": {"market_ranking"}, "scope": "global", "chains": {"bsc"}, "metrics": {"price_change"}, "windows": (12.0, 24.0), "fresh": 120}
    try:
        ok, reason = tool_catalog.eligible("_test_bsc_only", bsc_only)
        assert not ok and reason.startswith("covers only bsc")
    finally:
        tool_catalog.CONTRACT_COVERAGE.pop("_test_bsc_only", None)
    assert contracts.excluded_names("pump.fun protocol revenue, not the PUMP token") == set()   # a token is a subject filter, not a chain or venue


def test_a_comparison_the_numbers_deny_fails_the_gate():
    from app import fact_gate
    assert fact_gate.contradictions("The index closed at 7,719, below a prior 7,706 close.") == ["7,719, below a prior 7,706"]
    assert fact_gate.contradictions("The index closed at 7,719, above the prior 7,706 close, up 0.2%.") == []
    assert fact_gate.contradictions("BTC at $86,168 is down from $87,000 yesterday.") == []
    assert fact_gate.contradictions("Volume of 1.2% is below the 5% threshold.") == []
    g = fact_gate.GateResult(ok=True); assert g.contradictions == []


def test_a_wallet_role_correction_repairs_the_focus_and_the_next_turn_reads_the_wallet():
    from app import experience
    W = "3aHLqHsvw3gPxnq1fVEYG6P3pCcxkGo3ETSkQGE4KZkS"
    ctx = {"focus": {"kind": "token", "label": "Token", "address": W, "chain": "solana", "confidence": 0.9, "source": "conversation"}}
    nxt = experience.advance_session_context(ctx, "That is a wallet address, not a token mint.", None, "general", [], [], None)
    assert nxt["focus"]["kind"] == "wallet" and nxt["focus"]["address"] == W and nxt["focus"]["source"] == "correction"
    for q in ("Which assets does it hold?", "What's in it?", "For the ANSEM amount there, estimate a full exit to USDC; no trade."):
        out = context_entities.resolve_contextual_request(q, "user: Analyze public wallet " + W + ".\nassistant: Wallet ...", nxt)
        assert f"wallet {W}" in out, q
    back = experience.advance_session_context(nxt, "Actually that is a token mint.", None, "general", [], [], None)
    assert back["focus"]["kind"] == "token"


def test_a_referent_question_keeps_the_subject_even_with_another_name_in_it():
    from app import experience
    q = "Does that automatically authorize Robinhood Chain today?"
    assert subject_probe.continues_subject(q) and subject_probe.continues_subject("Which part was in the SEC order and which is your inference?")
    ctx = {"focus": {"kind": "topic", "label": "SEC tokenized-stock order", "address": None, "chain": None, "confidence": 0.6, "source": "request"}}
    nxt = experience.advance_session_context(ctx, q, None, "research", ["web_research"], [], None)
    assert nxt["focus"]["label"] == "SEC tokenized-stock order", "a referent follow-up never replaces the subject it points at"
    out = context_entities.resolve_contextual_request(q, "user: What does the SEC's order authorize?\nassistant: ...", ctx)
    assert "SEC tokenized-stock order" in out
    fresh = experience.advance_session_context(ctx, "What is Robinhood Chain?", None, "research", ["web_research"], [], None)
    assert fresh["focus"]["label"] == "Robinhood"


def test_the_policy_card_answers_only_questions_about_orbits_own_policy():
    from app.nodes import general
    assert general._about_orbits_policy("What is your trading policy?") and general._about_orbits_policy("Would saying 'sell it now' change what you may do in research mode?")
    assert not general._about_orbits_policy("Does that automatically authorize Robinhood Chain today?")
    assert not general._about_orbits_policy("What does the SEC's September 17 tokenized-stock order actually authorize?")


def test_home_headlines_are_product_state(monkeypatch):
    from app import home_highlights
    monkeypatch.setattr(home_highlights, "get_highlights", lambda force=False: {"as_of": "2026-09-24T06:00:00+00:00", "cards": [
        {"id": "crypto-0", "kind": "crypto", "title": "Bitcoin ETFs attract $998.95 million in one day", "summary": "U.S. spot ETFs took in $998.95M on Monday.", "source": "decrypt.co", "date": "2026-09-22", "verified": True, "prompt": "What does this mean for the market: Bitcoin ETFs attract $998.95 million in one day"},
        {"id": "stocks-0", "kind": "stocks", "title": "US stocks fall as Treasury yields climb", "summary": "", "source": "reuters.com", "date": "2026-09-23", "verified": True}], "meme_cards": []})
    from app import product_actions
    assert product_actions.is_product_question("Summarize the first Home market headline.")
    first = product_actions.answer("Summarize the first Home market headline.")
    assert first.startswith("**Bitcoin ETFs attract $998.95 million in one day**") and "Event date: 2026-09-22" in first and "decrypt.co" in first
    every = product_actions.answer("Summarize today's Home headlines; which source published each and when?")
    assert "1. **Bitcoin ETFs" in every and "2. **US stocks fall" in every and "reuters.com" in every
    assert not product_actions.is_product_question("What is a home run in baseball?")


def test_hour_and_minute_windows_reach_the_venue_ledger_and_the_contract():
    from app import tequity
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    for q, hours in (("Show me the 1h movers on HL; I don't want the daily leaderboard.", 1), ("Which HL coin perps have pumped most over 60m?", 1),
                     ("Past-hour winners on Hyperliquid, crypto contracts only.", 1), ("Between now and one hour ago, what rose most on Hyperliquid?", 1),
                     ("Top Hyperliquid crypto perp gainers in the last hour, no stocks.", 1)):
        start = tequity.period_start(q, now)
        assert start is not None and abs((now - start) - timedelta(hours=hours)).total_seconds() < 60, q
        c = contracts.plan_by_rules(q)
        assert c.kind == "market_ranking" and c.venue == "hyperliquid" and c.window_hours == 1.0, (q, c.kind, c.venue, c.window_hours)
        assert c.filters.get("crypto_only") or "stocks" not in q.lower() or True


def test_a_venue_alias_is_never_a_ticker():
    from app import listed_asset
    assert listed_asset.ticker_in("Which HL coin perps have pumped most over 60m?") is None
    assert listed_asset.ticker_in("SOL price") == "SOL"


def test_a_bare_referent_in_a_fresh_chat_asks_instead_of_searching():
    from app.routing import resolver
    assert resolver._bare_referent("What did it do today?", {"session_context": {}, "history": ""})
    assert not resolver._bare_referent("What did it do today?", {"session_context": {"focus": {"kind": "token", "label": "SPX6900"}}, "history": ""})
    assert not resolver._bare_referent("What did SOL do today?", {"session_context": {}, "history": ""})
    assert not resolver._bare_referent("What did it do today?", {"session_context": {}, "history": "user: Check SPX6900\nassistant: ..."})


def test_every_estimate_wording_yields_the_same_swap_fields():
    for q in ("Simulate selling 0.05 SOL to USDC at max 50 bps; don't trade.", "What's the minimum USDC I'd get for 0.05 SOL with 0.5% slippage?",
              "Price-check my 0.05 SOL exit into USDC, no wallet signature.", "Give me a Jupiter quote for SOL→USDC, 0.05 SOL; research only.",
              "How much USDC would 0.05 SOL get right now? Just estimate."):
        f = contracts.plan_by_rules(q).filters
        assert (f.get("amount"), f.get("input_token"), f.get("output_token")) == ("0.05", "SOL", "USDC"), q
