"""The question contract, hard eligibility, typed facts, the fact gate and
the pipeline that joins them (2026-09-23). No network: a fake router."""
import asyncio
from datetime import datetime, timezone
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
    assert not g.ok and g.missing[0].startswith("an event inside the last 30 days (the newest dated event is 2025-01-14)")


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
