"""X is an optional, attributed discovery lane, not verified market data."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

from app import research_loop, x_news


def _tweet(i, *, author="analyst", minutes=5, text="BTC ETF flows look higher"):
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return {"id": str(i), "text": text, "createdAt": created.strftime("%a %b %d %H:%M:%S +0000 %Y"),
            "author": {"userName": author}, "likeCount": 8}


def test_x_discovery_keeps_recent_unique_authors_and_direct_links(monkeypatch):
    x_news.reset_for_test()
    monkeypatch.setattr(x_news.x_tweets, "enabled", lambda: True)
    monkeypatch.setattr(x_news.x_tweets, "_page", lambda query, cursor: {"tweets": [
        _tweet(1), _tweet(2, author="analyst", minutes=10),
        _tweet(3, author="reporter", minutes=20), _tweet(4, author="old", minutes=3000),
        _tweet(5, author="bad)link", minutes=2),
    ]})
    result = asyncio.run(x_news.search("BTC ETF", hours=48))
    assert result["status"] == "ok" and len(result["posts"]) == 2
    assert result["posts"][0]["url"] == "https://x.com/analyst/status/1"
    card = x_news.render(result)
    assert "X posts (unverified)" in card and "independent verification" in card
    assert "since:" in result["query"] and "-is:retweet" in result["query"]


def test_payment_failure_opens_a_breaker_and_does_not_break_research(monkeypatch):
    x_news.reset_for_test()
    monkeypatch.setattr(x_news.x_tweets, "enabled", lambda: True)
    calls = []
    def denied(query, cursor):
        calls.append(query)
        request = httpx.Request("GET", "https://api.twitterapi.io/twitter/tweet/advanced_search")
        raise httpx.HTTPStatusError("payment required", request=request, response=httpx.Response(402, request=request))
    monkeypatch.setattr(x_news.x_tweets, "_page", denied)
    assert asyncio.run(x_news.search("BTC"))["status"] == "unavailable"
    assert asyncio.run(x_news.search("BTC"))["status"] == "unavailable"
    assert len(calls) == 1
    x_news.reset_for_test()


def test_recent_news_research_adds_x_as_labelled_context_without_changing_gate(monkeypatch):
    monkeypatch.setattr(x_news, "enabled", lambda: True)
    monkeypatch.setattr(x_news, "search", lambda *args, **kwargs: asyncio.sleep(0, result={
        "query": "Fed stablecoin", "posts": [{"author": "FedNews", "id": "42", "created_at": "2026-09-25T12:00:00+00:00",
            "text": "A proposal was announced", "url": "https://x.com/FedNews/status/42"}]}))
    async def core(*args):
        return {"answer": "Checked answer", "trajectory": {"research_loop": {}}, "gate": {"ok": True}}
    monkeypatch.setattr(research_loop, "_run", core)
    contract = SimpleNamespace(kind="recent_events", window_hours=24)
    out = asyncio.run(research_loop.run({}, "Fed stablecoin rules", contract, None, ()))
    assert out["gate"] == {"ok": True}
    assert "X posts (unverified)" in out["answer"] and "Checked answer" in out["answer"]
    assert out["trajectory"]["x_discovery"]["posts"][0]["url"].endswith("/42")
