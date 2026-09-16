"""Thumbs up/down on an answer feed the tools behind it into outcome scoring:
weighted, idempotent per turn, reversible, and visible in the ranking term."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import execution_policy
from app import feedback, main, tool_outcomes
from app.graph import AgentRun
from app.settings import settings


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    async def no_pool():
        return None

    async def no_redis():
        return None

    monkeypatch.setattr(tool_outcomes, "get_pg_pool", no_pool)
    monkeypatch.setattr(feedback, "get_pg_pool", no_pool)
    monkeypatch.setattr(feedback, "get_redis", no_redis)
    tool_outcomes._counts.clear()
    feedback.reset()
    yield
    tool_outcomes._counts.clear()
    feedback.reset()


@pytest.fixture
def fake_agent(monkeypatch):
    async def fake_run(message, wallet, history, session_context, action):
        trajectory = {"tool_name_0": "birdeye_token_overview", "observation_0": "ok", "tool_name_1": "semantic_cache", "observation_1": "cached"}
        return AgentRun(answer="ok", trajectory=trajectory, trade_plan=None, intent="research", capabilities=["market_data"])

    monkeypatch.setattr(execution_policy, "run_agent", fake_run)


def test_rating_moves_the_tools_behind_the_turn_and_is_reversible(fake_agent):
    client = TestClient(main.app)
    turn = client.post("/chat", json={"message": "price of BONK"}).json()
    sid, rev = turn["session_id"], turn["session_revision"]
    before = tool_outcomes.adjustment("birdeye_token_overview")

    down = client.post("/chat/feedback", json={"session_id": sid, "session_revision": rev, "rating": "down"})
    assert down.status_code == 200 and down.json() == {"rating": "down", "tools": ["birdeye_token_overview"], "changed": True}
    counts = tool_outcomes._counts["birdeye_token_overview"]
    assert counts.feedback_down == 1 and counts.feedback_up == 0
    assert tool_outcomes.adjustment("birdeye_token_overview") < before  # a poor answer ranks the tool down
    assert "semantic_cache" not in tool_outcomes._counts

    # Clicking the same rating again is a no-op; switching to up moves both counters.
    assert client.post("/chat/feedback", json={"session_id": sid, "session_revision": rev, "rating": "down"}).json()["changed"] is False
    assert counts.feedback_down == 1
    client.post("/chat/feedback", json={"session_id": sid, "session_revision": rev, "rating": "up"})
    assert (counts.feedback_up, counts.feedback_down) == (1, 0)
    assert tool_outcomes.adjustment("birdeye_token_overview") > before
    # Retracting returns the tool to where it was.
    client.post("/chat/feedback", json={"session_id": sid, "session_revision": rev, "rating": "none"})
    assert (counts.feedback_up, counts.feedback_down) == (0, 0)
    assert tool_outcomes.adjustment("birdeye_token_overview") == pytest.approx(before)
    assert asyncio.run(feedback.rating_for(sid, rev)) == "none"


def test_feedback_weighs_more_than_one_automatic_outcome(monkeypatch):
    monkeypatch.setattr(settings, "tool_feedback_weight", 3.0)
    asyncio.run(tool_outcomes.record_turn({"tool_name_0": "x_tool", "observation_0": "ok"}, None))
    clean = tool_outcomes.adjustment("x_tool")
    asyncio.run(tool_outcomes.record_feedback(["x_tool"], 0, 1))
    rated_down = tool_outcomes.adjustment("x_tool")
    asyncio.run(tool_outcomes.record_feedback(["x_tool"], 0, -1))
    asyncio.run(tool_outcomes.record_turn({"tool_name_0": "x_tool", "observation_0": "MCP tool call failed: boom"}, None))
    one_failure = tool_outcomes.adjustment("x_tool")
    assert rated_down < one_failure < clean  # one thumbs-down outweighs one failed call
    row = next(r for r in tool_outcomes.snapshot() if r["tool"] == "x_tool")
    assert row["feedback_down"] == 0 and row["calls"] == 2


def test_feedback_validation_and_unknown_turns(fake_agent):
    client = TestClient(main.app)
    turn = client.post("/chat", json={"message": "hello"}).json()
    assert client.post("/chat/feedback", json={"session_id": turn["session_id"], "session_revision": 99, "rating": "up"}).status_code == 404
    assert client.post("/chat/feedback", json={"session_id": "nope", "session_revision": 1, "rating": "up"}).status_code == 404
    assert client.post("/chat/feedback", json={"session_id": turn["session_id"], "session_revision": turn["session_revision"], "rating": "meh"}).status_code == 422
    assert client.get("/health").json()["counters"].get("feedback_up", 0) >= 0
