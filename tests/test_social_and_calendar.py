"""X / KOL sentiment tool (both data paths) and the market event calendar."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import event_calendar, home_highlights, main, social_sentiment
from app.nodes import research as research_mod
from app.provider_registry import get_provider_router
from app.settings import settings


@pytest.fixture(autouse=True)
def _fresh():
    social_sentiment.reset()
    event_calendar.reset()
    home_highlights.reset()
    yield
    social_sentiment.reset()
    event_calendar.reset()
    home_highlights.reset()


def test_symbol_extraction_and_trigger():
    assert social_sentiment.extract_symbol("what is crypto twitter saying about BONK") == "BONK"
    assert social_sentiment.extract_symbol("KOL sentiment on $jup today") == "JUP"
    assert social_sentiment.extract_symbol("what are people saying on X") is None
    assert social_sentiment.matches("what are KOLs saying about SOL")
    assert not social_sentiment.matches("price of SOL")


def test_perplexity_path_renders_ranked_posts(monkeypatch):
    monkeypatch.setattr(settings, "x_bearer_token", None)
    monkeypatch.setattr(social_sentiment, "perplexity_available", lambda: True)
    payload = {"sentiment": "bullish", "score": 0.6, "summary": "Traders are positioning for the ETF decision.",
               "posts": [{"author": "@hsaka", "reach": "300k followers", "stance": "bullish", "summary": "Called the breakout.", "url": "https://x.com/hsaka/status/1"},
                         {"author": "@fake", "reach": "?", "stance": "bearish", "summary": "Rug fears.", "url": "https://evil.example/phish"}],
               "narratives": ["ETF approval", "meme season"], "risks": ["insider selling"]}
    calls = []
    monkeypatch.setattr(social_sentiment, "perplexity_invoke", lambda tool, prompt, instr: calls.append((tool, prompt)) or json.dumps(payload))
    card = social_sentiment.x_kol_sentiment("what is crypto twitter saying about BONK")
    assert card.startswith("# X / KOL sentiment — BONK") and "**bullish** (score +0.60)" in card
    assert "[@hsaka](https://x.com/hsaka/status/1)" in card and "| @fake |" in card and "evil.example" not in card  # non-X links dropped
    assert "**Narratives**: ETF approval · meme season" in card and "insider selling" in card
    assert calls[0][0] == "web_search" and "$BONK" in calls[0][1]
    social_sentiment.x_kol_sentiment("KOL sentiment on BONK")
    assert len(calls) == 1  # cached per symbol


def test_x_api_path_weights_stance_by_reach(monkeypatch):
    monkeypatch.setattr(settings, "x_bearer_token", "xoxb-test")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [
                {"id": "1", "author_id": "a", "text": "$SOL looks bullish, breakout incoming", "public_metrics": {"like_count": 500, "retweet_count": 100}},
                {"id": "2", "author_id": "b", "text": "$SOL is going to dump, sell now", "public_metrics": {"like_count": 5, "retweet_count": 0}},
            ], "includes": {"users": [
                {"id": "a", "username": "bigwhale", "public_metrics": {"followers_count": 400000}},
                {"id": "b", "username": "smallfry", "public_metrics": {"followers_count": 120}},
            ]}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            assert headers["Authorization"] == "Bearer xoxb-test" and "$SOL" in params["query"]
            return FakeResponse()

    monkeypatch.setattr(social_sentiment.httpx, "Client", FakeClient)
    card = social_sentiment.x_kol_sentiment("what are KOLs saying about SOL")
    assert "**Provider**: X API v2" in card and "**bullish**" in card and "1 bullish · 1 bearish" in card
    assert card.index("@bigwhale") < card.index("@smallfry")  # ranked by reach


def test_sentiment_tool_is_reachable_through_the_router(monkeypatch):
    monkeypatch.setattr(settings, "perplexity_api_key", "pplx-test")
    router = get_provider_router()
    ranked = [t.name for t in router._ranked_union("what is crypto twitter saying about BONK", ("market_sentiment",), (), None)]
    assert ranked[0] == "x_kol_sentiment"
    assert "market_sentiment" in router.matched_capabilities("what are KOLs saying about SOL")


def test_calendar_parses_dates_inside_the_window_only(monkeypatch):
    today = datetime.now(timezone.utc).date()
    monkeypatch.setattr(event_calendar, "perplexity_available", lambda: True)
    payload = {"events": [
        {"date": (today + timedelta(days=1)).isoformat(), "time": "14:00 ET", "category": "macro", "title": "FOMC rate decision", "detail": "Markets price a 25 bp hike.", "impact": "high", "source": "federalreserve.gov"},
        {"date": (today + timedelta(days=2)).isoformat(), "category": "earnings", "title": "NVDA earnings", "detail": "Consensus $46B.", "impact": "high", "source": "nvidia.com"},
        {"date": (today + timedelta(days=3)).isoformat(), "category": "unlock", "title": "ARB unlock 92M tokens", "impact": "medium", "source": "defillama.com"},
        {"date": (today + timedelta(days=40)).isoformat(), "category": "crypto", "title": "Too far out", "impact": "low"},
        {"date": "not a date", "category": "crypto", "title": "No date", "impact": "low"},
        {"date": today.isoformat(), "category": "weird", "title": "Today thing", "impact": "silly"},
    ]}
    monkeypatch.setattr(event_calendar, "perplexity_invoke", lambda *a: json.dumps(payload))
    data = event_calendar.get_calendar(7)
    titles = [e["title"] for e in data["events"]]
    assert titles == ["Today thing", "FOMC rate decision", "NVDA earnings", "ARB unlock 92M tokens"]
    assert data["events"][0]["category"] == "crypto" and data["events"][0]["impact"] == "medium"  # sanitised
    card = event_calendar.render(data)
    assert "🏛 **FOMC rate decision** · 14:00 ET · **HIGH** · federalreserve.gov" in card and "🔓 **ARB unlock 92M tokens**" in card
    assert event_calendar.today_events(data)[0]["title"] == "Today thing"
    # Cached: a second read within the window doesn't hit the source again.
    monkeypatch.setattr(event_calendar, "perplexity_invoke", lambda *a: (_ for _ in ()).throw(AssertionError("no second call")))
    assert len(event_calendar.get_calendar(7)["events"]) == 4
    # A narrower window filters the cached result.
    assert [e["title"] for e in event_calendar.get_calendar(1)["events"]] == ["Today thing", "FOMC rate decision"]


def test_calendar_reaches_research_endpoint_chips_and_brief(monkeypatch):
    today = datetime.now(timezone.utc).date()
    monkeypatch.setattr(event_calendar, "perplexity_available", lambda: True)
    monkeypatch.setattr(event_calendar, "perplexity_invoke", lambda *a: json.dumps({"events": [
        {"date": (today + timedelta(days=1)).isoformat(), "category": "macro", "title": "CPI print", "impact": "high", "source": "bls.gov"}]}))
    out = asyncio.run(research_mod.research_node({"request": "what events could move the market this week?", "capabilities": ["web_research"], "chains": [], "history": "", "session_context": {}}))
    assert out["answer"].startswith("# Market events") and "CPI print" in out["answer"] and out["trajectory"]["tool_name_0"] == "market_event_calendar"
    assert event_calendar.TRIGGER.search("when is the next FOMC meeting?")
    assert not event_calendar.TRIGGER.search("what is a liquidity pool?")
    client = TestClient(main.app)
    assert client.get("/calendar?days=7").json()["events"][0]["title"] == "CPI print"
    monkeypatch.setattr(home_highlights, "perplexity_available", lambda: False)
    monkeypatch.setattr(home_highlights.market_overview, "_get_json", lambda url: (_ for _ in ()).throw(RuntimeError("offline")))
    week = next(c for c in client.get("/home/suggestions").json()["categories"] if c["id"] == "week")
    assert week["rows"][0].startswith("What does CPI print on ")


def test_lunarcrush_path_is_preferred_and_renders_measured_metrics(monkeypatch):
    monkeypatch.setattr(settings, "lunarcrush_api_key", "lc-test")
    monkeypatch.setattr(settings, "x_bearer_token", "xoxb-should-not-be-used")
    seen = []

    class FakeResponse:
        def __init__(self, payload, status=200):
            self.payload, self.status_code = payload, status

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            assert headers["Authorization"] == "Bearer lc-test"
            seen.append(url)
            if url.endswith("/coins/sol/v1"):
                return FakeResponse({"data": {"name": "Solana", "symbol": "SOL", "price": 98.2, "percent_change_24h": -5.1, "galaxy_score": 71, "alt_rank": 4, "social_dominance": 3.4}})
            if url.endswith("/topic/solana/v1"):
                return FakeResponse({"data": {"topic": "solana", "topic_rank": 3, "sentiment": 68, "trend": "down", "interactions_24h": 12_400_000, "num_posts": 18_200, "num_contributors": 9_100, "types_sentiment": {"tweet": 66, "reddit-post": 71}}})
            if url.endswith("/topic/solana/posts/v1"):
                return FakeResponse({"data": [
                    {"post_type": "tweet", "post_title": "SOL ETF flows slowing | watch $95", "post_link": "https://x.com/a/status/1", "creator_name": "@analyst", "creator_followers": 320000, "interactions_24h": 45000},
                    {"post_type": "reddit-post", "post_title": "Solana upgrade thread", "post_link": "https://reddit.com/r/solana/x", "creator_name": "u/dev", "creator_followers": 1200, "interactions_24h": 900},
                ]})
            if url.endswith("/topic/solana/creators/v1"):
                return FakeResponse({"data": [{"creator_name": "@analyst", "creator_followers": 320000, "interactions_24h": 45000}, {"creator_name": "@small", "creator_followers": 500, "interactions_24h": 20}]})
            return FakeResponse({"error": "Topic not found"}, 404)

    monkeypatch.setattr(social_sentiment.httpx, "Client", FakeClient)
    card = social_sentiment.x_kol_sentiment("what are KOLs saying about SOL")
    assert card.startswith("# Social sentiment — SOL") and "**Provider**: LunarCrush API v4" in card
    assert "**bullish** — sentiment 68/100, trend down" in card
    assert "| Galaxy Score™ | 71 |" in card and "| AltRank™ | 4 |" in card and "| Interactions (24h) | 12,400,000 |" in card
    assert "| @analyst | 320,000 | 45,000 |" in card and "[SOL ETF flows slowing / watch $95](https://x.com/a/status/1)" in card
    assert "tweet: 66 · reddit-post: 71" in card
    assert not any("api.x.com" in u for u in seen)  # X API not touched when LunarCrush answered
