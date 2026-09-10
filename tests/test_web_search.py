from types import SimpleNamespace

from app.web_search import _dedupe_response, is_crypto_trends_query
from app import market_brief


def test_duplicate_search_response_is_collapsed():
    response = "Market overview\n\n- BTC leads\n\nMarket overview\n\n- BTC leads"
    assert _dedupe_response(response) == "Market overview\n\n- BTC leads"


def test_nonduplicate_search_response_is_preserved():
    response = "Market overview\n\n- BTC leads\n\nRisks\n\n- Volatility"
    assert _dedupe_response(response) == response


def test_crypto_trend_queries_use_market_brief_route():
    assert is_crypto_trends_query("What's trending in crypto right now?")
    assert is_crypto_trends_query("Show hot Web3 tokens")
    assert not is_crypto_trends_query("bitcoin price now")


def test_market_brief_ends_with_actionable_handoff(monkeypatch):
    monkeypatch.setattr(market_brief, "_get_json", lambda _url: {})
    router = SimpleNamespace(
        route=lambda *_args: SimpleNamespace(output="No fresh narratives available.")
    )
    monkeypatch.setattr(market_brief, "get_provider_router", lambda: router)
    market_brief._cache.clear()
    result = market_brief.crypto_market_brief("What's trending in crypto right now?")
    assert "## If you want, I can:" in result
    assert "Show new token launches" in result
    assert "holder concentration" in result
