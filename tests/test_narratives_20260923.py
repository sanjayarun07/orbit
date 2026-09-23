"""Narratives are themes, not DEX Screener's memecoin metas (user, 2026-09-23)."""
import asyncio

from app import dexscreener_tools
from app.nodes import research


def test_the_metas_card_says_what_it_is(monkeypatch):
    monkeypatch.setattr(dexscreener_tools, "_get", lambda path, params=None: [{"name": "Cat", "volume": 4.6e7, "liquidity": 6.5e7, "marketCap": 8.5e8, "priceChange24h": -0.8, "tokenCount": 97}])
    card = dexscreener_tools.dexscreener_trending_metas("trending narratives")
    assert card.startswith("# Trending memecoin metas on DEX Screener") and "not the market's narratives" in card


def test_a_narrative_ask_leads_with_the_market_read_and_keeps_the_metas_as_the_meme_slice(monkeypatch):
    assert research._NARRATIVE_ASK.search("What are the trending narratives right now?") and research._NARRATIVE_ASK.search("which narratives are driving crypto this week")
    assert research._DEX_SCOPED.search("trending memecoin narratives on dex screener") and not research._DEX_SCOPED.search("What are the trending narratives right now?")
    monkeypatch.setattr(research, "perplexity_available", lambda: True)
    monkeypatch.setattr(research, "perplexity_web_search", lambda q: "AI agents: sector volume +18% w/w (Kaiko, 2026-09-22). Perp DEXes: Hyperliquid fees record (DefiLlama, 2026-09-21).")
    monkeypatch.setattr(dexscreener_tools, "_get", lambda path, params=None: [{"name": "Cat", "volume": 4.6e7, "liquidity": 6.5e7, "marketCap": 8.5e8, "priceChange24h": -0.8, "tokenCount": 97}])
    seen = {}

    async def synth(request, cards, trajectory, advice=False):
        seen["request"], seen["cards"] = request, cards
        return "**Taken together**\n\nsummary\n\n---\n\n" + cards
    monkeypatch.setattr(research.composition, "synthesize", synth)
    out = asyncio.run(research._research_node({"request": "What are the trending narratives right now?", "contextual_request": None, "history": "", "session_context": {},
                                               "capabilities": ["market_data"], "chains": []}, {}))
    assert seen["cards"].index("# Market narratives this week") < seen["cards"].index("# Trending memecoin metas on DEX Screener")
    assert "narratives are themes drawing flows across the market, not tokens" in seen["request"]
    assert out["trajectory"]["tool_name_0"] == "perplexity_web_search" and out["trajectory"]["tool_name_1"] == "dexscreener_trending_metas"


def test_a_dex_scoped_metas_ask_reaches_the_metas_tool():
    from app.provider_registry import get_provider_router
    tool = next(t for t in get_provider_router().tools() if t.name == "dexscreener_trending_metas")
    assert tool.matches("trending memecoin metas on dex screener") and tool.matches("meme narratives on dex screener")
    assert not tool.matches("trending memecoins on solana")
