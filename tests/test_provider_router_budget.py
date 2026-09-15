from app import call_budget
from app.provider_router import ProviderRouter, ProviderTool


def _router_with_two_candidates(handler_a, handler_b, cost_a=0.0, cost_b=0.0):
    router = ProviderRouter()
    router.register(ProviderTool("tool_a", "provider_a", ("market_data",), handler_a, cost_usd=cost_a, priority=10))
    router.register(ProviderTool("tool_b", "provider_b", ("market_data",), handler_b, cost_usd=cost_b, priority=1))
    return router


def test_route_stops_early_once_budget_is_exhausted():
    """tool_a (higher priority) is the only candidate that would ever be
    tried if the budget is already exhausted before route() starts -- the
    call must never reach tool_a's handler, and route() must fail the same
    way it does when every candidate is genuinely exhausted (a plain
    RuntimeError every caller of route() already handles).
    """
    calls = []

    def handler_a(request):
        calls.append("a")
        return "output-a"

    def handler_b(request):
        calls.append("b")
        return "output-b"

    router = _router_with_two_candidates(handler_a, handler_b)
    token = call_budget.start_budget(max_calls=0, max_cost_usd=100.0)
    try:
        try:
            router.route("some request", "market_data")
            assert False, "expected RuntimeError"
        except RuntimeError:
            pass
    finally:
        call_budget.reset_budget(token)
    assert calls == []


def test_route_charges_the_tool_cost_against_the_budget():
    def handler_a(request):
        return "output-a"

    router = _router_with_two_candidates(handler_a, lambda r: "output-b", cost_a=0.05)
    token = call_budget.start_budget(max_calls=10, max_cost_usd=100.0)
    try:
        router.route("some request", "market_data")
        budget = call_budget.current_budget()
        assert budget.cost_usd == 0.05
        assert budget.calls == 1
    finally:
        call_budget.reset_budget(token)


def test_route_semantic_cache_hit_does_not_charge_the_budget():
    calls = []

    def handler_a(request):
        calls.append("a")
        return "output-a"

    router = _router_with_two_candidates(handler_a, lambda r: "output-b")
    token = call_budget.start_budget(max_calls=1, max_cost_usd=100.0)
    try:
        first = router.route("some request", "market_data")
        assert first.output == "output-a"
        # This call already used the turn's one available "real" call;
        # the second route() call for the same request must be served from
        # the semantic cache, not a second charge against the budget.
        second = router.route("some request", "market_data")
        assert second.cached is True
        assert len(calls) == 1
    finally:
        call_budget.reset_budget(token)
