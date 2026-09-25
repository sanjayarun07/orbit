"""Dated reporting remains a linked lead, separate from measured market data."""

import asyncio
from datetime import datetime, timezone

from app import market_today


def test_reporting_lead_requires_a_recent_direct_article():
    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    found = {"text": "The market crashed because of an uncited rumor.", "sources": [
        {"title": "Home", "url": "https://example.com/", "date": "2026-09-25"},
        {"title": "Old report", "url": "https://example.com/old", "date": "2026-09-23"},
        {"title": "Current direct report", "url": "https://example.com/news/2026/09/25/report", "date": "2026-09-25"},
    ]}
    assert market_today._dated_lead(found, now=now)["title"] == "Current direct report"
    assert market_today._dated_lead({"sources": found["sources"][:2]}, now=now) is None


def test_reporting_lead_prefers_today_then_citation_order():
    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    found = {"text": "Important event [3]. A separate development [2].", "sources": [
        {"title": "Yesterday", "url": "https://example.com/2026/09/24/story", "date": "2026-09-24"},
        {"title": "Today second", "url": "https://example.com/2026/09/25/second", "date": "2026-09-25"},
        {"title": "Today first", "url": "https://example.com/2026/09/25/first", "date": "2026-09-25"},
    ]}
    assert market_today._dated_lead(found, now=now)["title"] == "Today first"


def test_reporting_lead_rejects_a_freshly_updated_index_or_tracker():
    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    found = {"sources": [
        {"title": "Latest crypto news", "url": "https://example.com/market/market-news/", "date": "2026-09-25"},
        {"title": "ETF flows chart", "url": "https://example.com/treasuries/etf-flows/", "date": "2026-09-25"},
    ]}
    assert market_today._dated_lead(found, now=now) is None


def test_reporting_lead_accepts_a_specific_article_without_a_date_in_its_url():
    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    found = {"sources": [{"title": "Crypto market steadies after exchange breach",
                          "url": "https://example.com/markets/crypto-market-steadies-after-exchange-breach",
                          "date": "2026-09-25"}]}
    assert market_today._dated_lead(found, now=now)["title"] == "Crypto market steadies after exchange breach"


def test_reporting_lead_prefers_a_specific_story_over_a_daily_roundup():
    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    found = {"text": "Daily roundup [1]. Specific report [2].", "sources": [
        {"title": "Latest Crypto News Update - September 25, 2026", "url": "https://example.com/ai/a/crypto-news-update-25-September-2026", "date": "2026-09-25"},
        {"title": "Bitcoin steadies as rate bets pressure crypto", "url": "https://example.org/crypto-markets-bitcoin-majors-friday-september-25-2026/", "date": "2026-09-25"},
    ]}
    assert market_today._dated_lead(found, now=now)["title"] == "Bitcoin steadies as rate bets pressure crypto"


def test_market_today_combines_independent_sources_without_repeating_search_prose(monkeypatch):
    monkeypatch.setattr(market_today.market_overview, "crypto_market_overview", lambda _: "# Crypto Market Overview\nBTC $84K · [CoinGecko](https://example.com/prices)")
    monkeypatch.setattr(market_today.perplexity_tools, "perplexity_available", lambda: True)
    def search(query, *, recency_days):
        assert recency_days == 2
        axis = "news" if "developments" in query else "flows"
        return {"text": "Unsupported claim: BTC rose because of this.", "sources": [{
            "title": "Direct " + axis + " report", "url": "https://example.com/2026/09/25/" + axis + "/story",
            "date": datetime.now(timezone.utc).date().isoformat(),
        }]}
    monkeypatch.setattr(market_today.perplexity_tools, "perplexity_search_with_sources", search)
    answer, trajectory = asyncio.run(market_today.compose("How's the crypto market today?"))
    assert "Direct news report" in answer and "Direct flows report" in answer
    assert "Unsupported claim" not in answer
    assert "does not establish that they caused" in answer
    assert trajectory["tool_name_0"] == "crypto_market_overview"
    assert trajectory["tool_name_1"] == "perplexity_web_search"


def test_market_today_keeps_the_snapshot_when_reporting_is_unavailable(monkeypatch):
    monkeypatch.setattr(market_today.market_overview, "crypto_market_overview", lambda _: "# Crypto Market Overview\nBTC $84K")
    monkeypatch.setattr(market_today.perplexity_tools, "perplexity_available", lambda: False)
    answer, trajectory = asyncio.run(market_today.compose("crypto market today"))
    assert "No recent, dated reporting" in answer and "BTC $84K" in answer
    assert trajectory["tool_name_0"] == "crypto_market_overview"


def test_reporting_search_retries_an_index_once_for_a_direct_article(monkeypatch):
    now = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(market_today.perplexity_tools, "perplexity_available", lambda: True)
    calls = []
    def search(query, *, recency_days):
        calls.append(query)
        if len(calls) == 1:
            return {"sources": [{"title": "Latest news", "url": "https://example.com/news/", "date": "2026-09-25"}]}
        return {"sources": [{"title": "Direct article", "url": "https://example.com/2026/09/25/article", "date": "2026-09-25"}]}
    monkeypatch.setattr(market_today.perplexity_tools, "perplexity_search_with_sources", search)
    assert market_today._search("news", "crypto news", now)["title"] == "Direct article"
    assert len(calls) == 2 and "Exclude rolling feeds" in calls[1]


def test_market_today_keeps_x_posts_separate_from_reporting_and_figures(monkeypatch):
    monkeypatch.setattr(market_today.market_overview, "crypto_market_overview", lambda _: "# Crypto Market Overview\nBTC $84K")
    monkeypatch.setattr(market_today.perplexity_tools, "perplexity_available", lambda: False)
    monkeypatch.setattr(market_today.x_news, "enabled", lambda: True)
    async def social(*args, **kwargs):
        return {"status": "ok", "posts": [{"author": "trader", "id": "123", "created_at": "2026-09-25T12:00:00+00:00",
                                          "text": "Rumour: ETF approval", "url": "https://x.com/trader/status/123"}]}
    monkeypatch.setattr(market_today.x_news, "search", social)
    answer, trajectory = asyncio.run(market_today.compose("How's crypto today?"))
    assert "X posts (unverified)" in answer and "Rumour" in answer
    assert "independent verification" in answer and "BTC $84K" in answer
    assert trajectory["tool_name_1"] == "x_news_search"
