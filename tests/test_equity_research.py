from app.nodes import runtime, routing, portfolio, research
import asyncio
from types import SimpleNamespace

from app import graph
from app import equities_data


def test_equity_research_uses_two_perplexity_evidence_streams(monkeypatch):
    calls = []

    class Router:
        def route(self, request, capability, chains, *, provider=None):
            calls.append((request, capability, chains, provider))
            if capability == "finance_data":
                return SimpleNamespace(
                    tool="perplexity_finance_search",
                    output="NVDA is $200. [Quote](https://example.com/quote)",
                )
            return SimpleNamespace(
                tool="perplexity_web_search",
                output="A new product launched. [News](https://example.com/news)",
            )

    async def call_lm(_program, **kwargs):
        assert "$NVDA" in kwargs["request"]
        assert "NVDA is $200" in kwargs["financial_evidence"]
        assert "new product" in kwargs["news_evidence"]
        return SimpleNamespace(answer="## Bottom line\nGrounded NVDA analysis.")

    monkeypatch.setattr(research, "get_provider_router", lambda: Router())
    monkeypatch.setattr(runtime, "_call_lm", call_lm)
    result = asyncio.run(research._equity_research({"request": "Analyze $NVDA", "history": ""}))

    assert len(calls) == 2
    assert {call[1] for call in calls} == {"finance_data", "web_research"}
    assert all(call[3] == "perplexity" for call in calls)
    assert result["trajectory"]["tool_name_0"] == "perplexity_finance_search"
    assert result["trajectory"]["tool_name_1"] == "perplexity_web_search"
    assert "https://example.com/quote" in result["answer"]
    assert "https://example.com/news" in result["answer"]


def test_equity_research_joins_matching_gateway_record(monkeypatch):
    class Router:
        def route(self, request, capability, chains, *, provider=None):
            return SimpleNamespace(tool="perplexity_finance_search" if capability == "finance_data" else "perplexity_web_search",
                                   output="Web background with a source. [Source](https://example.com/story)")

    async def synthesize(_program, **kwargs):
        assert "insider_trades" in kwargs["financial_evidence"]
        assert "2026-09-25" in kwargs["financial_evidence"]
        return SimpleNamespace(answer="AAPL insider activity was reported on 2026-09-25.")

    monkeypatch.setattr(research, "get_provider_router", lambda: Router())
    monkeypatch.setattr(runtime, "_call_lm", synthesize)
    monkeypatch.setattr(equities_data, "enabled", lambda: True)
    monkeypatch.setattr(equities_data, "run", lambda name, request: '{"insider_trades":[{"ticker":"AAPL","filing_date":"2026-09-25"}]}')
    result = asyncio.run(research._equity_research({"request": "AAPL insider trades", "history": ""}))
    assert result["trajectory"]["tool_name_2"] == "equities_insider_trades"
