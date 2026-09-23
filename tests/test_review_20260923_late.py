"""The late review of 2026-09-23 (three gate gaps) and the UI review the same
evening (citations cut at ten, the knowledge card's instruction, a wallet chip
from a URL, a brief cut mid-sentence)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import composition, context_entities, evidence_pipeline, fact_gate, facts, perplexity_tools
from app.contracts import QuestionContract, Subject, plan_by_rules

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _rows(n, **attrs):
    return [facts.Fact(kind="ranking_row", subject=f"T{i}", value=1.0, unit="pct", source="x", attrs=dict(attrs)) for i in range(n)]


def test_an_undated_fact_under_a_freshness_contract_is_not_current():
    c = plan_by_rules("top gainers on Hyperliquid in the last 24h")
    undated = _rows(5)
    g = fact_gate.check(c, undated, now=NOW)
    assert not g.ok and g.stale == ["x (no observation time stated)"] and g.missing == ["enough ranked rows for the asked scope (0 of 3)"]
    stamped = [r.model_copy(update={"observed_at": NOW.isoformat()}) for r in undated]
    assert fact_gate.check(c, stamped, now=NOW).ok


def test_a_live_state_fact_is_stamped_with_its_fetch_time_by_the_pipeline():
    card = "# Pairs up the most on Hyperliquid (1d)\n**Provider**: Tequity (internal feed)\n\n| # | Pair | Type | 24h change | 24h quote volume |\n|---|---|---|---:|---:|\n| 1 | A/USDC | perp | +5.0% | $1M |\n"
    rows = evidence_pipeline._facts_of(SimpleNamespace(tool="tequity_movers", output=card, structured=None), "market_ranking")
    assert rows and rows[0].observed_at is not None
    kb = evidence_pipeline._facts_of(SimpleNamespace(tool="knowledge_base_search", output=card, structured=None), "market_ranking")
    assert kb and kb[0].observed_at is None, "a knowledge passage is not stamped as fresh"


def test_a_source_without_a_date_is_not_the_dated_source_the_contract_asks_for():
    c = QuestionContract(kind="open_research", subject=Subject(kind="topic"), scope="any", metric="events", required_facts=["source"], freshness_seconds=7 * 86400)
    undated = facts.facts_from_search({"text": "Claim [1].", "sources": [{"n": 1, "title": "t", "url": "https://x", "date": None}]})
    assert not fact_gate.check(c, undated, now=NOW).ok
    dated = facts.facts_from_search({"text": "Claim [1].", "sources": [{"n": 1, "title": "t", "url": "https://x", "date": "2026-09-20"}]})
    assert fact_gate.check(c, dated, now=NOW).ok


def test_an_empty_required_list_still_requires_what_the_kind_means():
    c = QuestionContract(kind="holders", subject=Subject(kind="token", symbol="PEPE"), scope="on_chain", metric="holders", required_facts=[], freshness_seconds=3600)
    g = fact_gate.check(c, [], now=NOW)
    assert not g.ok and g.missing == ["holder positions (0 of 5)"]


def test_a_future_event_is_not_recent():
    c = plan_by_rules("what happened with Lido in the last 24 hours")
    assert c.kind == "recent_events"
    future = [facts.Fact(kind="event", subject="vote", source="perplexity_web_search", event_date="2026-10-01", attrs={})]
    g = fact_gate.check(c, future, now=NOW)
    assert not g.ok and "every dated event is in the future" in g.missing[0]
    tomorrow = [facts.Fact(kind="event", subject="vote", source="perplexity_web_search", event_date="2026-09-24", attrs={})]
    assert fact_gate.check(c, tomorrow, now=NOW).ok, "a date one day ahead can be today elsewhere"


def test_every_cited_source_is_listed_on_the_search_card():
    found = {"text": "A [11] and B [13] and C [2].", "sources": [{"n": i, "title": f"s{i}", "url": f"https://s/{i}", "date": None} for i in range(1, 16)]}
    card = perplexity_tools.render_search_card("q", found)
    assert "[11] [s11]" in card and "[13] [s13]" in card and "[12] [s12]" not in card and "[10] [s10]" in card


def test_the_knowledge_card_shows_passages_not_an_instruction():
    from app.knowledge import tool
    context = "[1] Aave · Docs › E-mode\n" + ("E-mode raises the liquidation threshold for correlated assets. " * 40) + "\n\n[2] Aave · Risk\nShort passage."
    out = tool._compact_passages(context)
    assert "Answer from these passages" not in out and out.startswith("**[1] Aave · Docs › E-mode**") and " …" in out and "Short passage." in out
    assert len(out) < 1200


def test_an_address_inside_a_source_url_is_not_a_wallet():
    history = "user: how does aave e-mode work\nassistant: See [Etherscan](https://etherscan.io/address/0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2) for the address."
    assert context_entities.extract_wallet_reference(history) is None


def test_a_brief_never_ends_mid_sentence():
    para = "First sentence is here. " * 30 + "The last one runs on and on but do "
    out = composition.brief(para.strip(), "", 60)
    body = out.split("\n\nSources:")[0]
    assert body.endswith("First sentence is here.") and "but do" not in body


def test_a_spelled_out_multiplier_is_the_same_figure_as_the_cards_suffix():
    rows = [facts.Fact(kind="event", subject="buyback", source="perplexity_web_search", event_date="2026-09-21", attrs={"text": "a $52.4M buyback"})]
    assert fact_gate.unsupported_figures("The vote approved a $52.4 million buyback and 1.2 billion in TVL.", rows, "a $52.4M buyback · $1.2B TVL") == []
    assert fact_gate.unsupported_figures("The vote approved a $60 million buyback.", rows, "a $52.4M buyback") == ["$60 million"]


def test_a_headline_tap_answered_by_the_general_node_is_cut_to_the_tap_limit(monkeypatch):
    import asyncio
    from app.nodes import general
    long = "**Taken together**\n\n" + ("A sentence of the market read. " * 200) + "\n\n---\n\n# From the web\n[1] [src](https://s/1)"

    async def fake_answer(state, request, chains, *, context=""):
        return {"answer": long, "trajectory": None, "pipeline": "contract"}
    monkeypatch.setattr(evidence_pipeline, "answer", fake_answer)
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    state = {"request": "What does this mean for the market: Bitcoin ETFs log $998.95 million daily inflow", "routing_decision": {"speech_act": "explain", "domain": "crypto"}, "session_context": {}}
    out = asyncio.run(general.general_node(state))
    body = out["answer"].split("\n\nSources:")[0]
    assert len(body.split()) <= composition.HEADLINE_TAP_WORDS + 5 and body.rstrip().endswith("read.")


def test_a_pipeline_lead_survives_the_strip_but_its_summary_does_not():
    piped = "**Not established: enough ranked rows (0 of 3).**\n\n**Taken together**\n\nsummary prose\n\n---\n\n# Card\n| a | b |\n"
    out = composition.strip_synthesis(piped)
    assert out.startswith("**Not established: enough ranked rows (0 of 3).**\n\n# Card") and "Taken together" not in out
    assert composition.strip_synthesis("**Taken together**\n\nx\n\n---\n\n# Card") == "# Card"
    assert composition.strip_synthesis("# Card only") == "# Card only"


def test_an_ask_naming_three_dimensions_of_one_token_is_a_deep_dive():
    from app import token_deepdive
    five = "Build an evidence table for Solana token 9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump: identity, holders, liquidity, deployer and security. Include sources, timestamps and missing data."
    assert token_deepdive.names_dimensions(five) >= 3
    assert token_deepdive.names_dimensions("Who are the top holders of PEPE?") == 1
    assert token_deepdive.names_dimensions("Find BONK pools on Solana with at least $1000000 liquidity") == 1
    assert token_deepdive.names_dimensions("How's the crypto market today?") == 0
