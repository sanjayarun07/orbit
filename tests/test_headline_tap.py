"""A Home headline tap is grounded in the market's state: the pulse card
joins the story, and a policy headline also gets the social read (the one
place Minara visibly beat the product on its own Home screen, 2026-09-25)."""
from __future__ import annotations

import asyncio

from app import composition, evidence_pipeline, market_overview
from tests.test_contract_pipeline import FakeRouter

PULSE = "# Market pulse\n**Retrieved** 2026-09-26 09:00 UTC\n\n- **BTC** $84,700 (−0.8% 24h)\n- **ETH** $2,711 (+0.3% 24h)\n\n- Fear & Greed: 73 (Greed)\n- Total market cap: $3.1T"
WEB = "# From the web (dated, with sources)\n**Query**: q\n\nThe Federal Reserve requested comment on two stablecoin proposals on September 24, 2026. [1]\n\nSources:\n[1] [Fed](https://federalreserve.gov/x) · 2026-09-24"
SOCIAL = "# X social trending\n- Posts about stablecoin rules: mostly relieved, some accounts call it a crackdown."


def _run(monkeypatch, request: str, outputs: dict, pulse: str = PULSE):
    monkeypatch.setattr(evidence_pipeline.settings, "contract_pipeline_enabled", True)
    from app.nodes import runtime
    monkeypatch.setattr(runtime, "planner_available", lambda: False)
    monkeypatch.setattr(market_overview, "market_pulse_card", lambda: pulse)
    seen = {}

    async def synth(req, cards, trajectory, advice=False, research=False):
        seen["request"] = req
        return f"**Taken together**\n\nA read.\n\n---\n\n{cards}"
    monkeypatch.setattr(evidence_pipeline.composition, "synthesize", synth)
    router = FakeRouter(outputs)
    saved = evidence_pipeline.get_provider_router
    evidence_pipeline.get_provider_router = lambda: router
    try:
        out = asyncio.run(evidence_pipeline.answer({}, request, ()))
    finally:
        evidence_pipeline.get_provider_router = saved
    return out, router, seen


def test_a_headline_tap_is_recognised_on_the_ask_line_only():
    assert composition.is_headline_tap("What does this mean for the market: Fed proposes tougher stablecoin rules")
    assert composition.is_headline_tap("What does this mean for memecoins: Solana gainers lead as BONK rallies")
    assert not composition.is_headline_tap("Fed proposes tougher stablecoin rules\nResolved from conversation context: What does this mean for the market: x")
    assert not composition.is_headline_tap("What happened to Solana in the last 24 hours?")


def test_a_policy_headline_gets_the_pulse_and_the_social_read(monkeypatch):
    out, router, seen = _run(monkeypatch, "What does this mean for the market: Fed proposes tougher stablecoin rules as crypto stays volatile",
                             {"perplexity_web_search": WEB, "x_social_trending": SOCIAL})
    assert out is not None and out["contract"]["kind"] == "open_research"
    answer = out["answer"]
    assert "# Market pulse" in answer and "Fear & Greed: 73" in answer and "# X social trending" in answer
    assert "x_social_trending" in router.calls
    assert "grounded in the market pulse card and the social read" in seen["request"] and "never its publication date" in seen["request"]
    assert "No prediction, no target, no advice." in seen["request"]


def test_a_market_headline_gets_the_pulse_but_not_the_social_read(monkeypatch):
    out, router, seen = _run(monkeypatch, "What does this mean for memecoins: Solana gainers lead as BONK rallies",
                             {"perplexity_web_search": WEB, "x_social_trending": SOCIAL})
    assert "# Market pulse" in out["answer"] and "# X social trending" not in out["answer"]
    assert "x_social_trending" not in router.calls
    assert "grounded in the market pulse card:" in seen["request"]


def test_an_unavailable_pulse_leaves_the_tap_on_the_story(monkeypatch):
    out, router, seen = _run(monkeypatch, "What does this mean for memecoins: Solana gainers lead as BONK rallies", {"perplexity_web_search": WEB}, pulse="")
    assert "# Market pulse" not in out["answer"] and "Home headline tap" not in seen["request"]
    assert "September 24, 2026" in out["answer"]


def test_an_ordinary_research_question_never_gets_the_pulse(monkeypatch):
    out, router, seen = _run(monkeypatch, "How does Akash AKT and ACT compare? Is ACT actually transferable?", {"perplexity_web_search": WEB})
    assert "# Market pulse" not in out["answer"] and "x_social_trending" not in router.calls


def test_the_pulse_card_is_two_sections_or_nothing(monkeypatch):
    calls = []

    def fake_get(url):
        calls.append(url)
        if "simple/price" in url:
            return {"bitcoin": {"usd": 84700, "usd_24h_change": -0.8}, "ethereum": {"usd": 2711, "usd_24h_change": 0.3}, "solana": {"usd": 118.8, "usd_24h_change": 1.1}}
        if "/global" in url:
            return {"data": {"total_market_cap": {"usd": 3.1e12}, "market_cap_change_percentage_24h_usd": -0.5, "market_cap_percentage": {"btc": 55.1, "eth": 12.3}}}
        if "fng" in url:
            return {"data": [{"value": "73", "value_classification": "Greed"}]}
        raise RuntimeError("down")
    monkeypatch.setattr(market_overview, "_get_json", fake_get)
    market_overview._cache.clear()
    card = market_overview.market_pulse_card()
    assert card.startswith("# Market pulse") and "| BTC |" in card and "Fear & Greed**: 73" in card and "Top movers" not in card and "Trending" not in card
    assert "not a reading of the headline" in card
    monkeypatch.setattr(market_overview, "_get_json", lambda url: (_ for _ in ()).throw(RuntimeError("down")))
    market_overview._cache.clear()
    assert market_overview.market_pulse_card() == ""


def test_an_off_topic_social_read_is_dropped_not_shown(monkeypatch):
    noise = "# X social trending\n- @UsePaid posting bullishly about PAID\n- @top7ico on FOGO and XPL unlocks"
    out, router, seen = _run(monkeypatch, "What does this mean for the market: Fed proposes tougher stablecoin rules as crypto stays volatile",
                             {"perplexity_web_search": WEB, "x_social_trending": noise})
    assert "x_social_trending" in router.calls and "# X social trending" not in out["answer"]
    assert "grounded in the market pulse card:" in seen["request"] and "social read" not in seen["request"]
    assert evidence_pipeline._about_headline("Fed proposes tougher stablecoin rules", "posts about stablecoin issuers")
    assert not evidence_pipeline._about_headline("Fed proposes tougher stablecoin rules", "posts about PAID and XPL unlocks")
