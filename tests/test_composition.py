"""A request that needs several tools gets all of them, combined and read
together (user rule, 2026-09-17). Three failures from one transcript:
"check trending tokens... check whale activity" answered only the first;
"suggest crypto for day trading based on technicals, news, sentiment" got a
generic list and "no live data"; "trending tokens" was answered with paid
boosts.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import composition
from app.nodes import research as research_mod
from app.routing import intent_router


# --- splitting and combining ---------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("check trending tokens and let me know. check whale activity", ["check trending tokens", "check whale activity"]),
    ("today market trend on crypto. why zec is pumping", ["today market trend on crypto", "why zec is pumping"]),
    ("price and volume of BONK", ["price and volume of BONK"]),          # a bare "and" is one ask
    ("top holders of BONK. also its liquidity", ["top holders of BONK", "its liquidity"]),
    ("hi", ["hi"]),
])
def test_asks_split_at_sentence_boundaries_and_connectors_only(text, expected):
    assert composition.split_asks(text) == expected


def test_combine_merges_cards_and_renumbers_their_trajectories():
    answer, traj = composition.combine([
        ("# A\ncard a", {"tool_name_0": "x", "observation_0": "ax", "tool_name_1": "y", "observation_1": "ay"}),
        ("", None),
        ("# B\ncard b", {"tool_name_0": "z", "observation_0": "bz"}),
    ])
    assert answer == "# A\ncard a\n\n---\n\n# B\ncard b"
    assert traj == {"tool_name_0": "x", "observation_0": "ax", "tool_name_1": "y", "observation_1": "ay", "tool_name_2": "z", "observation_2": "bz"}


# --- the compound message, end to end through the research node -------------------

def test_a_compound_message_answers_every_ask_and_reads_them_together(monkeypatch):
    seen = []

    async def per_clause(state, sink):
        seen.append(state["request"])
        if "trending" in state["request"]:
            return {"answer": "# Top volume\n| BTC | ... |", "trajectory": {"tool_name_0": "coingecko_top_volume", "observation_0": "volume"}}
        return await original(state, sink)   # the whale clause reaches the real clarifier

    original = research_mod._research_node
    monkeypatch.setattr(research_mod, "_research_node", per_clause)
    monkeypatch.setattr(composition.runtime, "_call_synthesis_lm", AsyncMock(return_value=SimpleNamespace(summary="Volume leaders are the majors; whale activity needs a token.")))
    out = asyncio.run(research_mod.research_node({"request": "check trending tokens and let me know. check whale activity",
                                                  "capabilities": ["token_discovery", "market_data"], "chains": [], "history": "", "session_context": {}}))
    assert seen == ["check trending tokens", "check whale activity"]
    assert out["answer"].startswith("**Taken together**") and "# Top volume" in out["answer"]
    assert "Whale activity for which token or wallet?" in out["answer"], "the second ask was dropped again"
    assert out["trajectory"]["tool_name_0"] == "coingecko_top_volume"


def test_synthesis_failure_still_returns_the_cards(monkeypatch):
    monkeypatch.setattr(composition.runtime, "_call_synthesis_lm", AsyncMock(side_effect=RuntimeError("model down")))
    answer = asyncio.run(composition.synthesize("q", "# Card\nfacts", {}))
    assert answer == "# Card\nfacts"


# --- whale activity with no subject is asked about ----------------------------------

def test_whale_activity_without_a_token_is_a_question_not_a_web_search(monkeypatch):
    monkeypatch.setattr(research_mod, "_resolve_named_token", AsyncMock(side_effect=AssertionError("must not fall through")))
    out = asyncio.run(research_mod.research_node({"request": "check whale activity", "capabilities": ["web_research"], "chains": [], "history": "", "session_context": {}}))
    assert "which token or wallet" in out["answer"] and out["trajectory"] is None


# --- market-wide advice is composed from several tools ------------------------------

def test_a_market_wide_advice_ask_composes_movers_volume_sentiment_and_news(monkeypatch):
    invoked = []

    def invoke(tool, probe, chains=()):
        invoked.append(tool)
        return SimpleNamespace(output=f"# {tool}\nrows", tool=tool, provider="p")

    monkeypatch.setattr(composition, "get_provider_router", lambda: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(composition.runtime, "_call_synthesis_lm", AsyncMock(return_value=SimpleNamespace(summary="Majors lead volume; sentiment neutral; news quiet.")))
    out = asyncio.run(research_mod.research_node({"request": "suggest some crypto to buy for day trading based on technicals, news, sentiment",
                                                  "capabilities": ["finance_data", "web_research"], "chains": [], "history": "", "session_context": {}}))
    assert {"coingecko_gainers_losers", "coingecko_top_volume", "market_sentiment_snapshot", "perplexity_finance_search"} <= set(invoked)
    assert out["answer"].startswith("**Taken together**") and "Not financial advice" in out["answer"]
    assert out["trajectory"]["tool_name_0"] and "# coingecko_gainers_losers" in out["answer"]


def test_a_named_asset_advice_ask_is_not_the_market_wide_bundle(monkeypatch):
    monkeypatch.setattr(composition, "compose_market_advice", AsyncMock(side_effect=AssertionError("a named asset is a deep dive, not the market bundle")))
    monkeypatch.setattr(research_mod, "_run_token_deep_dive", AsyncMock(return_value={"answer": "lens", "trajectory": None}))
    out = asyncio.run(research_mod.research_node({"request": "should I buy BONK for day trading", "capabilities": ["token_discovery"], "chains": [], "history": "", "session_context": {}}))
    assert out["answer"] == "lens"
    assert research_mod._mentions_asset("should I day trade BONK") and not research_mod._mentions_asset("suggest some crypto to buy for day trading")


def test_market_wide_advice_routes_with_the_market_tools_in_reach():
    from app.routing.resolver import _speech_route
    from app.routing.semantic import SpeechUnderstanding
    route = _speech_route(SpeechUnderstanding(speech_act="advice", domain="crypto", explicit_action=False, confidence=0.95), "speech_model")
    assert {"market_data", "market_sentiment", "token_discovery", "news"} <= set(route["capabilities"]) and route["intent"] == "research"


# --- a bare "trending tokens" is organic, not paid boosts -------------------------------

def test_a_bare_trending_ask_ranks_the_volume_list_and_boosts_answer_attention_asks():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "routing_eval"))
    from harness import _ranked_tools
    bare = _ranked_tools("check trending tokens", semantic=False)
    assert bare and bare[3][0] == "coingecko_top_volume" and "dexscreener_boosted_tokens" not in bare[3], bare
    hot = _ranked_tools("hot tokens on solana right now", semantic=False)
    assert hot and hot[3][0] in {"dexscreener_boosted_tokens", "geckoterminal_pools"} and "dexscreener_boosted_tokens" in hot[3][:2], hot
    scoped = _ranked_tools("trending tokens on solana", semantic=False)
    assert scoped and scoped[3][0] in {"geckoterminal_pools", "dexscreener_boosted_tokens", "dexscreener_trending_metas"}, scoped
