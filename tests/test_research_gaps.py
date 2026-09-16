"""The fall-through log: research turns only web search could answer, tagged
by the data topic they needed."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import execution_policy
from app import main, research_gaps
from app.settings import settings


@pytest.fixture(autouse=True)
def memory_gaps(monkeypatch):
    research_gaps.reset()

    async def no_pool():
        return None

    monkeypatch.setattr(research_gaps, "get_pg_pool", no_pool)
    yield
    research_gaps.reset()


def test_classification_names_the_data_the_question_needed():
    assert research_gaps.classify("how big is the Arbitrum DAO treasury") == "treasury"
    assert research_gaps.classify("bitcoin ETF outflows today") == "etf_flows"
    assert research_gaps.classify("when is the next ARB unlock") == "unlocks"
    assert research_gaps.classify("tokenized treasuries on Ethereum") == "rwa"
    assert research_gaps.classify("what is the funding rate on HYPE perps") == "funding_rates"
    assert research_gaps.classify("tell me about pudgy penguins") == "other"
    assert research_gaps.needs_for("treasury").startswith("DefiLlama API tier")


def test_only_web_only_research_turns_count():
    assert research_gaps.is_fallthrough("research", ["perplexity_web_search"])
    assert research_gaps.is_fallthrough("research", ["semantic_cache", "perplexity_web_search", "finish"])
    assert not research_gaps.is_fallthrough("research", ["perplexity_web_search", "defillama_protocols"])   # a data tool answered
    assert not research_gaps.is_fallthrough("research", ["knowledge_base_search"])
    assert not research_gaps.is_fallthrough("general", ["perplexity_web_search"])
    assert not research_gaps.is_fallthrough("research", [])


def test_record_and_summary_count_by_topic():
    assert asyncio.run(research_gaps.record("how big is the Arbitrum DAO treasury", "research", ["perplexity_web_search"], session_id="s1")) == "treasury"
    assert asyncio.run(research_gaps.record("bitcoin ETF flows this week", "research", ["perplexity_web_search"])) == "etf_flows"
    assert asyncio.run(research_gaps.record("Optimism treasury runway", "research", ["perplexity_web_search"])) == "treasury"
    assert asyncio.run(research_gaps.record("Aave TVL", "research", ["defillama_protocols"])) is None
    summary = asyncio.run(research_gaps.summary(days=7))
    assert summary["total"] == 3 and summary["topics"][0]["topic"] == "treasury" and summary["topics"][0]["count"] == 2
    assert summary["topics"][0]["samples"][0]["request"].startswith("Optimism") and summary["topics"][0]["needs"]


def test_admin_endpoint_requires_the_key(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-secret")
    client = TestClient(main.app)
    assert client.get("/admin/research/gaps").status_code in (401, 403)
    asyncio.run(research_gaps.record("ETF inflows", "research", ["perplexity_web_search"]))
    data = client.get("/admin/research/gaps?days=30", headers={"Authorization": "Bearer admin-secret"}).json()
    assert data["total"] == 1 and data["topics"][0]["topic"] == "etf_flows"


def test_chat_turn_records_a_fallthrough(monkeypatch):
    from app.graph import AgentRun

    async def fake_run(message, wallet, history, session_context, action):
        return AgentRun(answer="web says…", trajectory={"tool_name_1": "perplexity_web_search"}, trade_plan=None, intent="research", capabilities=["web_research"])

    monkeypatch.setattr(execution_policy, "run_agent", fake_run)
    client = TestClient(main.app)
    assert client.post("/chat", json={"message": "how much is in the Arbitrum DAO treasury"}).status_code == 200
    assert asyncio.run(research_gaps.summary(days=1))["topics"][0]["topic"] == "treasury"
