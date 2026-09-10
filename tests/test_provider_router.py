from app.provider_router import ProviderRouter, ProviderTool
from app.settings import settings


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
