"""Contract tests for the equities-data gateway and its chat routing."""

import json

import httpx
import pytest

from app import equities_data as gateway
from app.provider_registry import get_provider_router


@pytest.mark.parametrize("name,prompt,expected", [
    ("equities_company_news", "AAPL news", {"tickers": "AAPL"}),
    ("equities_earnings_upcoming", "when is NVDA next earnings date", {"limit": 10}),
    ("equities_price_history", "AAPL historical price 30d", {"symbols": "AAPL", "interval": "day", "range": "30d"}),
    ("equities_ratings_consensus", "AAPL analyst consensus", {"ticker": "AAPL"}),
    ("equities_institutional_holdings", "institutional holdings CIK 0001067983", {"filer_cik": "0001067983"}),
    ("equities_venue_movers", "Hyperliquid stock gainers", {"dex": "hyperliquid", "category": "stock", "tab": "gainers"}),
])
def test_documented_query_mapping(name, prompt, expected):
    params = gateway._params(gateway.BY_NAME[name], prompt)
    assert params.items() >= expected.items()
    if name == "equities_price_history":
        assert "limit" not in params  # unsupported by the actual endpoint


@pytest.mark.parametrize("prompt,tool", [
    ("latest insider trades for AAPL", "equities_insider_trades"),
    ("when is NVDA next earnings date", "equities_earnings_upcoming"),
    ("AAPL analyst consensus", "equities_ratings_consensus"),
    ("SEC filings for AAPL", "equities_filings"),
    ("latest SEC filings", "equities_filings_feed"),
    ("institutional holdings of AAPL", "equities_institutional_holdings"),
    ("CPI inflation data", "equities_inflation"),
    ("yield curve today", "equities_yield_curve"),
])
def test_specific_requests_reach_their_data_source(prompt, tool):
    assert tool in gateway.matching_tools(prompt)


def test_disjoint_equity_sources():
    assert "equities_earnings_history" not in gateway.matching_tools("when is NVDA next earnings date")
    assert "equities_ratings" not in gateway.matching_tools("AAPL analyst consensus")
    assert "equities_venue_movers" not in gateway.matching_tools("Aster stock gainers")  # existing live feed leads


def test_intraday_is_not_silently_substituted_with_daily_bars():
    with pytest.raises(ValueError, match="daily bars only"):
        gateway._params(gateway.BY_NAME["equities_price_history"], "AAPL intraday 1m price history")


def test_bare_crypto_ticker_does_not_use_cash_equity_prices():
    assert "equities_price_snapshot" not in gateway.matching_tools("SOL price today")
    assert "equities_price_snapshot" in gateway.matching_tools("BP stock price today")
    assert "equities_price_snapshot" not in gateway.matching_tools("TSLA price on Hyperliquid")


def test_every_collection_read_endpoint_is_registered():
    registered = {tool.name for tool in get_provider_router().tools()}
    assert {endpoint.name for endpoint in gateway.ENDPOINTS} <= registered


def test_internal_auth_and_user_watchlist_jwt_are_separate(monkeypatch):
    monkeypatch.setattr(gateway.settings, "equities_data_base_url", "https://gateway.example")
    monkeypatch.setattr(gateway.settings, "equities_data_internal_token", "private-token")
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, json={"data": {"items": []}}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    gateway.health()
    gateway.watchlist_list("user-jwt")
    assert "X-Internal-Token" not in calls[0][2]["headers"]
    assert calls[1][2]["headers"]["X-Internal-Token"] == "private-token"
    assert calls[1][2]["headers"]["Authorization"] == "Bearer user-jwt"
    assert "private-token" not in json.dumps(calls[1][2]["params"] or {})


def test_health_accepts_gateway_plain_text(monkeypatch):
    monkeypatch.setattr(gateway.settings, "equities_data_base_url", "https://gateway.example")

    def fake_request(self, method, url, **kwargs):
        return httpx.Response(200, text="ok", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    assert gateway.health() == {"status": "ok"}


def test_watchlist_and_cache_methods_map_to_collection_operations(monkeypatch):
    calls = []
    monkeypatch.setattr(gateway, "_request", lambda *args, **kwargs: calls.append((args, kwargs)) or {"data": {}})
    gateway.watchlist_add("NVDA", "user-jwt", "earnings_calendar")
    gateway.watchlist_remove("NVDA", "user-jwt")
    gateway.watchlist_list("user-jwt", "earnings_calendar")
    gateway.cache_delete("ratings:pagesize=3")
    assert [(args[0], args[1]) for args, _ in calls] == [
        ("POST", "/api/v1/watchlist"), ("DELETE", "/api/v1/watchlist"),
        ("GET", "/api/v1/watchlist"), ("DELETE", "/api/v1/internal/cache"),
    ]
    assert calls[0][1]["body"] == {"ticker": "NVDA", "source": "earnings_calendar"}
    assert calls[2][1]["params"] == {"source": "earnings_calendar"}
    assert calls[3][1]["params"] == {"key": "ratings:pagesize=3"}


def test_response_is_bounded_valid_json(monkeypatch):
    monkeypatch.setattr(gateway, "_request", lambda *a, **k: {"data": {"earnings": [{"ticker": "AAPL", "text": "x" * 1200}] * 80}})
    card = gateway.run("equities_earnings_history", "AAPL earnings")
    assert len(card) < 12000
    payload = card.split("```json\n", 1)[1].split("\n```", 1)[0]
    parsed = json.loads(payload)
    assert len(parsed["data"]["earnings"]) <= 5


def test_current_rates_show_newest_observations(monkeypatch):
    monkeypatch.setattr(gateway, "_request", lambda *a, **k: {"data": {"interest_rates": [
        {"date": "1954-07-01", "rate": 1}, {"date": "2026-08-01", "rate": 4},
    ]}})
    card = gateway.run("equities_interest_rates", "current Fed interest rates")
    payload = card.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(payload)["data"]["interest_rates"][0]["date"] == "2026-08-01"


def test_current_macro_series_reaches_gateway_before_web_research(monkeypatch):
    import asyncio
    from app.nodes import research

    monkeypatch.setattr(gateway, "enabled", lambda: True)
    monkeypatch.setattr(gateway, "run", lambda name, prompt: "CPI reading: 2.7% as of 2026-09-25")
    result = asyncio.run(research.research_node({"request": "current CPI inflation data", "history": "", "capabilities": ["finance_data"], "chains": []}))
    assert result["trajectory"]["tool_name_0"] == "equities_inflation"
    assert "2.7%" in result["answer"]


def test_venue_stock_pair_quote_keeps_venue_source(monkeypatch):
    import asyncio
    from app import tequity
    from app.nodes import research

    monkeypatch.setattr(tequity, "enabled", lambda: True)
    monkeypatch.setattr(tequity, "quote", lambda prompt: "TSLA Hyperliquid pair: $240")
    result = asyncio.run(research.research_node({"request": "TSLA price on Hyperliquid", "history": "", "capabilities": ["equity_research"], "chains": []}))
    assert result["trajectory"]["tool_name_0"] == "tequity_quote"
    assert "Hyperliquid" in result["answer"]
