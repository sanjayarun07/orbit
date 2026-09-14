from types import SimpleNamespace

from app import call_budget, web_search as web_search_module
from app.web_search import _dedupe_response, _openai_web_search, is_crypto_trends_query
from app import market_brief


def test_duplicate_search_response_is_collapsed():
    response = "Market overview\n\n- BTC leads\n\nMarket overview\n\n- BTC leads"
    assert _dedupe_response(response) == "Market overview\n\n- BTC leads"


def test_openai_web_search_is_refused_without_calling_the_api_when_budget_exhausted(monkeypatch):
    calls = []

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                return SimpleNamespace(output=[], output_text="should not be reached")

    monkeypatch.setattr(web_search_module, "_get_client", lambda: FakeClient())
    token = call_budget.start_budget(max_calls=0, max_cost_usd=100.0)
    try:
        result = _openai_web_search("What's the latest on SOL?")
    finally:
        call_budget.reset_budget(token)
    assert result == "Web search unavailable: per-turn data budget reached."
    assert calls == []


def test_openai_web_search_strips_raw_citation_markers(monkeypatch):
    # Live-observed: OpenAI's Responses API sometimes embeds an inline
    # citation annotation directly in output_text using Private Use Area
    # sentinel characters (U+E200 ... U+E201) wrapping a "cite" marker, an
    # internal reference, and the source URL -- meant to be resolved via the
    # response's structured `annotations` array, not shown to the user. This
    # reproduced live via /chat and, once cached, was replayed verbatim to
    # every subsequent caller of the same query until eviction.
    leaked = (
        "Meow is the founder of Jupiter Exchange"
        "citeturn0search1https://example.com/a"
        " and Ben Chow is a co-founder."
    )

    class FakeClient:
        class responses:
            @staticmethod
            def create(**kwargs):
                return SimpleNamespace(output=[], output_text=leaked)

    monkeypatch.setattr(web_search_module, "_get_client", lambda: FakeClient())
    token = call_budget.start_budget(max_calls=10, max_cost_usd=100.0)
    try:
        result = _openai_web_search("Who is the founder of Jupiter exchange?")
    finally:
        call_budget.reset_budget(token)
    assert "" not in result and "" not in result and "" not in result
    assert "cite" not in result
    assert result == "Meow is the founder of Jupiter Exchange and Ben Chow is a co-founder."


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
        try_route=lambda *_args, **_kwargs: SimpleNamespace(output="No fresh narratives available.")
    )
    monkeypatch.setattr(market_brief, "get_provider_router", lambda: router)
    market_brief._cache.clear()
    result = market_brief.crypto_market_brief("What's trending in crypto right now?")
    assert "## If you want, I can:" in result
    assert "Show new token launches" in result
    assert "holder concentration" in result


def test_market_brief_naming_a_chain_scopes_fetches_and_narrative_to_that_chain(monkeypatch):
    """'trending tokens on solana' must not silently answer with an
    unscoped, all-chain brief: fetches, the narrative request, and the
    output should all be scoped to the named chain.
    """
    called_urls = []

    def fake_get_json(url):
        called_urls.append(url)
        if "/overview/dexs/solana" in url:
            return {"total24h": 555_000_000, "change_1d": 1.0, "change_7d": 2.0}
        if "/overview/dexs/" in url and "solana" not in url:
            raise AssertionError(f"fetched an unrequested chain: {url}")
        return {}

    captured_route_args = {}

    def fake_route(request, capability, chains):
        captured_route_args.update({"request": request, "capability": capability, "chains": chains})
        return SimpleNamespace(output="Solana narrative placeholder.")

    monkeypatch.setattr(market_brief, "_get_json", fake_get_json)
    monkeypatch.setattr(market_brief, "get_provider_router", lambda: SimpleNamespace(try_route=fake_route))
    market_brief._cache.clear()

    result = market_brief.crypto_market_brief("trending tokens on solana")

    assert not any("/overview/dexs/ethereum" in url or "/overview/dexs/base" in url for url in called_urls)
    assert captured_route_args["chains"] == ("solana",)
    assert "solana" in captured_route_args["request"].lower()
    assert result.startswith("# SOLANA crypto market brief")
    assert "## SOLANA DEX volume" in result
    assert "## Where the volume is" not in result
