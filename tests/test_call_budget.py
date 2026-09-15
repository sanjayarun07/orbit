from app import call_budget


def test_charge_and_check_is_a_noop_with_no_active_budget():
    assert call_budget.current_budget() is None
    assert call_budget.charge_and_check() is True
    assert call_budget.charge_and_check(cost_usd=1000.0) is True  # unbounded outside a turn


def test_charge_and_check_counts_calls_and_stops_at_the_call_limit():
    token = call_budget.start_budget(max_calls=2, max_cost_usd=100.0)
    try:
        assert call_budget.charge_and_check() is True   # call 1
        assert call_budget.charge_and_check() is True   # call 2
        assert call_budget.charge_and_check() is False  # call 3 -- over the limit
        budget = call_budget.current_budget()
        assert budget.calls == 3
    finally:
        call_budget.reset_budget(token)
    assert call_budget.current_budget() is None


def test_charge_and_check_stops_at_the_cost_limit_even_under_the_call_limit():
    token = call_budget.start_budget(max_calls=100, max_cost_usd=0.05)
    try:
        assert call_budget.charge_and_check(cost_usd=0.03) is True
        assert call_budget.charge_and_check(cost_usd=0.03) is False  # $0.06 > $0.05 cap
    finally:
        call_budget.reset_budget(token)


def test_reset_budget_restores_the_prior_context():
    outer_token = call_budget.start_budget(max_calls=5, max_cost_usd=1.0)
    try:
        inner_token = call_budget.start_budget(max_calls=1, max_cost_usd=1.0)
        call_budget.charge_and_check()
        assert call_budget.charge_and_check() is False  # inner budget exhausted
        call_budget.reset_budget(inner_token)
        # back to the outer budget, which is still fresh
        assert call_budget.charge_and_check() is True
    finally:
        call_budget.reset_budget(outer_token)
    assert call_budget.current_budget() is None


def test_market_brief_narrative_route_sees_the_turn_budget(monkeypatch):
    # The narrative fetch runs try_route on a ThreadPoolExecutor thread, which
    # does not inherit contextvars by default -- the budget must still reach it.
    from app import market_brief

    seen: list[object] = []

    class _Router:
        def try_route(self, *args):
            seen.append(call_budget.current_budget())
            return None

    monkeypatch.setattr(market_brief, "_get_json", lambda url: {})
    monkeypatch.setattr(market_brief, "get_provider_router", lambda: _Router())
    token = call_budget.start_budget(max_calls=5, max_cost_usd=1.0)
    try:
        market_brief._build_crypto_market_brief("how is the market")
    finally:
        call_budget.reset_budget(token)
    assert seen and seen[0] is not None
