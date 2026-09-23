"""The question contract, hard eligibility, typed facts, the fact gate and
the pipeline that joins them (2026-09-23). No network: a fake router."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

STAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

import pytest

from app import contracts, evidence_pipeline, fact_gate, facts, tool_catalog
from app.contracts import QuestionContract, Subject, plan_by_rules

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)

GAINERS_BASE = ("# Gainers on Base\n**Data freshness**: " + STAMP + " · **Basis**: CoinGecko's Base ecosystem category\n\n"
                "| Token | Price | 24h change | 24h volume |\n|---|---:|---:|---:|\n| [AERO](https://x) | $1.20 | +12.5% | $45.2M |\n| [BRETT](https://x) | $0.08 | +9.1% | $12.0M |\n| [DEGEN](https://x) | $0.004 | +7.7% | $3.3M |\n")
HL_MOVERS = ("# Pairs up the most on Hyperliquid (1d)\n**Provider**: Tequity (internal feed) · **Snapshot**: " + STAMP + " · 3 pairs of 3 listed\n\n"
             "| # | Pair | Type | Last price | 24h price change | 24h quote volume |\n|---:|---|---|---:|---:|---:|\n| 1 | MET/USDC | perp | $0.40 | +36.52% | $7.8M |\n| 2 | TSLA/USDC | stock | $452.00 | +2.50% | $7.0M |\n| 3 | BTC/USDC | perp | $85,000.00 | -1.20% | $2.2B |\n")
PEPE_HOLDERS = ("# Token holders\n**Provider**: Mobula · **Contract**: `0x69` · **Chain**: ethereum · **Checked**: " + STAMP + "\n\n"
                "| # | Wallet | Share | Value | Buys/Sells | Unrealized PnL | First token trade | Wallet funded | Labels |\n|---:|---|---:|---:|---:|---:|---|---|---|\n"
                + "".join(f"| {i} | `0x{str(i) * 40}` | {8 - i}.00% | $1.0M | —/— | — | — | — | proTrader |\n" for i in range(1, 6))
                + "| 6 | `0x000000000000000000000000000000000000dEaD` | 1.64% | $1.0M | —/— | — | — | — | liquidityPool |\n")
USDC_YIELDS = ("# Yields: USDC on Base\n**Provider**: DeFiLlama · **Checked**: " + STAMP + "\n\n"
               "| Pool | Project | Chain | Exposure | APY | Base / reward | TVL | Notes |\n|---|---|---|---|---:|---:|---:|---|\n"
               "| USDC | aave-v3 | Base | single-asset | 4.10% | 4.10% / — | $50.0M | stablecoin |\n| USDC-WETH | aerodrome | Base | LP (multi-asset) | 414.00% | 10% / 404% | $30.0M | IL risk: yes |\n")
LIDO_WEB = "Lido's Snapshot vote on expense optimization and stETH buyback parameters opened on 2026-09-21 and runs to 2026-09-25.\n\nThe earlier BORG foundation vote concluded in 2025-01-14.\n\nSources: [research.lido.fi](https://research.lido.fi/t/x)"


class FakeRouter:
    replay = True

    def __init__(self, outputs: dict[str, str]):
        self.outputs = outputs
        self.calls: list[str] = []
        self._tools = [SimpleNamespace(name=n, priority=10.0, spec=tool_catalog.TOOL_SPECS.get(n), matches=lambda r: True, capabilities=("x",), risk="read_only")
                       for n in tool_catalog.CONTRACT_COVERAGE]

    def tools(self):
        return tuple(self._tools)

    def _enabled(self, tool):
        return True

    def invoke(self, name, request, chains=()):
        self.calls.append(name)
        out = self.outputs.get(name)
        return SimpleNamespace(output=out, tool=name, provider="fake") if out else None


@pytest.fixture(autouse=True)
def _pipeline(monkeypatch):
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: False)

    async def synth(request, cards, trajectory, advice=False):
        return f"**Taken together**\n\nsummary\n\n---\n\n{cards}"
    monkeypatch.setattr(evidence_pipeline.composition, "synthesize", synth)


def _run(outputs: dict[str, str], request: str):
    router = FakeRouter(outputs)
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, request, ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    return out, router


def test_the_rules_planner_reads_scope_window_and_filters():
    c = plan_by_rules("top gainers on Base in the last 24h")
    assert (c.kind, c.subject.chain, c.scope, c.metric, c.window_hours, c.direction) == ("market_ranking", "base", "venue_trades", "price_change", 24.0, "gainers")
    assert plan_by_rules("tokens in the Base ecosystem up the most today").scope == "global"
    assert plan_by_rules("Hyperliquid gainers excluding stocks").filters == {"crypto_only": True}
    assert plan_by_rules("top pools on Solana by volume with at least $1,000,000 liquidity").filters.get("min_liquidity_usd") == 1_000_000
    assert plan_by_rules("What did the Lido community vote on recently?").kind == "recent_events"
    assert plan_by_rules("price of BONK").kind == "other"


def test_eligibility_is_hard():
    base = plan_by_rules("top gainers on Base in the last 24h")
    assert tool_catalog.eligible("coingecko_gainers_losers", base) == (False, "scope is global, the ask needs trades on the venue")
    assert tool_catalog.eligible("coingecko_gainers_losers", plan_by_rules("tokens in the Base ecosystem up the most today"))[0]
    assert tool_catalog.eligible("tequity_movers", plan_by_rules("Hyperliquid gainers excluding stocks"))[0]
    assert tool_catalog.eligible("tequity_volume_leaders", plan_by_rules("most traded on aster in the last 2 hours"))[0]
    assert not tool_catalog.eligible("tequity_movers", plan_by_rules("biggest movers on hyperliquid this week"))[0]        # 24h feed cannot serve a week
    assert tool_catalog.eligible("tequity_period_movers", plan_by_rules("biggest movers on hyperliquid this week"))[0]
    assert tool_catalog.eligible("mobula_token_holders", plan_by_rules("Who are the top holders of PEPE?"))[0]
    assert not tool_catalog.eligible("mobula_token_holders", plan_by_rules("top gainers on Base"))[0]


def test_facts_are_typed_and_burn_addresses_classified():
    rows = facts.facts_from_card("mobula_token_holders", PEPE_HOLDERS, "holders")
    assert len(rows) == 6 and rows[-1].attrs["class"] == "burn" and rows[0].attrs["class"] == "wallet" and rows[0].value == 7.0
    y = facts.facts_from_card("defillama_yields", USDC_YIELDS, "yields")
    assert [(f.subject, f.attrs["exposure"], f.value) for f in y] == [("USDC", "single", 4.1), ("USDC-WETH", "lp", 414.0)]
    e = facts.facts_from_card("perplexity_web_search", LIDO_WEB, "recent_events")
    assert e[0].event_date == "2026-09-21" and facts.event_date_of(e[0]) == datetime(2026, 9, 21, tzinfo=timezone.utc)
    r = facts.facts_from_card("tequity_movers", HL_MOVERS, "market_ranking")
    assert [(f.subject, f.attrs["type"], f.value) for f in r] == [("MET/USDC", "perp", 36.52), ("TSLA/USDC", "stock", 2.5), ("BTC/USDC", "perp", -1.2)]
    assert r[0].observed_at is not None


def test_a_card_is_as_fresh_as_the_latest_stamp_on_its_provider_line():
    # A ledger card carries its baseline and its last tick; the last tick is
    # when the data was seen (Aster losers "232 min old", live 2026-09-23).
    card = ("# Pairs down the most on Aster since 2026-09-23 00:00 UTC\n"
            "**Provider**: Tequity tick ledger · **From tick**: 2026-09-23 10:53 UTC · **To tick**: 2026-09-23 14:56 UTC · 40 pairs present at both ends\n")
    assert facts.observed_at(card) == "2026-09-23T14:56:00+00:00"
    assert facts.observed_at("**Checked**: 2026-09-23 14:37:12 UTC") == "2026-09-23T14:37:12+00:00"


def test_the_gate_requires_facts_that_match_the_contract():
    c = plan_by_rules("Hyperliquid gainers excluding stocks")
    rows = facts.facts_from_card("tequity_movers", HL_MOVERS, "market_ranking")
    g = fact_gate.check(c, rows, now=NOW)
    assert not g.ok and g.missing == ["enough ranked rows for the asked scope (2 of 3)"]
    c = plan_by_rules("Find USDC yield on Base without exposing me to another volatile token")
    g = fact_gate.check(c, facts.facts_from_card("defillama_yields", USDC_YIELDS, "yields"), now=NOW)
    assert g.ok
    c = plan_by_rules("What did the Lido community vote on recently?")
    g = fact_gate.check(c, facts.facts_from_card("perplexity_web_search", LIDO_WEB, "recent_events"), now=NOW)
    assert g.ok
    g = fact_gate.check(c, facts.facts_from_card("knowledge_base_search", "The BORG foundation vote concluded on 2025-01-14 with 98% support.", "recent_events"), now=NOW)
    assert not g.ok and g.missing[0].startswith("an event inside the last 30 days (the newest past event is 2025-01-14)")


def test_unsupported_figures_are_named():
    rows = facts.facts_from_card("coingecko_gainers_losers", GAINERS_BASE, "market_ranking")
    assert fact_gate.unsupported_figures("AERO rose 12.5% on $45.2M of volume, while the index closed at 27,244.28.", rows) == ["27,244.28"]
    assert fact_gate.unsupported_figures("AERO rose 12.5% on $45.2M; 2026 has been strong.", rows) == []


def test_base_gainers_are_shown_as_the_nearest_verifiable_ranking_not_as_the_ask():
    out, router = _run({"coingecko_gainers_losers": GAINERS_BASE}, "top gainers on Base in the last 24h")
    assert router.calls == ["coingecko_gainers_losers"] and out["pipeline"] == "contract"
    assert out["answer"].startswith("**The exact scope asked for could not be verified from a source that covers it.")
    assert out["gate"]["scope_satisfied"] is False and "AERO" in out["answer"]


def test_hyperliquid_stocks_go_to_the_feed_and_pass_the_gate():
    out, router = _run({"tequity_movers": HL_MOVERS}, "which tokenized stocks are moving on hyperliquid")
    assert router.calls == ["tequity_movers"] and out["gate"]["scope_satisfied"] is True
    assert "could not be verified" not in out["answer"]


def test_a_recently_question_leads_with_discovery_and_needs_a_dated_recent_event():
    out, router = _run({"perplexity_web_search": LIDO_WEB, "knowledge_base_search": "BORG vote background [1]."}, "What did the Lido community vote on recently?")
    assert router.calls[0] == "perplexity_web_search" and out["gate"]["ok"] is True
    out, router = _run({"knowledge_base_search": "The BORG foundation vote concluded on 2025-01-14."}, "What did the Lido community vote on recently?")
    assert not out["gate"]["ok"] and "Not established: an event inside the last 30 days" in out["answer"]


def test_a_non_contract_ask_is_left_to_the_legacy_path():
    out, router = _run({}, "price of BONK")
    assert out is None and router.calls == []


def test_a_structured_search_keeps_claim_to_source_links():
    found = {"text": "Lido's Snapshot vote opened on 2026-09-21 [1]. The BORG vote concluded 2025-01-14 [2].",
             "sources": [{"n": 1, "title": "research.lido.fi", "url": "https://research.lido.fi/t/x", "date": "2026-09-21"}, {"n": 2, "title": "docs", "url": "https://docs.lido.fi", "date": None}]}
    rows = facts.facts_from_search(found)
    sources = [f for f in rows if f.kind == "source"]
    events = [f for f in rows if f.kind == "event"]
    assert len(sources) == 2 and sources[0].attrs["url"] == "https://research.lido.fi/t/x" and sources[0].event_date == "2026-09-21"
    assert events[0].attrs["cites"] == [1, 2] and events[0].attrs["traced"] and events[0].event_date == "2026-09-21"
    from app import perplexity_tools
    card = perplexity_tools.render_search_card("q", found)
    assert "[1] [research.lido.fi](https://research.lido.fi/t/x) · 2026-09-21" in card and "opened on 2026-09-21 [1]" in card


def test_open_research_is_taken_only_behind_the_flag(monkeypatch):
    assert plan_by_rules("Who are the investors backing EigenLayer?").kind == "open_research"
    assert plan_by_rules("Why is SOL moving today?").kind == "open_research"
    assert plan_by_rules("what is the price of BONK").kind == "other"
    monkeypatch.setattr(evidence_pipeline.settings, "discovery_first_research", False)
    out, router = _run({"perplexity_web_search": LIDO_WEB}, "Who are the investors backing EigenLayer?")
    assert out is None
    monkeypatch.setattr(evidence_pipeline.settings, "discovery_first_research", True)
    out, router = _run({"perplexity_web_search": LIDO_WEB}, "Who are the investors backing EigenLayer?")
    assert out is not None and router.calls[0] == "perplexity_web_search" and out["contract"]["kind"] == "open_research"


def test_stale_facts_satisfy_nothing_and_are_named():
    # Review 2026-09-23: stale evidence passed the gate as ok.
    c = plan_by_rules("top gainers on Hyperliquid in the last 24h")
    assert c.freshness_seconds
    old = (NOW - timedelta(seconds=c.freshness_seconds + 600)).isoformat()
    rows = [facts.Fact(kind="ranking_row", subject=f"T{i}/USDC", value=float(i), unit="pct", source="tequity_movers", observed_at=old, attrs={}) for i in range(5)]
    g = fact_gate.check(c, rows, now=NOW)
    assert not g.ok and g.stale == ["tequity_movers (70 min old)"] and g.missing == ["enough ranked rows for the asked scope (0 of 3)"]
    fresh = [r.model_copy(update={"observed_at": NOW.isoformat()}) for r in rows]
    assert fact_gate.check(c, fresh, now=NOW).ok


def test_a_filter_is_met_only_by_rows_that_state_the_property():
    # A global ranking with no type column does not establish tokenized stocks;
    # a row with no liquidity figure does not establish a liquidity floor.
    c = plan_by_rules("which tokenized stocks are moving on hyperliquid")
    untyped = [facts.Fact(kind="ranking_row", subject=f"T{i}", value=1.0, unit="pct", source="x", observed_at=NOW.isoformat(), attrs={"type": None}) for i in range(5)]
    g = fact_gate.check(c, untyped, now=NOW)
    assert not g.ok and g.missing == ["enough ranked rows for the asked scope that are tokenized stocks (0 of 5)"] or g.missing[0].endswith("that are tokenized stocks (0 of 3)")
    typed = [r.model_copy(update={"attrs": {"type": "stock"}}) for r in untyped]
    assert fact_gate.check(c, typed, now=NOW).ok
    c = plan_by_rules("top pools on Solana by volume with at least $1,000,000 liquidity")
    by_volume = [facts.Fact(kind="ranking_row", subject=f"P{i}", value=1.0, unit="pct", source="x", observed_at=NOW.isoformat(), attrs={"volume_usd": 5_000_000.0}) for i in range(5)]
    assert not fact_gate.check(c, by_volume, now=NOW).ok
    with_liq = [r.model_copy(update={"attrs": {"volume_usd": 5_000_000.0, "liquidity_usd": 2_000_000.0}}) for r in by_volume]
    assert fact_gate.check(c, with_liq, now=NOW).ok
    card = "| # | Pool | Volume 24h | Liquidity |\n|---|---|---|---|\n| 1 | BONK/SOL | $5.0M | $2.0M |\n"
    assert facts.facts_from_card("geckoterminal_pools", card, "market_ranking")[0].attrs["liquidity_usd"] == 2_000_000.0


def test_an_untraceable_figure_fails_the_gate_but_cards_totals_and_times_do_not():
    c = plan_by_rules("Who are the top holders of PEPE?")
    rows = facts.facts_from_card("mobula_token_holders", PEPE_HOLDERS, "holders")
    total = sum(r.value for r in rows)
    ok_text = f"The top {len(rows)} holders control {total:.2f}% as of 14:37 UTC on 2026-09-23; the largest holds {rows[0].value}%."
    assert fact_gate.check(c, rows, ok_text, now=NOW, evidence_text=PEPE_HOLDERS).ok
    g = fact_gate.check(c, rows, "The largest holder controls 41.7% of supply.", now=NOW, evidence_text=PEPE_HOLDERS)
    assert not g.ok and g.unsupported == ["41.7%"]
    assert fact_gate.check(c, rows, "Mobula lists 50 positions.", now=NOW, evidence_text=PEPE_HOLDERS + "\n50 positions read").ok


def test_an_explain_about_a_protocol_takes_the_open_research_contract(monkeypatch):
    # "How does Aave V3's E-mode change the liquidation threshold?" was an
    # "explain" act answered from the model's memory with no source (live,
    # 2026-09-23). The general node hands a crypto explain to the contract.
    from app.nodes import general
    seen = []

    async def fake_answer(state, request, chains, *, context=""):
        seen.append(request)
        return {"answer": "From the web [1]\n\nSources:\n[1] https://docs.aave.com", "trajectory": None, "pipeline": "contract"}
    monkeypatch.setattr(evidence_pipeline, "answer", fake_answer)
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    state = {"request": "How does Aave V3's E-mode change the liquidation threshold?", "routing_decision": {"speech_act": "explain", "domain": "crypto"}, "session_context": {}}
    out = asyncio.run(general.general_node(state))
    assert out["pipeline"] == "contract" and seen == [state["request"]]
    called = []

    async def no_lm(*a, **k):
        called.append(1)
        return SimpleNamespace(answer="hi")
    monkeypatch.setattr(general.runtime, "answer", no_lm)
    out = asyncio.run(general.general_node({"request": "thanks, that helps", "routing_decision": {"speech_act": "explain", "domain": "general"}, "session_context": {}}))
    assert out["answer"] == "hi" and called and len(seen) == 1, "chit-chat never reaches the contract"
