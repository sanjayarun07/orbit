from types import SimpleNamespace

import httpx

from app.provider_router import ProviderRouter, ProviderTool
from app.settings import settings


def _flaky_handler(exc: Exception):
    """Raises exc on the first call, succeeds on the second -- mirrors a
    one-off transient blip rather than a persistently broken tool."""
    calls = {"count": 0}

    def handler(_request: str) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise exc
        return "recovered result"

    return handler, calls


def test_transient_connect_error_is_retried_in_place():
    router = ProviderRouter()
    handler, calls = _flaky_handler(httpx.ConnectError("connection refused"))
    router.register(ProviderTool("primary", "one", ("market_data",), handler))

    result = router.route("BTC price now", "market_data")

    assert result.output == "recovered result"
    assert result.tool == "primary"  # retried the SAME tool, not a fallback to another candidate
    assert result.attempted == ("primary",)
    assert calls["count"] == 2


def test_transient_5xx_status_is_retried_in_place():
    router = ProviderRouter()
    response = httpx.Response(503, request=httpx.Request("GET", "https://example.test"))
    exc = httpx.HTTPStatusError("server error", request=response.request, response=response)
    handler, calls = _flaky_handler(exc)
    router.register(ProviderTool("primary", "one", ("market_data",), handler))

    result = router.route("BTC price now", "market_data")

    assert result.output == "recovered result"
    assert result.tool == "primary"
    assert calls["count"] == 2


def test_non_transient_status_falls_back_without_retrying():
    router = ProviderRouter()
    response = httpx.Response(400, request=httpx.Request("GET", "https://example.test"))
    exc = httpx.HTTPStatusError("bad request", request=response.request, response=response)
    handler, calls = _flaky_handler(exc)
    router.register(ProviderTool("primary", "one", ("market_data",), handler, priority=10))
    router.register(ProviderTool("fallback", "two", ("market_data",), lambda _request: "fresh result", priority=1))

    result = router.route("BTC price now", "market_data")

    assert result.output == "fresh result"
    assert result.tool == "fallback"
    assert calls["count"] == 1  # never retried a 400 -- fell straight back to the next candidate


def test_value_error_falls_back_without_retrying():
    """Non-regression: a plain logic error (e.g. an unsupported chain) must
    keep falling back exactly as it did before this change -- a retry of
    the identical request cannot fix it."""
    router = ProviderRouter()
    router.register(ProviderTool(
        "primary", "one", ("market_data",), lambda _request: (_ for _ in ()).throw(ValueError("bad chain")), priority=10
    ))
    router.register(ProviderTool("fallback", "two", ("market_data",), lambda _request: "fresh result", priority=1))

    result = router.route("BTC price now", "market_data")

    assert result.tool == "fallback"


def test_retry_count_is_tracked_per_tool_and_surfaced_in_catalog():
    router = ProviderRouter()
    handler, _calls = _flaky_handler(httpx.ConnectError("connection refused"))
    router.register(ProviderTool("primary", "one", ("market_data",), handler))

    router.route("BTC price now", "market_data")

    row = next(row for row in router.catalog() if row["name"] == "primary")
    assert row["retries"] == 1


def test_falls_back_after_provider_failure():
    router = ProviderRouter()
    router.register(ProviderTool(
        "primary", "one", ("market_data",), lambda _request: (_ for _ in ()).throw(RuntimeError("down")), priority=10
    ))
    router.register(ProviderTool(
        "fallback", "two", ("market_data",), lambda _request: "fresh result", priority=1
    ))
    result = router.route("BTC price now", "market_data")
    assert result.output == "fresh result"
    assert result.tool == "fallback"
    assert result.attempted == ("primary", "fallback")
    assert "primary" in result.failures[0]


def test_try_route_returns_none_when_no_provider_succeeds_but_route_still_raises():
    """The empty case is handled at the call site by branching on None
    (try_route), not by remembering to catch a RuntimeError (route). Both
    must agree on when nothing usable is available; route() is just the
    strict wrapper.
    """
    router = ProviderRouter()
    router.register(ProviderTool(
        "only", "one", ("market_data",),
        lambda _request: (_ for _ in ()).throw(RuntimeError("down")), priority=10,
    ))

    assert router.try_route("BTC price now", "market_data") is None

    try:
        router.route("BTC price now", "market_data")
        assert False, "route() must still raise when try_route() would return None"
    except RuntimeError:
        pass


def test_try_route_across_picks_globally_best_tool_regardless_of_capability_bucket():
    """The trades-vs-holders shape: a broad tool in an earlier-ordered
    capability bucket must not beat a more specific tool in a later bucket.
    Ranking the UNION of both buckets lets _score decide (keyword specificity
    + priority), instead of a fixed capability order picking the first
    bucket's only candidate.
    """
    router = ProviderRouter()
    router.register(ProviderTool(
        "broad", "one", ("token_security",),
        lambda _r: "broad-output", matches=lambda _r: True, priority=3,
    ))
    router.register(ProviderTool(
        "specific", "two", ("market_data",),
        lambda _r: "specific-output",
        matches=lambda r: "trades" in r.lower(), keywords=("trades",), priority=6,
    ))
    request = "recent trades for mint Xyz on solana"

    # Routing the earlier-ordered bucket alone yields only the broad tool...
    assert router.route(request, "token_security").tool == "broad"
    # ...but ranking across both buckets picks the specific, higher-scoring one.
    result = router.try_route_across(request, ("token_security", "market_data"))
    assert result is not None
    assert result.tool == "specific"
    assert result.output == "specific-output"


def test_try_route_across_invokes_a_multi_capability_tool_only_once():
    calls = []

    def handler(_request):
        calls.append(1)
        return "out"

    router = ProviderRouter()
    router.register(ProviderTool(
        "multi", "one", ("token_discovery", "market_data"), handler,
        matches=lambda _r: True, priority=5,
    ))

    result = router.try_route_across("some request", ("token_discovery", "market_data"))

    assert result is not None and result.tool == "multi"
    assert calls == [1]  # deduped across the two capabilities, not called twice


def test_matched_capabilities_surfaces_narrow_gated_tools_only():
    """The reachability backstop: a tool's OWN narrow matcher surfaces its
    capability, so it can be routed to even when the upstream classifier never
    emitted that capability. Broad catch-alls (default always-match) are
    excluded so they don't claim every request.
    """
    router = ProviderRouter()
    router.register(ProviderTool(
        "narrow", "one", ("market_data",), lambda _r: "x",
        matches=lambda r: "funding" in r.lower(),
    ))
    router.register(ProviderTool("catchall", "two", ("web_research",), lambda _r: "x"))  # default matcher

    assert router.matched_capabilities("funding rate for BTC on Hyperliquid") == {"market_data"}
    # The catch-all's capability is never surfaced, and a request the narrow
    # tool doesn't recognize surfaces nothing at all.
    assert router.matched_capabilities("tell me a joke") == set()


def test_matched_capabilities_respects_chain_compatibility():
    router = ProviderRouter()
    router.register(ProviderTool(
        "sol_only", "one", ("token_security",), lambda _r: "x",
        matches=lambda r: "safety" in r.lower(), chains=("solana",),
    ))
    assert router.matched_capabilities("safety check", ("solana",)) == {"token_security"}
    assert router.matched_capabilities("safety check", ("base",)) == set()


def test_try_route_across_returns_none_when_nothing_matches():
    router = ProviderRouter()
    router.register(ProviderTool(
        "gated", "one", ("market_data",), lambda _r: "x",
        matches=lambda r: "trades" in r.lower(),
    ))
    assert router.try_route_across("just a portfolio question", ("market_data", "token_security")) is None


def test_try_route_returns_result_on_success_matching_route():
    router = ProviderRouter()
    router.register(ProviderTool("primary", "one", ("market_data",), lambda _request: "ok"))

    result = router.try_route("BTC price now", "market_data")

    assert result is not None
    assert result.output == "ok"
    assert result.tool == "primary"


def test_falls_back_after_provider_returns_soft_failure():
    router = ProviderRouter()
    router.register(ProviderTool(
        "primary", "one", ("finance_data",),
        lambda _request: "I can’t retrieve the live Bitcoin price because market-data access is unavailable.",
        priority=10,
    ))
    router.register(ProviderTool(
        "fallback", "two", ("finance_data",),
        lambda _request: "Bitcoin is trading at $78,702.70 USD as of 10:30 UTC.",
        priority=1,
    ))

    result = router.route("Bitcoin price now", "finance_data")

    assert result.tool == "fallback"
    assert result.attempted == ("primary", "fallback")
    assert "unusable" in result.failures[0]


def test_quota_exhaustion_skips_provider():
    router = ProviderRouter()
    router.register(ProviderTool(
        "limited", "one", ("web_research",), lambda _request: "limited", quota_per_minute=1, cache_ttl_seconds=0, priority=10
    ))
    router.register(ProviderTool(
        "available", "two", ("web_research",), lambda _request: "fallback", priority=1
    ))
    assert router.route("first unique question", "web_research").tool == "limited"
    assert router.route("second unrelated question", "web_research").tool == "available"


def test_semantic_cache_reuses_equivalent_normalized_request():
    calls = []
    router = ProviderRouter()
    router.register(ProviderTool(
        "source", "one", ("finance_data",), lambda request: calls.append(request) or "42"
    ))
    assert router.route("BTC price now!", "finance_data").output == "42"
    result = router.route("btc PRICE now", "finance_data")
    assert result.cached is True
    assert calls == ["BTC price now!"]


def test_circuit_breaker_removes_unhealthy_provider(monkeypatch):
    monkeypatch.setattr(settings, "provider_circuit_failure_threshold", 1)
    router = ProviderRouter()
    router.register(ProviderTool(
        "broken", "one", ("url_fetch",), lambda _request: (_ for _ in ()).throw(RuntimeError("down")), priority=10
    ))
    router.register(ProviderTool(
        "healthy", "two", ("url_fetch",), lambda _request: "ok", priority=1
    ))
    assert router.route("https://one.example", "url_fetch").tool == "healthy"
    assert [item.name for item in router.candidates("https://two.example", "url_fetch")] == ["healthy"]


def test_request_match_guard_runs_before_cost_scoring():
    router = ProviderRouter()
    router.register(ProviderTool(
        "cheap_wrong_shape", "one", ("market_data",), lambda _request: "wrong",
        matches=lambda request: "trending" in request.lower(), cost_usd=0, priority=100,
    ))
    router.register(ProviderTool(
        "specific", "two", ("market_data",), lambda _request: "price", cost_usd=1,
    ))
    assert router.route("WIF price", "market_data").tool == "specific"


def test_runtime_policy_can_disable_and_reprioritize_tools():
    router = ProviderRouter()
    router.register(ProviderTool("first", "one", ("equity_research",), lambda _request: "one", priority=10))
    router.register(ProviderTool("second", "two", ("equity_research",), lambda _request: "two", priority=1))
    router.configure("first", {"enabled": False})
    assert router.route("research Apple stock", "equity_research").tool == "second"
    router.configure("first", {"enabled": True, "priority": -10, "quota_per_minute": 7, "cost_usd": 0.3})
    row = next(item for item in router.catalog() if item["name"] == "first")
    assert row["priority"] == -10
    assert row["quota_per_minute"] == 7
    assert row["cost_usd_per_invocation"] == 0.3


def test_route_can_be_restricted_to_one_provider():
    router = ProviderRouter()
    router.register(ProviderTool(
        "other", "openai", ("web_research",), lambda _request: "wrong provider", priority=100
    ))
    router.register(ProviderTool(
        "required", "perplexity", ("web_research",), lambda _request: "perplexity result", priority=1
    ))
    result = router.route("Research NVDA", "web_research", provider="perplexity")
    assert result.tool == "required"
    assert result.provider == "perplexity"


def test_narrow_compound_regex_tools_have_a_semantic_fallback_description():
    """Batch added on top of the original 5 pilots -- tools whose `matches`
    gate ANDs multiple conditions together (chain+address+keyword, or more)
    have the highest false-negative risk, since every part must match.
    Registers the real providers (not stubs) to catch a typo/removed
    `description=` regressing silently."""
    from app.provider_registry import get_provider_router

    router = get_provider_router()
    described = {tool.name: tool.description for tool in router._tools}
    for name in (
        "coingecko_token_by_contract", "coingecko_gainers_losers", "coinmarketcap_token_by_contract",
        "goplus_token_security", "honeypot_token_security", "defillama_chain_tvl", "defillama_protocols",
        "goldrush_wallet_transactions", "goldrush_wallet_balances", "goldrush_hyperliquid_positions",
        "helius_wallet_transactions", "exchange_listing_announcements",
    ):
        assert name in described, f"{name} is not registered"
        assert described[name], f"{name} has no semantic-fallback description"


def test_regex_gated_tool_becomes_candidate_via_semantic_fallback(monkeypatch):
    """A tool whose regex `matches` returns False is still excluded by
    default -- but with a `description` set, it gets a second chance
    through the semantic fallback (app/routing/tool_semantic.py), stubbed
    here so the test never touches a real embedding API.
    """
    from app import provider_router as provider_router_module

    router = ProviderRouter()
    router.register(ProviderTool(
        "regex_miss", "provider_a", ("market_data",), lambda _r: "output",
        matches=lambda _r: False, description="a tool description",
    ))

    class StubEmbeddingRouter:
        def qualify(self, request, capability, gated):
            names = frozenset(name for name, _ in gated)
            return SimpleNamespace(qualifying=names, method="embedding", reason="qualified")

    monkeypatch.setattr(provider_router_module, "tool_embedding_router", lambda _model: StubEmbeddingRouter())
    names = [tool.name for tool in router.candidates("some request", "market_data")]
    assert names == ["regex_miss"]


def test_tool_without_description_never_triggers_embedding_call(monkeypatch):
    from app import provider_router as provider_router_module

    router = ProviderRouter()
    router.register(ProviderTool(
        "regex_miss_no_description", "provider_a", ("market_data",), lambda _r: "output",
        matches=lambda _r: False,
    ))

    def fail_if_called(_model):
        raise AssertionError("tool_embedding_router must never be called for a tool with no description")

    monkeypatch.setattr(provider_router_module, "tool_embedding_router", fail_if_called)
    assert router.candidates("some request", "market_data") == []


def test_semantic_fallback_respects_chain_mismatch(monkeypatch):
    """Text similarity alone must never approve a tool for a chain it
    doesn't support -- this hard filter is new specifically for the
    semantic-fallback path (chain fit is only ever soft-scored for
    regex-matched tools elsewhere in candidates()).
    """
    from app import provider_router as provider_router_module

    router = ProviderRouter()
    router.register(ProviderTool(
        "solana_only", "provider_a", ("market_data",), lambda _r: "output",
        matches=lambda _r: False, description="a tool description", chains=("solana",),
    ))

    class StubEmbeddingRouter:
        def qualify(self, request, capability, gated):
            raise AssertionError("must never reach the embedding call once the chain mismatch already excluded the tool")

    monkeypatch.setattr(provider_router_module, "tool_embedding_router", lambda _model: StubEmbeddingRouter())
    assert router.candidates("some request", "market_data", chains=("base",)) == []


def test_preview_disables_the_semantic_fallback(monkeypatch):
    """preview() (the admin dashboard's read-only inspection endpoint) has
    no active call budget to bound repeat embedding spend against -- the
    semantic fallback must be off there regardless of gate state.
    """
    from app import provider_router as provider_router_module

    router = ProviderRouter()
    router.register(ProviderTool(
        "regex_miss", "provider_a", ("market_data",), lambda _r: "output",
        matches=lambda _r: False, description="a tool description",
    ))

    def fail_if_called(_model):
        raise AssertionError("preview() must never trigger the semantic fallback")

    monkeypatch.setattr(provider_router_module, "tool_embedding_router", fail_if_called)
    assert router.preview("some request", "market_data") == []
