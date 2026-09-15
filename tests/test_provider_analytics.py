from app import provider_analytics


def test_emit_turn_budget_event_is_a_noop_with_no_clickhouse_configured(monkeypatch):
    """Mirrors emit_provider_event's already-proven safe behavior (exercised
    implicitly by every provider_router test in the suite, none of which
    configure ClickHouse) -- must never raise or start a background thread
    when clickhouse_host is unset, the default in every test/dev environment."""
    monkeypatch.setattr(provider_analytics.settings, "clickhouse_host", None)
    provider_analytics.emit_turn_budget_event(
        intent="research", calls_used=2, cost_usd=0.01, calls_ceiling=12, cost_ceiling=0.5, capped=False,
    )
    assert provider_analytics._turn_started is False
