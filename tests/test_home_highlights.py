"""Welcome-screen highlights: news JSON parsed into four tiles, the keyless
market fallback, the static fallback, and the per-window cache."""
import pytest
from fastapi.testclient import TestClient

from app import home_highlights, main
from app.settings import settings


@pytest.fixture(autouse=True)
def _fresh():
    home_highlights.reset()
    yield
    home_highlights.reset()


NEWS = '''Here you go:
{"crypto":[{"headline":"Bitcoin tops $120k as spot ETF inflows hit a record","summary":"US spot BTC ETFs took in $1.2B on Monday.","source":"coindesk.com"},
{"headline":"Solana ETF decision pushed to October","summary":"The SEC extended its review window again.","source":"theblock.co"}],
"stocks":[{"headline":"Nvidia beats on revenue, guides higher","summary":"Q2 revenue $46B vs $45B expected; shares +5% after hours.","source":"reuters.com"},
{"headline":"Fed holds rates, signals one cut this year","summary":"The dot plot moved from two cuts to one.","source":"wsj.com"}]}'''


def test_news_answer_becomes_four_tiles(monkeypatch):
    monkeypatch.setattr(home_highlights, "perplexity_available", lambda: True)
    calls = []

    def fake_invoke(tool_type, prompt, instructions):
        calls.append((tool_type, prompt))
        return NEWS

    monkeypatch.setattr(home_highlights, "perplexity_invoke", fake_invoke)
    data = home_highlights.get_highlights()
    assert data["source"] == "news" and len(data["cards"]) == 4
    assert [c["kind"] for c in data["cards"]] == ["crypto", "crypto", "stocks", "stocks"]
    assert data["cards"][0]["title"].startswith("Bitcoin tops $120k") and data["cards"][0]["source"] == "coindesk.com"
    assert data["cards"][2]["prompt"].startswith("What does this mean for the market: ") and "Nvidia" in data["cards"][2]["prompt"]
    assert calls[0][0] == "web_search"
    # Second read within the window is served from cache -- no second paid call.
    home_highlights.get_highlights()
    assert len(calls) == 1
    assert len(home_highlights.get_highlights(force=True)["cards"]) == 4 and len(calls) == 2


def test_market_fallback_when_news_is_unavailable(monkeypatch):
    monkeypatch.setattr(home_highlights, "perplexity_available", lambda: False)
    payloads = {
        home_highlights.market_overview._SIMPLE_URL: {"bitcoin": {"usd": 118250.4, "usd_24h_change": 2.31}},
        home_highlights.market_overview._MARKETS_URL: [
            {"name": "Jupiter", "symbol": "jup", "total_volume": 50_000_000, "price_change_percentage_24h": 12.4},
            {"name": "Thin Coin", "symbol": "thin", "total_volume": 10, "price_change_percentage_24h": 900.0},
        ],
        home_highlights.market_overview._TRENDING_URL: {"coins": [{"item": {"name": "Bonk", "symbol": "bonk"}}]},
    }
    monkeypatch.setattr(home_highlights.market_overview, "_get_json", lambda url: payloads[url])
    data = home_highlights.get_highlights()
    assert data["source"] == "market"
    titles = [c["title"] for c in data["cards"]]
    assert titles[0] == "Bitcoin $118,250 · +2.3% in 24h"
    assert titles[1] == "Jupiter (JUP) +12.4% today"  # illiquid 900% mover ignored
    assert titles[2] == "Trending now: Bonk (BONK)" and data["cards"][2]["prompt"] == "Is BONK safe to ape into?"
    assert data["cards"][3]["kind"] == "stocks"


def test_placeholder_news_is_not_a_tile():
    text = '{"crypto":[{"headline":"Live crypto news unavailable","summary":"No current results.","source":"unknown"}],"stocks":[{"headline":"Fed holds","summary":"Rates unchanged at 4.25%.","source":"wsj.com"}]}'
    parsed = home_highlights._parse_news(text)
    assert parsed == {"crypto": [], "stocks": [{"headline": "Fed holds", "summary": "Rates unchanged at 4.25%.", "source": "wsj.com"}], "memes": []}
    assert home_highlights._parse_news('{"crypto":[],"stocks":[]}') is None


def test_unparseable_news_and_dead_market_fall_back_to_static(monkeypatch):
    monkeypatch.setattr(home_highlights, "perplexity_available", lambda: True)
    monkeypatch.setattr(home_highlights, "perplexity_invoke", lambda *a: "Sorry, I can't help with that.")

    def boom(url):
        raise RuntimeError("offline")

    monkeypatch.setattr(home_highlights.market_overview, "_get_json", boom)
    data = home_highlights.get_highlights()
    assert data["source"] == "market"  # the stocks prompt card still builds
    assert [c["id"] for c in data["cards"]] == ["stocks", "portfolio", "balances", "security"]


def test_endpoint_is_public_and_cached(monkeypatch):
    monkeypatch.setattr(home_highlights, "perplexity_available", lambda: True)
    monkeypatch.setattr(home_highlights, "perplexity_invoke", lambda *a: NEWS)
    client = TestClient(main.app)
    first = client.get("/home/highlights")
    assert first.status_code == 200 and first.json()["source"] == "news"
    assert client.get("/home/highlights").json() == first.json()


def test_tile_prompt_routes_to_web_research_not_a_token_lookup():
    """Live regression: the tile prompt "Tell me more about this and why it
    matters for the market: Bitcoin ETFs lose $450M as CLARITY Act stalls" was
    answered by DEX pair search (an empty table) because "Bitcoin" read as a
    token. It is a headline explainer: anchored to web research."""
    from app.routing import intent_router, resolver

    prompt = "Tell me more about this and why it matters for the market: Bitcoin ETFs lose $450M as CLARITY Act stalls"
    route = intent_router.route_capabilities(prompt)
    assert route is not None and route.reason == "news_explainer" and route.intent == "research" and route.capabilities == ("web_research",)
    assert "news_explainer" in resolver._ANCHORED_REASONS
    for other in ("explain this headline: SOL hits new high", "what does this mean: Fed holds rates",
                  "What does this mean for the market: Bitcoin and Ethereum ETFs lose a combined $592 million",   # the tile + chip template
                  "What does this mean for the market: Senate blocks CLARITY Act in 49–50 procedural vote?"):
        assert intent_router.route_capabilities(other).reason == "news_explainer", other
    # Plain token asks are untouched.
    assert (intent_router.route_capabilities("price of BONK") or type("r", (), {"reason": None})).reason != "news_explainer"
