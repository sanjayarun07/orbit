import asyncio
from types import SimpleNamespace

import pytest

from app import tool_outcomes
from app.settings import settings


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    async def no_pool():
        return None

    monkeypatch.setattr(tool_outcomes, "get_pg_pool", no_pool)
    monkeypatch.setattr(settings, "provider_outcome_weight", 3.0)
    monkeypatch.setattr(settings, "tool_outcome_prior_rate", 0.8)
    monkeypatch.setattr(settings, "tool_outcome_prior_weight", 5.0)
    tool_outcomes._counts.clear()
    yield
    tool_outcomes._counts.clear()


def _validation(grounding_status):
    return SimpleNamespace(checks=[SimpleNamespace(name="grounding", status=grounding_status)])


def test_outcomes_credit_usable_results_and_debit_failed_or_ungrounded_ones():
    traj = {
        "tool_name_0": "birdeye_token_overview", "observation_0": "**Price**: $1.20",
        "tool_name_1": "mobula_token_details", "observation_1": "Mobula request failed: 429",
        "tool_name_2": "semantic_cache", "observation_2": "cached",
        "market_research": {"tool_name_0": "geckoterminal_pools", "observation_0": "Liquidity $9M"},  # team desk nesting
    }
    assert tool_outcomes.outcomes_from_turn(traj, _validation("ok")) == [
        ("birdeye_token_overview", True), ("mobula_token_details", False), ("geckoterminal_pools", True),
    ]
    # An ungrounded answer debits every tool that ran (the data was not usable).
    assert all(not ok for _, ok in tool_outcomes.outcomes_from_turn(traj, _validation("warn")))
    # No grounding check at all (nothing to trace): a clean call still counts as a success.
    assert tool_outcomes.outcomes_from_turn({"tool_name_0": "x", "observation_0": "fine"}, None) == [("x", True)]
    assert tool_outcomes.outcomes_from_turn("not a dict", None) == []


def test_unobserved_tool_gets_zero_adjustment_then_earns_one_from_evidence():
    assert tool_outcomes.adjustment("never_seen") == 0.0
    bad = {"tool_name_0": "flaky_tool", "observation_0": "provider error 500"}
    for _ in range(3):
        asyncio.run(tool_outcomes.record_turn(bad, None))
    three = tool_outcomes.adjustment("flaky_tool")
    assert three < 0  # below the prior after three misses
    for _ in range(7):
        asyncio.run(tool_outcomes.record_turn(bad, None))
    assert tool_outcomes.adjustment("flaky_tool") < three  # more evidence, stronger debit
    # Prior weight 5 + rate 0.8 with 10/10 misses: (0 + 4) / 15 = 0.267 -> 3 * (0.267 - 0.8)
    assert tool_outcomes.adjustment("flaky_tool") == pytest.approx(3.0 * (4 / 15 - 0.8))
    good = {"tool_name_0": "solid_tool", "observation_0": "**Price**: $1"}
    for _ in range(10):
        asyncio.run(tool_outcomes.record_turn(good, _validation("ok")))
    assert tool_outcomes.adjustment("solid_tool") > 0
    rows = {r["tool"]: r for r in tool_outcomes.snapshot()}
    assert rows["flaky_tool"]["trusted"] is True and rows["solid_tool"]["calls"] == 10


def test_router_ranking_uses_the_outcome_term(monkeypatch):
    from app.provider_registry import get_provider_router

    router = get_provider_router()
    tool = next(t for t in router._tools if t.name == "birdeye_token_overview")
    request, chains = "price of BONK on solana", ("solana",)
    base = router._score(tool, request, chains)
    monkeypatch.setattr(tool_outcomes, "adjustment", lambda name: -2.0 if name == tool.name else 0.0)
    assert router._score(tool, request, chains) == pytest.approx(base - 2.0)


def test_admin_outcomes_endpoint_requires_the_admin_key(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    monkeypatch.setattr(settings, "admin_api_key", "test-admin")
    client = TestClient(main.app)
    assert client.get("/admin/tools/outcomes").status_code == 401
    asyncio.run(tool_outcomes.record_turn({"tool_name_0": "t", "observation_0": "ok"}, None))
    body = client.get("/admin/tools/outcomes", headers={"Authorization": "Bearer test-admin"}).json()
    assert body["prior_rate"] == 0.8 and body["tools"][0]["tool"] == "t" and body["tools"][0]["calls"] == 1
